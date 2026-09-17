"""One-shot APRS transmit: Dire Wolf modulates the frame, the radio sends it.

Dire Wolf's ``gen_packets`` utility turns one TNC2-format line into a complete
Ball 202 burst as a mono 16-bit WAV. Pi-Sat renders that burst at the active
transport's TX audio rate, keys the native IC-9700 over CI-V, streams the audio
at real time, and unkeys. Only the shared controller's ``set_ptt`` and
``send_audio`` are used, so the transport details stay in the connectivity
module.
"""
from __future__ import annotations

import logging
import os
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Any

from pi_sat_controller.backend.aprs.callsign import (
    is_transmit_callsign,
    resolve_mycall,
)


LOGGER = logging.getLogger(__name__)

# Dire Wolf's standard VHF packet modem: Bell 202 AFSK, 1200 baud, 1200/2200 Hz.
MODEM_BAUD = 1200
_GENERATOR_TIMEOUT_S = 15.0
# Audio is paced at real time so the radio sees a normal audio stream rather
# than one oversized write.
_AUDIO_CHUNK_SECONDS = 0.02
_PCM_BYTES_PER_SAMPLE = 2
_MAX_INFO_BYTES = 256
_MAX_STATUS_TEXT = 62
_MAX_MESSAGE_TEXT = 67
_PTT_RELEASE_ATTEMPTS = 3
_PTT_RELEASE_RETRY_S = 0.15
# The controller confirms a release asynchronously (the radio may still be
# draining transmit audio), so wait for the confirmed state before declaring the
# transmission finished.
_PTT_RELEASE_CONFIRM_S = 2.5
_DEFAULT_DESTINATION = "APDW16"
_DEFAULT_PATH = "WIDE1-1,WIDE2-1"


def _radio_connected(controller: Any) -> bool:
    """Whether the controller still reports a live radio session."""
    try:
        return bool((controller.snapshot() or {}).get("connected"))
    except Exception:
        return False


def _degrees_minutes(value: float, degree_digits: int, positive: str, negative: str) -> str:
    """Format ``degrees`` + decimal ``minutes`` for one APRS coordinate."""
    hemisphere = negative if value < 0 else positive
    magnitude = abs(float(value))
    degrees = int(magnitude)
    minutes = round((magnitude - degrees) * 60.0, 2)
    if minutes >= 60.0:
        degrees += 1
        minutes -= 60.0
    return f"{degrees:0{degree_digits}d}{minutes:05.2f}{hemisphere}"


def format_latitude(latitude_deg: float) -> str:
    return _degrees_minutes(latitude_deg, 2, "N", "S")


def format_longitude(longitude_deg: float) -> str:
    return _degrees_minutes(longitude_deg, 3, "E", "W")


def split_symbol(symbol: str | None) -> tuple[str, str]:
    """Split the stored two-character symbol into its table and code characters."""
    text = str(symbol or "").strip()
    if len(text) != 2:
        return "/", ">"
    return text[0], text[1]


def build_position_info(
    latitude_deg: float,
    longitude_deg: float,
    *,
    symbol: str | None = "/>",
    comment: str = "",
) -> str:
    """Build an APRS position report without a timestamp."""
    table, code = split_symbol(symbol)
    return (
        "!"
        + format_latitude(latitude_deg)
        + table
        + format_longitude(longitude_deg)
        + code
        + str(comment or "").strip()
    )


def build_line(
    mycall: str | None,
    destination: str | None,
    path: str | None,
    info: str,
) -> str:
    """Build one TNC2 monitor line: ``SOURCE>DEST,PATH:info``."""
    if len(info.encode("ascii", "replace")) > _MAX_INFO_BYTES:
        raise ValueError("The APRS information field is longer than 256 characters.")
    source = str(mycall or "").strip().upper()
    if not source:
        raise ValueError("An APRS callsign is required.")
    dest = str(destination or "").strip().upper() or _DEFAULT_DESTINATION
    digipeaters = ",".join(
        part.strip().upper() for part in str(path or "").split(",") if part.strip()
    )
    header = f"{source}>{dest}"
    if digipeaters:
        header = f"{header},{digipeaters}"
    return f"{header}:{info}"


def build_beacon_info(
    *,
    latitude_deg: float,
    longitude_deg: float,
    symbol: str | None = "/>",
    comment: str = "",
) -> str:
    return build_position_info(
        latitude_deg,
        longitude_deg,
        symbol=symbol,
        comment=comment,
    )


