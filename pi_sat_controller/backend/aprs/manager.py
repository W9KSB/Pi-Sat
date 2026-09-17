"""Owns one Dire Wolf process fed from a copy of native Icom RX PCM."""
from __future__ import annotations

from array import array
from collections import deque
from collections.abc import Callable
import logging
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
import shutil
import socket
import subprocess
import sys
from threading import Event, Lock, RLock, Thread
from time import monotonic, sleep
from typing import Any

from pi_sat_controller.backend.aprs.ax25 import KissDecoder, decode_ax25
from pi_sat_controller.backend.aprs.callsign import resolve_mycall
from pi_sat_controller.backend.aprs.log import AprsLog
from pi_sat_controller.backend.aprs.packets import describe
from pi_sat_controller.backend.audio_gain import apply_gain_db, clamp_gain_db, peak_of


LOGGER = logging.getLogger(__name__)

_DEFAULT_KISS_PORT = 8001
_KISS_CONNECT_TIMEOUT_S = 2.0
_KISS_RETRY_DELAY_S = 1.5
_LISTENING_STALE_S = 2.0
_DECODER_LOG_LINES = 200
_DECODER_ERROR_MARKERS = ("error", "fail", "fatal", "denied", "unable", "cannot", "can't")


def _peak_of(samples: "array") -> int:
    peak = 0
    for value in samples:
        magnitude = -value if value < 0 else value
        if magnitude > peak:
            peak = magnitude
    return peak


def _decode_pcm(pcm: bytes) -> "array":
    """Return one PCM block as native-order 16-bit samples."""
    usable = len(pcm) - (len(pcm) % 2)
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _encode_pcm(samples: "array") -> bytes:
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def _dbfs(peak: int) -> float | None:
    return round(20 * math.log10(peak / 32768.0), 1) if peak > 0 else None


class AprsManager:
    """Decodes APRS from the MAIN receive channel without touching the radio."""

    DISABLED = "Disabled"
    WAITING = "Waiting for radio audio"
    LISTENING = "Listening"
    FAILED = "Decode failed"

    def __init__(
        self,
        *,
        project_root: Path,
        data_dir: Path,
        get_controller: Callable[[], Any],
        get_config: Callable[[], Any] | None = None,
        rx_gain_db: float = -6.0,
    ) -> None:
        self.project_root = Path(project_root)
        self.data_dir = Path(data_dir)
        self.log = AprsLog(self.data_dir, retention=25)
        self.get_controller = get_controller
        self.get_config = get_config
        self._lock = RLock()
        self._lifecycle_lock = Lock()
        self._process_lock = Lock()
        self._enabled = False
        self._state = self.DISABLED
        self._error: str | None = None
        self._packet_count = 0
        self._input_samples = 0
        self._input_rate: int | None = None
        self._input_channels = 0
        self._input_channel = "main"
        self._input_peak_left = 0
        self._input_peak_right = 0
        # Level of the trimmed stream actually handed to the decoder.
        self._decoder_peak = 0
        self._rx_gain_db = clamp_gain_db(rx_gain_db)
        self._last_input_at: float | None = None
        self._subscribers: list[Queue[dict[str, Any]]] = []
        self._sequence = 0
        self._stop = Event()
        self._wake = Event()
        self._telemetry_lock = Lock()
        self._decoder_output: deque[str] = deque(maxlen=_DECODER_LOG_LINES)
        self._kiss_connected = False
        self._kiss_attempts = 0
        self._kiss_failures = 0
        self._thread: Thread | None = None
        self._reader_thread: Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_generation = 0
        self._fatal_worker = False
        self._kiss_port = _kiss_port_from_environment()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "state": self._state,
                "error": self._error,
                "packet_count": self._packet_count,
                "retained": len(self.log.list()),
                "retention": self.log.retention,
                "input_samples": self._input_samples,
                "input_rate": self._input_rate,
                "input_channels": self._input_channels,
                "input_channel": self._input_channel,
                "input_level_left_dbfs": _dbfs(self._input_peak_left),
                "input_level_right_dbfs": _dbfs(self._input_peak_right),
                "rx_gain_db": self._rx_gain_db,
                "decoder_level_dbfs": _dbfs(self._decoder_peak),
                "audio_source": "Native IC-9700 RX PCM",
                "decoder": "Dire Wolf",
                "kiss_port": self._kiss_port,
                "kiss_connected": self._kiss_connected,
                "kiss_attempts": self._kiss_attempts,
                "kiss_failures": self._kiss_failures,
                "sequence": self._sequence,
            }

    def decoder_log(self) -> list[str]:
        """Return the newest lines Dire Wolf printed, oldest first."""
        with self._telemetry_lock:
            return list(self._decoder_output)

    def set_rx_gain_db(self, value: Any) -> dict[str, Any]:
        """Trim the level of the decoder's own copy of the receive audio.

        This affects only the secondary stream that feeds Dire Wolf. The radio,
        the browser playback level, and every other audio consumer are
        untouched.
        """
        gain = clamp_gain_db(value)
        with self._lock:
            self._rx_gain_db = gain
        return self.snapshot()

    def apply_rx_gain(self, pcm: bytes) -> bytes:
        """Return a scaled copy of ``pcm`` for the decoder, clipped to range."""
        return apply_gain_db(pcm, self._rx_gain_db)

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lifecycle_lock:
            if enabled:
                with self._lock:
                    if self._enabled:
                        return self.snapshot()
                    self._enabled = True
                    self._state = self.WAITING
                    self._error = None
                    self._fatal_worker = False
                    self._input_peak_left = 0
                    self._input_peak_right = 0
                    self._stop.clear()
                    self._thread = Thread(target=self._run, name="aprs-decoder", daemon=True)
                    thread = self._thread
                thread.start()
                self._publish_status()
            else:
                with self._lock:
                    if not self._enabled:
                        return self.snapshot()
                    self._enabled = False
                    self._state = self.DISABLED
                    self._error = None
                    thread = self._thread
                    self._thread = None
                self._stop.set()
                self._wake.set()
                self._terminate_worker()
                if thread is not None:
                    thread.join(timeout=3.0)
                reader = self._reader_thread
                if reader is not None:
                    reader.join(timeout=3.0)
                self._publish_status()
        return self.snapshot()

    def shutdown(self) -> None:
        self.set_enabled(False)

    def subscribe(self) -> Queue[dict[str, Any]]:
        subscriber: Queue[dict[str, Any]] = Queue(maxsize=1024)
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = [item for item in self._subscribers if item is not subscriber]

    @staticmethod
    def extract_main_pcm(packet: bytes, channels: int, selection: str = "main") -> bytes:
        """Return the decoder's channel as mono PCM from one native audio packet.

        Icom stereo RX audio interleaves the two receivers. Which physical side
        (MAIN or SUB) lands on the left sample is a radio/transport property, so
        the operator selects it instead of the code assuming one.
        """
        if channels not in (1, 2) or len(packet) < 24:
            return b""
        pcm_length = int.from_bytes(packet[22:24], "big")
        pcm = packet[24:]
        if not pcm_length or pcm_length != len(pcm) or len(pcm) % (channels * 2):
            return b""
        if channels == 1:
            return pcm
        samples = _decode_pcm(pcm)
        left = samples[0::2]
        if selection == "right":
            return _encode_pcm(array("h", samples[1::2]))
        if selection == "both":
            right = samples[1::2]
            return _encode_pcm(array("h", ((a + b) // 2 for a, b in zip(left, right))))
        return _encode_pcm(array("h", left))

    @staticmethod
    def channel_peaks(packet: bytes, channels: int) -> tuple[int, int]:
        """Return the (left, right) peak magnitudes of one native audio packet."""
        if channels not in (1, 2) or len(packet) < 24:
            return 0, 0
        samples = _decode_pcm(packet[24:])
        if channels == 1:
            peak = _peak_of(samples)
            return peak, peak
        return _peak_of(samples[0::2]), _peak_of(samples[1::2])

    def _run(self) -> None:
        controller = None
        audio_queue = None
        sample_rate = None
        channels = 0
        selection = "main"
        connected = False
        try:
            while not self._stop.is_set():
                next_controller = self.get_controller()
                if next_controller is not controller:
                    if controller is not None and audio_queue is not None:
                        controller.unsubscribe_audio(audio_queue)
                    self._terminate_worker()
                    controller = next_controller
                    audio_queue = None
                    sample_rate = None
                    channels = 0
                    selection = "main"
                    connected = False
                    with self._lock:
                        self._last_input_at = None
                    if controller is not None:
                        sample_rate = int(controller.config.sample_rate)
                        channels = 2 if controller.config.rx_codec == "lpcm16_stereo" else 1
                        audio_queue = controller.subscribe_audio(self._wake.set)
                        selection = self._configured_channel()
                        with self._lock:
                            self._input_rate = sample_rate
                            self._input_channels = channels
                            self._input_channel = selection

                if controller is not None:
                    try:
                        snapshot_reader = getattr(controller, "try_snapshot", None)
                        radio = (
                            snapshot_reader()
                            if snapshot_reader is not None
                            else controller.snapshot()
                        )
                        if radio is not None:
                            connected = bool(radio.get("connected"))
                    except Exception:
                        pass
                packets = controller.read_audio(audio_queue) if connected and audio_queue is not None else []
                if packets:
                    if not self._ensure_worker(int(sample_rate)):
                        self._wake.wait(0.5)
                        self._wake.clear()
                        continue
                    wrote_samples = 0
                    for packet in packets:
                        left_peak, right_peak = self.channel_peaks(packet, channels)
                        if left_peak or right_peak:
                            with self._lock:
                                self._input_peak_left = max(left_peak, int(self._input_peak_left * 0.9))
                                self._input_peak_right = max(right_peak, int(self._input_peak_right * 0.9))
                        pcm = self.extract_main_pcm(packet, channels, selection)
                        if not pcm:
                            continue
                        pcm = self.apply_rx_gain(pcm)
                        peak = peak_of(pcm)
                        with self._lock:
                            self._decoder_peak = max(peak, int(self._decoder_peak * 0.9))
                        if self._write_pcm(pcm):
                            wrote_samples += len(pcm) // 2
                    with self._lock:
                        # Audio arrived from the radio regardless of whether the
                        # decoder accepted it; "waiting for radio audio" must
                        # mean only this.
                        self._last_input_at = monotonic()
                    if wrote_samples:
                        with self._lock:
                            self._input_samples += wrote_samples
                        if self._state == self.WAITING and not self._fatal_worker:
                            self._set_state(self.LISTENING)
                else:
                    with self._lock:
                        last_input_at = self._last_input_at
                    if (
                        not connected
                        or last_input_at is None
                        or monotonic() - last_input_at > _LISTENING_STALE_S
                    ) and not self._fatal_worker:
                        self._set_state(self.WAITING)
                    self._wake.wait(0.25)
                    self._wake.clear()
        except Exception as exc:
            LOGGER.exception("APRS audio consumer failed")
            self._set_failure(str(exc), fatal=True)
        finally:
            if controller is not None and audio_queue is not None:
                controller.unsubscribe_audio(audio_queue)
            self._terminate_worker()

    def _worker_path(self) -> Path | None:
        override = os.environ.get("PI_SAT_APRS_DIREWOLF")
        if override:
            candidate = Path(override).expanduser()
            if candidate.is_file():
                return candidate
        found = shutil.which("direwolf")
        return Path(found) if found else None

    def _write_config(self, sample_rate: int) -> Path | None:
        """Generate a receive-only Dire Wolf configuration for the stdin feed."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / "direwolf.conf"
        mycall = resolve_mycall(self._configured_mycall())
        lines = [
            "# Generated by Pi-Sat for the receive-only APRS module.",
            "# Feed PCM from stdin and disable audio output; Dire Wolf requires",
            "# both ADEVICE fields.",
            "ADEVICE stdin null",
            "ACHANNELS 1",
            f"ARATE {int(sample_rate)}",
            "CHANNEL 0",
            f"MYCALL {mycall}",
            "MODEM 1200",
            "AGWPORT 0",
            f"KISSPORT {self._kiss_port}",
        ]
        try:
            path.write_text("\n".join(lines) + "\n", encoding="ascii")
        except OSError as exc:
            self._set_failure(f"Unable to write the Dire Wolf configuration: {exc}", fatal=True)
            return None
        return path

    def _configured_mycall(self) -> str:
        """Read the shared APRS callsign, tolerating an absent configuration."""
        if self.get_config is None:
            return ""
        try:
            return self.get_config().aprs.mycall
        except Exception:
            LOGGER.debug("APRS callsign unavailable from configuration", exc_info=True)
            return ""

    def _configured_channel(self) -> str:
        """Read the decoder audio channel, tolerating an absent configuration."""
        if self.get_config is None:
            return "main"
        try:
            value = str(getattr(self.get_config().aprs, "channel", "")).strip().lower()
        except Exception:
            LOGGER.debug("APRS channel unavailable from configuration", exc_info=True)
            return "main"
        return value if value in {"main", "right", "both"} else "main"

    def _ensure_worker(self, sample_rate: int) -> bool:
        with self._process_lock:
            if self._process is not None and self._process.poll() is None:
                return True
            path = self._worker_path()
            if path is None:
                self._set_failure(
                    "Dire Wolf is not installed; install the direwolf package or set PI_SAT_APRS_DIREWOLF",
                    fatal=True,
                )
                return False
            config_path = self._write_config(sample_rate)
            if config_path is None:
                return False
            try:
                process = subprocess.Popen(
                    self._worker_command(path, config_path),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                    shell=False,
                )
            except OSError as exc:
                self._set_failure(f"Unable to start Dire Wolf: {exc}", fatal=True)
                return False
            self._process = process
            self._process_generation += 1
            generation = self._process_generation
        Thread(
            target=self._read_worker_output,
            args=(process,),
            name="aprs-decoder-log",
            daemon=True,
        ).start()
        reader = Thread(
            target=self._read_kiss_packets,
            args=(generation,),
            name="aprs-kiss",
            daemon=True,
        )
        self._reader_thread = reader
        reader.start()
        with self._lock:
            self._fatal_worker = False
        return True

    @staticmethod
    def _worker_command(path: Path, config_path: Path) -> list[str]:
        """Build the Dire Wolf command line.

        The configuration file names both audio halves itself
        (``ADEVICE stdin null``), so no positional source argument is needed.
        """
        return [str(path), "-c", str(config_path)]

    def _write_pcm(self, pcm: bytes) -> bool:
        with self._process_lock:
            process = self._process
            if process is None or process.stdin is None:
                return False
            if process.poll() is not None:
                # Report an exited decoder immediately instead of treating
                # failed writes as missing radio audio.
                self._set_failure(
                    f"Dire Wolf exited with status {process.returncode}", fatal=True
                )
                return False
            try:
                process.stdin.write(pcm)
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError) as exc:
                self._set_failure(f"Dire Wolf audio input failed: {exc}", fatal=True)
                return False

    def _terminate_worker(self) -> None:
        with self._process_lock:
            process = self._process
            self._process = None
            self._process_generation += 1
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)

    def _read_worker_output(self, process: subprocess.Popen[bytes]) -> None:
        """Keep Dire Wolf's banner, decode reports and errors for the operator."""
        stream = process.stdout
        if stream is None:
            return
        # Read to EOF even after a restart: a decoder that dies immediately
        # must still leave its reason in the log.
        for raw_line in iter(stream.readline, b""):
            message = raw_line.decode("utf-8", errors="replace").strip()
            if not message:
                continue
            with self._telemetry_lock:
                self._decoder_output.append(message)
            if any(marker in message.lower() for marker in _DECODER_ERROR_MARKERS):
                LOGGER.warning("direwolf: %s", message)
            else:
                LOGGER.debug("direwolf: %s", message)

    def _read_kiss_packets(self, generation: int) -> None:
        decoder = KissDecoder()
        while not self._stop.is_set():
            with self._process_lock:
                if generation != self._process_generation:
                    return
            try:
                with self._telemetry_lock:
                    self._kiss_attempts += 1
                with socket.create_connection(
                    ("127.0.0.1", self._kiss_port), timeout=_KISS_CONNECT_TIMEOUT_S
                ) as connection:
                    with self._telemetry_lock:
                        self._kiss_connected = True
                    self._consume_kiss(connection, decoder, generation)
            except OSError as exc:
                with self._telemetry_lock:
                    self._kiss_connected = False
                    self._kiss_failures += 1
                if self._stop.is_set():
                    return
                LOGGER.debug("APRS KISS connection unavailable: %s", exc)
                sleep(_KISS_RETRY_DELAY_S)
                continue
            finally:
                with self._telemetry_lock:
                    self._kiss_connected = False
            if self._stop.is_set():
                return
            sleep(_KISS_RETRY_DELAY_S)

    def _consume_kiss(self, connection: socket.socket, decoder: KissDecoder, generation: int) -> None:
        while not self._stop.is_set():
            with self._process_lock:
                if generation != self._process_generation:
                    return
            try:
                data = connection.recv(4096)
            except socket.timeout:
                # A quiet channel is not a disconnect; keep KISS connected to
                # avoid packet-loss gaps.
                continue
            except OSError:
                return
            if not data:
                return
            for payload in decoder.feed(data):
                frame = decode_ax25(payload)
                if frame is None:
                    continue
                try:
                    entry = describe(frame)
                except Exception as exc:
                    LOGGER.warning("Ignored undecodable APRS packet: %s", exc)
                    continue
                if entry is not None:
                    self._record(entry)

    def _record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            if not self._enabled:
                return
        self.log.append(entry)
        with self._lock:
            self._packet_count += 1
        self._set_state(self.LISTENING)
        self._publish({"type": "packet", "packet": entry})

    def _set_failure(self, message: str, *, fatal: bool) -> None:
        with self._lock:
            if not self._enabled:
                return
            if self._state == self.FAILED and self._error == message:
                return
            self._state = self.FAILED
            self._error = message
            self._fatal_worker = fatal
        self._publish_status()

    def _set_state(self, state: str) -> None:
        with self._lock:
            if not self._enabled or (self._state == state and self._error is None):
                return
            self._state = state
            self._error = None
        self._publish_status()

    def _publish_status(self) -> None:
        self._publish({"type": "status", "status": self.snapshot()})

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            event = {**event, "sequence": self._sequence}
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except Full:
                try:
                    subscriber.get_nowait()
                except Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except Full:
                    pass


def _kiss_port_from_environment() -> int:
    raw = os.environ.get("PI_SAT_APRS_KISS_PORT", "")
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_KISS_PORT
    return port if 0 < port < 65536 else _DEFAULT_KISS_PORT