def build_status_info(text: str) -> str:
    message = str(text or "").strip()
    if not message:
        raise ValueError("Enter a message to transmit.")
    if len(message) > _MAX_STATUS_TEXT:
        raise ValueError(f"APRS status text is limited to {_MAX_STATUS_TEXT} characters.")
    return f">{message}"


def build_message_info(addressee: str, text: str) -> str:
    target = str(addressee or "").strip().upper()
    if not target:
        raise ValueError("Enter an addressee callsign.")
    if len(target) > 9:
        raise ValueError("An APRS message addressee is limited to 9 characters.")
    message = str(text or "").strip()
    if not message:
        raise ValueError("Enter a message to transmit.")
    if len(message) > _MAX_MESSAGE_TEXT:
        raise ValueError(f"APRS message text is limited to {_MAX_MESSAGE_TEXT} characters.")
    return f":{target:<9}:{message}"


def read_wav_pcm(data: bytes) -> tuple[bytes, dict[str, int]]:
    """Return the PCM payload and format details from a RIFF/WAVE byte string."""
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("The APRS modulator did not return a RIFF/WAVE stream.")
    offset = 12
    fmt: dict[str, int] | None = None
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        chunk_size = struct.unpack_from("<I", data, offset + 4)[0]
        body = offset + 8
        end = body + chunk_size
        if end > len(data):
            break
        if chunk_id == b"fmt " and chunk_size >= 16:
            audio_format, channels, sample_rate, _, _, bits = struct.unpack_from(
                "<HHIIHH", data, body
            )
            fmt = {
                "format": audio_format,
                "channels": channels,
                "sample_rate": sample_rate,
                "bits": bits,
            }
        elif chunk_id == b"data":
            if fmt is None:
                raise ValueError("The APRS modulator audio block had no format header.")
            if fmt["format"] != 1 or fmt["bits"] != 16:
                raise ValueError("Only 16-bit PCM APRS modulator output is supported.")
            if fmt["channels"] != 1:
                raise ValueError("The APRS modulator must return mono audio.")
            return data[body:end], fmt
        offset = end + (chunk_size & 1)
    raise ValueError("The APRS modulator output contained no audio block.")


def _generator_path() -> Path | None:
    """Locate ``gen_packets``, which Dire Wolf installs beside ``direwolf``."""
    override = os.environ.get("PI_SAT_APRS_GEN_PACKETS")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate
    found = shutil.which("gen_packets")
    if found:
        return Path(found)
    direwolf = os.environ.get("PI_SAT_APRS_DIREWOLF")
    if direwolf:
        sibling = Path(direwolf).expanduser().parent / "gen_packets"
        if sibling.is_file():
            return sibling
    return None


def render_audio(*, line: str, sample_rate: int, amplitude: int) -> bytes:
    """Modulate one TNC2 line into mono 16-bit PCM at ``sample_rate``."""
    tool = _generator_path()
    if tool is None:
        raise RuntimeError(
            "gen_packets was not found; install Dire Wolf's utilities or set "
            "PI_SAT_APRS_GEN_PACKETS."
        )
    if sample_rate <= 0:
        raise RuntimeError("The APRS modulator needs a positive sample rate.")
    with tempfile.TemporaryDirectory(prefix="pi-sat-aprs-") as directory:
        wav_path = Path(directory) / "burst.wav"
        argv = [
            str(tool),
            "-B", str(MODEM_BAUD),
            "-r", str(int(sample_rate)),
            "-a", str(int(amplitude)),
            "-o", str(wav_path),
            "-",
        ]
        try:
            completed = subprocess.run(
                argv,
                input=(line + "\n").encode("ascii", "replace"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_GENERATOR_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("The APRS modulator timed out.") from exc
        except OSError as exc:
            raise RuntimeError(f"Unable to run the APRS modulator: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(
                f"The APRS modulator failed: {detail or f'exit status {completed.returncode}'}"
            )
        try:
            payload = wav_path.read_bytes()
        except OSError as exc:
            raise RuntimeError("The APRS modulator produced no audio.") from exc
    pcm, fmt = read_wav_pcm(payload)
    if not pcm:
        raise RuntimeError("The APRS modulator produced no audio; check the frame content.")
    if int(fmt["sample_rate"]) != int(sample_rate):
        raise RuntimeError("The APRS modulator returned an unexpected sample rate.")
    return pcm


class AprsTransmitter:
    """Renders and keys one APRS transmission at a time through the native radio."""

    READY = "Ready"
    DISARMED = "Disarmed"
    TRANSMITTING = "Transmitting"
    FAILED = "Failed"

    def __init__(
        self,
        *,
        project_root: Path,
        data_dir: Path,
        get_controller: Callable[[], Any],
        get_config: Callable[[], Any],
    ) -> None:
        self.project_root = Path(project_root)
        self.data_dir = Path(data_dir)
        self.get_controller = get_controller
        self.get_config = get_config
        self._lock = Lock()
        self._state_lock = Lock()
        self._armed = False
        self._transmitting = False
        self._error: str | None = None
        # A non-fatal condition worth showing the operator, e.g. a transmit that
        # went out whose PTT release could not be confirmed.
        self._warning: str | None = None
        self._tx_count = 0
        self._last_tx_at: float | None = None
        self._last_tx_utc: str | None = None
        self._last_line: str | None = None
        self._last_bytes = 0
        self._last_sample_rate: int | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "armed": self._armed,
                "state": self._state_locked(),
                "error": self._error,
                "warning": self._warning,
                "tx_count": self._tx_count,
                "last_tx_utc": self._last_tx_utc,
                "last_line": self._last_line,
                "last_bytes": self._last_bytes,
                "sample_rate": self._last_sample_rate,
                "generator": str(_generator_path() or ""),
            }

    def set_armed(self, armed: bool) -> dict[str, Any]:
        if type(armed) is not bool:
            raise ValueError("armed must be a boolean")
        with self._state_lock:
            self._armed = armed
            if not armed:
                self._error = None
                self._warning = None
        return self.snapshot()

    def shutdown(self) -> None:
        """Disarm so a restarted or stopped backend never transmits unattended."""
        with self._state_lock:
            self._armed = False

    def transmit(
        self,
        *,
        kind: str = "beacon",
        text: str = "",
        addressee: str = "",
    ) -> dict[str, Any]:
        """Render and send one frame, returning the updated transmit status."""
        with self._state_lock:
            if not self._armed:
                self._error = "Arm APRS transmit before sending."
                raise ValueError(self._error)
        try:
            config = self.get_config()
        except Exception as exc:
            self._fail(f"APRS configuration is unavailable: {exc}")
            raise ValueError(self._error or "APRS configuration is unavailable.") from exc
        aprs = config.aprs
        mycall = resolve_mycall(aprs.mycall)
        if not is_transmit_callsign(mycall):
            self._fail("Set a valid APRS callsign in Settings before transmitting.")
            raise ValueError(self._error or "Set a valid APRS callsign.")
        normalized = str(kind or "beacon").strip().lower()
        if normalized == "beacon":
            station = config.station
            info = build_beacon_info(
                latitude_deg=station.latitude_deg,
                longitude_deg=station.longitude_deg,
                symbol=aprs.symbol,
                comment=aprs.comment,
            )
        elif normalized == "status":
            try:
                info = build_status_info(text)
            except ValueError as exc:
                self._fail(str(exc))
                raise
        elif normalized == "message":
            try:
                info = build_message_info(addressee, text)
            except ValueError as exc:
                self._fail(str(exc))
                raise
        else:
            self._fail(f"Unsupported APRS transmit kind: {kind}")
            raise ValueError(self._fail_message())
        try:
            line = build_line(mycall, aprs.destination, aprs.path, info)
        except ValueError as exc:
            self._fail(str(exc))
            raise
        return self._send(line, aprs)

    def _send(self, line: str, aprs: Any) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            self._fail("An APRS transmission is already in progress.")
            raise ValueError(self._fail_message())
        try:
            controller = self.get_controller()
            if controller is None:
                self._fail("The native IC-9700 controller is unavailable.")
                raise ValueError(self._fail_message())
            radio = controller.snapshot() or {}
            if not radio.get("connected"):
                self._fail("Connect the native IC-9700 before transmitting.")
                raise ValueError(self._fail_message())
            if radio.get("ptt") is True:
                self._fail("The radio is already transmitting.")
                raise ValueError(self._fail_message())
            interval = max(0, int(getattr(aprs, "min_interval_s", 0) or 0))
            now = monotonic()
            if self._last_tx_at is not None and interval and now - self._last_tx_at < interval:
                remaining = int(interval - (now - self._last_tx_at)) + 1
                self._fail(f"Wait {remaining} s before the next APRS transmission.")
                raise ValueError(self._fail_message())
            sample_rate = int(getattr(controller.config, "sample_rate", 0) or 0)
            if sample_rate <= 0:
                self._fail("The radio TX audio sample rate is unknown.")
                raise ValueError(self._fail_message())
            problem = self._transmit_audio_problem(controller)
            if problem is not None:
                self._fail(problem)
                raise ValueError(problem)
            try:
                pcm = render_audio(
                    line=line,
                    sample_rate=sample_rate,
                    amplitude=int(getattr(aprs, "amplitude", 100) or 100),
                )
            except RuntimeError as exc:
                self._fail(str(exc))
                raise ValueError(self._fail_message()) from exc
            peak = max((abs(sample) for (sample,) in struct.iter_unpack("<h", pcm)), default=0)
            if peak == 0:
                self._fail("The APRS modulator returned silent audio; LAN transmit was cancelled.")
                raise ValueError(self._fail_message())
            LOGGER.info(
                "APRS LAN audio prepared: rate=%d format=S16_LE channels=1 bytes=%d duration_s=%.3f peak=%d",
                sample_rate, len(pcm), len(pcm) / (2 * sample_rate), peak,
            )
            self._mark_transmitting()
            failure: str | None = None
            paced = getattr(controller, "set_transmit_stream_paced", None)
            if paced is not None:
                # Claim the transmit buffer before the key reaches the radio. The
                # browser streams microphone audio for the whole session, so a
                # claim made after key-up would let microphone frames prepend this
                # modulated burst. It also tells the core not to insert silence
                # frames into a real-time modulated stream.
                paced(True)
            try:
                controller.set_ptt(True)
                self._delay_ms(int(getattr(aprs, "tx_delay_ms", 0) or 0))
                self._stream(controller, pcm, sample_rate)
                controller.flush_audio()
                LOGGER.info("APRS LAN audio submitted to transport: bytes=%d", len(pcm))
                self._delay_ms(int(getattr(aprs, "tx_tail_ms", 0) or 0))
            except Exception as exc:
                LOGGER.exception("APRS transmit failed while keyed")
                failure = f"APRS transmit failed: {exc}"
            finally:
                if paced is not None:
                    paced(False)
                # Always release PTT, including when key-up or readback failed
                # part way; an unkey against an already-idle radio is harmless.
                # A release problem is a safety warning, not a failed frame: the
                # audio may already have been transmitted, so it must not be
                # reported as a failure or dropped from the transmit count.
                release_warning = self._release_ptt(controller)
            if failure:
                self._fail(failure)
                raise ValueError(self._fail_message())
            self._record_success(line, len(pcm), sample_rate)
            if release_warning is not None:
                self._set_warning(
                    "APRS transmit could not confirm the radio stopped transmitting "
                    f"({release_warning}). Confirm the radio is not still transmitting."
                )
            return self.snapshot()
        finally:
            self._lock.release()

    @staticmethod
    def _release_ptt(controller: Any) -> str | None:
        """Unkey the radio, returning a warning when the release is unconfirmed.

        A frame that already reached the radio is not failed by a release
        problem, so the caller reports this as a warning rather than a failed
        transmission. When the session is already gone there is nothing to
        retry: the controller's teardown path has commanded an unkey already.
        """
        wait_for_ptt = getattr(controller, "wait_for_ptt", None)
        last_error: str | None = None
        for attempt in range(_PTT_RELEASE_ATTEMPTS):
            try:
                controller.set_ptt(False)
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                LOGGER.warning(
                    "APRS transmit did not release PTT (attempt %d of %d): %s",
                    attempt + 1,
                    _PTT_RELEASE_ATTEMPTS,
                    last_error,
                )
                if not _radio_connected(controller):
                    break
                if attempt + 1 < _PTT_RELEASE_ATTEMPTS:
                    sleep(_PTT_RELEASE_RETRY_S)
                continue
            if wait_for_ptt is None or wait_for_ptt(False, _PTT_RELEASE_CONFIRM_S):
                return None
            last_error = "the radio did not confirm it stopped transmitting"
            LOGGER.warning(
                "APRS transmit could not confirm PTT release (attempt %d of %d)",
                attempt + 1,
                _PTT_RELEASE_ATTEMPTS,
            )
            if attempt + 1 < _PTT_RELEASE_ATTEMPTS:
                sleep(_PTT_RELEASE_RETRY_S)
        return last_error or "the radio did not confirm PTT release"

    @staticmethod
    def _transmit_audio_problem(controller: Any) -> str | None:
        """Report when the radio would key without taking Pi-Sat's audio.

        The IC-9700 chooses its modulation input separately from PTT. If
        ``DATA OFF MOD`` and ``DATA MOD`` are not pointed at the transport that
        carries our PCM, or the LAN level is zero, the radio transmits a
        carrier with nothing on it.
        """
        try:
            # Re-establish and verify the LAN input before keying, including
            # when a front-panel change happened after the initial connection.
            state = controller.configure_microphone(use_transport=True) or {}
        except Exception as exc:
            LOGGER.warning("Could not read the transmit audio configuration: %s", exc)
            return f"LAN transmit audio configuration could not be verified; PTT was not keyed: {exc}"
        microphone = state.get("microphone") if isinstance(state, dict) else None
        if not isinstance(microphone, dict):
            return "LAN transmit audio configuration readback is missing; PTT was not keyed."
        LOGGER.info(
            "APRS LAN modulation readback: data_off_mod=%s data_mod=%s lan_mod_level=%s/255",
            microphone.get("data_off_mod"), microphone.get("data_mod"), microphone.get("lan_mod_level"),
        )
        if (
            microphone.get("data_off_mod") != 5
            or microphone.get("data_mod") != 5
        ):
            return (
                "The radio is not set to take transmit audio from the LAN input, "
                "so transmitting would only produce a carrier. Set DATA OFF MOD and "
                "DATA MOD to the active transport on the Radio page under microphone "
                "and transmit audio configuration."
            )
        if (type(microphone.get("lan_mod_level")) is not int
                or not 0 <= microphone["lan_mod_level"] <= 255):
            return "The radio's LAN modulation level could not be verified; PTT was not keyed."
        if microphone["lan_mod_level"] == 0:
            return (
                "The radio's LAN modulation level is 0, so transmitting would only "
                "produce a carrier. Raise it on the Radio page under microphone and "
                "transmit audio configuration."
            )
        return None

    @staticmethod
    def _delay_ms(milliseconds: int) -> None:
        if milliseconds > 0:
            sleep(milliseconds / 1000.0)

    @staticmethod
    def _stream(controller: Any, pcm: bytes, sample_rate: int) -> None:
        """Send the burst at real time so the radio sees a normal audio stream."""
        chunk_bytes = max(
            _PCM_BYTES_PER_SAMPLE,
            int(sample_rate * _AUDIO_CHUNK_SECONDS) * _PCM_BYTES_PER_SAMPLE,
        )
        chunk_seconds = (chunk_bytes // _PCM_BYTES_PER_SAMPLE) / sample_rate
        started_at = monotonic()
        for index, start in enumerate(range(0, len(pcm), chunk_bytes)):
            target = started_at + index * chunk_seconds
            remaining = target - monotonic()
            if remaining > 0:
                sleep(remaining)
            controller.send_audio(pcm[start:start + chunk_bytes])

    def _state_locked(self) -> str:
        if self._transmitting:
            return self.TRANSMITTING
        if self._error:
            return self.FAILED
        return self.READY if self._armed else self.DISARMED

    def _fail_message(self) -> str:
        with self._state_lock:
            return self._error or "APRS transmit is unavailable."

    def _fail(self, message: str) -> None:
        with self._state_lock:
            self._error = message
            self._warning = None
            self._transmitting = False

    def _mark_transmitting(self) -> None:
        with self._state_lock:
            self._transmitting = True
            self._error = None
            self._warning = None

    def _set_warning(self, message: str) -> None:
        with self._state_lock:
            self._warning = message

    def _record_success(self, line: str, byte_count: int, sample_rate: int) -> None:
        with self._state_lock:
            self._transmitting = False
            self._error = None
            self._warning = None
            self._tx_count += 1
            self._last_tx_at = monotonic()
            self._last_tx_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._last_line = line
            self._last_bytes = byte_count
            self._last_sample_rate = sample_rate
