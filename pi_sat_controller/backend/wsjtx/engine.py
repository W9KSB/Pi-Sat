from __future__ import annotations

import configparser
import logging
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .audio_rx import WsjtxRxAudioBridge
from .audio_tx import WsjtxTxAudioBridge
from .control import WsjtxControl, WsjtxControlCancelled
from .rx_doppler import WsjtxRxDoppler
from .spectrum import WsjtxAudioSpectrum

LOGGER = logging.getLogger(__name__)

_DECODE_DEPTH_VALUES = {"fast": 1, "normal": 2, "deep": 3}


class WsjtxEngine:
    """Own an isolated stock WSJT-X process fed from Pi-Sat receive audio."""

    SINK_NAME = "pi_sat_wsjtx_rx"
    TX_SINK_NAME = "pi_sat_wsjtx_tx"
    RIG_NAME = "PiSat-RX"

    def __init__(
        self,
        *,
        project_root: Path,
        get_settings: Callable[[], dict[str, Any]],
        get_controller: Callable[[], Any],
        get_block_reason: Callable[[], str],
        get_manager: Callable[[], Any] | None = None,
        get_rx_tracking_manager: Callable[[], Any] | None = None,
    ) -> None:
        self._root = Path(project_root) / "data" / "wsjtx" / "engine"
        self._get_settings = get_settings
        self._get_block_reason = get_block_reason
        self._get_controller = get_controller
        self._lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._tx_operation_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._display_process: subprocess.Popen | None = None
        self._bus_process: subprocess.Popen | None = None
        self._display = ""
        self._module_id: str | None = None
        self._tx_module_id: str | None = None
        self._pactl = ""
        self._pacat = ""
        self._log_file = None
        self._environment: dict[str, str] | None = None
        self._pulse_detail = ""
        self._started_at = 0.0
        self._generation = 0
        self._mode = "FT8"
        self._decode_depth = "fast"
        self._pending_tx: tuple[int, str, int, bool] | None = None
        self._tx_epoch = 0
        self._last_radio = None
        self._last_radio_state: dict = {}
        self._last_radio_seen = 0.0
        self._control = WsjtxControl(get_manager=get_manager or (lambda: None),
                                    get_display=lambda: self._display,
                                    get_pid=lambda: self._process.pid if self._process is not None else None,
                                    get_started_at=lambda: self._started_at,
                                    get_environment=self._playback_environment)
        self.spectrum = WsjtxAudioSpectrum(get_controller=get_controller)
        self._rx_doppler = WsjtxRxDoppler(
            get_controller=get_controller,
            get_tracking_manager=get_rx_tracking_manager,
        )
        self._state = "stopped"
        self._detail = "Managed WSJT-X receive engine is stopped."
        self._monitor_stop = threading.Event()
        self._audio = WsjtxRxAudioBridge(
            get_controller=get_controller,
            get_block_reason=get_block_reason,
            playback_command=self._playback_command,
            get_playback_environment=self._playback_environment,
            on_pcm=self.spectrum.feed,
            process_pcm=self._rx_doppler.process_pcm,
            on_batch_end=self._rx_doppler.finish_batch,
            channel="right",
        )
        self._tx_audio = WsjtxTxAudioBridge(
            get_controller=get_controller, get_status=self._control.status,
            get_block_reason=get_block_reason, capture_command=self._capture_command,
            get_environment=self._playback_environment,
            request_status=self._control.request_status,
        )

    def _capture_command(self, sample_rate: int) -> list[str]:
        return [self._pacat, "--record", "--raw", "--format=s16le", "--channels=1",
                "--latency-msec=20", "--process-time-msec=20", f"--rate={sample_rate}",
                f"--device={self.TX_SINK_NAME}.monitor"]

    def _tx_available(self) -> bool:
        radio = self._get_controller()
        state = radio.try_snapshot() if radio is not None else None
        if isinstance(state, dict):
            self._last_radio, self._last_radio_state = radio, state
            self._last_radio_seen = time.monotonic()
        elif radio is self._last_radio and time.monotonic() - self._last_radio_seen < 2:
            state = self._last_radio_state
        return bool(self._state == "running" and self._control.available()
                    and isinstance(state, dict) and state.get("connected")
                    and callable(getattr(radio, "claim_transmit_stream", None))
                    and self._audio.snapshot().get("state") == "running"
                    and self._tx_audio.snapshot().get("capture_running")
                    and self._tx_audio.snapshot().get("state") != "error")

    def configure_tx(self, message: str, offset_hz: int, tx_even: bool) -> None:
        with self._tx_operation_lock:
            with self._state_lock:
                self._tx_epoch += 1
                epoch = self._tx_epoch
                self._pending_tx = None
            self._configure_tx_epoch(epoch, message, offset_hz, tx_even)

    def set_tx_enabled(
        self, enabled: bool, *, cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if not enabled:
            self.halt_tx()
            return
        with self._tx_operation_lock:
            with self._state_lock:
                epoch = self._tx_epoch
            superseded = lambda: (
                not self._tx_epoch_is_current(epoch)
                or (cancelled is not None and cancelled())
            )
            if superseded():
                raise WsjtxControlCancelled("Transmit request was superseded")
            if not self._tx_available():
                raise RuntimeError("Managed native transmit is unavailable.")
            try:
                if superseded():
                    raise WsjtxControlCancelled("Transmit request was superseded")
                # Halt cannot acquire this operation lock, so cleanup of any
                # claim made here remains bound to this serialized request.
                self._tx_audio.set_enabled(True)
                if superseded():
                    raise WsjtxControlCancelled("Transmit request was superseded")
                self._arm_stock(epoch, cancelled)
            except Exception:
                self._tx_audio.set_enabled(False)
                raise

    def _tx_epoch_is_current(self, epoch: int) -> bool:
        with self._state_lock:
            return self._tx_epoch == epoch

    def _check_tx_epoch(self, epoch: int) -> None:
        if not self._tx_epoch_is_current(epoch):
            raise WsjtxControlCancelled("Transmit request was superseded")

    def _configure_tx_epoch(
        self, epoch: int, message: str, offset_hz: int, tx_even: bool,
    ) -> None:
        self._check_tx_epoch(epoch)
        if not self._tx_available():
            raise RuntimeError("Connect the native radio and start managed RX before enabling TX.")
        if self._tx_audio.snapshot()["transmitting"] or self._control.status().get("transmitting"):
            with self._state_lock:
                if self._tx_epoch == epoch:
                    self._pending_tx = (epoch, message, offset_hz, tx_even)
            return
        restore_enabled = self._control.status().get("tx_enabled") is True
        cancelled = lambda: not self._tx_epoch_is_current(epoch)
        self._control.configure(message, offset_hz, tx_even, cancelled=cancelled)
        self._check_tx_epoch(epoch)
        self._tx_audio.prepare(message)
        self._check_tx_epoch(epoch)
        if restore_enabled and self._tx_audio.snapshot()["enabled"]:
            self._arm_stock(epoch)
        with self._state_lock:
            if self._pending_tx is not None and self._pending_tx[0] == epoch:
                self._pending_tx = None

    def _arm_stock(
        self, epoch: int | None = None, cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if epoch is None:
            with self._state_lock:
                epoch = self._tx_epoch
        superseded = lambda: (
            not self._tx_epoch_is_current(epoch)
            or (cancelled is not None and cancelled())
        )
        if superseded():
            raise WsjtxControlCancelled("Transmit request was superseded")
        self._control.set_enabled(True, cancelled=superseded)
        if superseded():
            raise WsjtxControlCancelled("Transmit request was superseded")

    def halt_tx(self) -> None:
        # Invalidate work first. A slow native helper that completes later must
        # never re-arm the station after this Halt request.
        with self._state_lock:
            self._tx_epoch += 1
            self._pending_tx = None
        problem = None
        try:
            self._tx_audio.set_enabled(False)
        except Exception as error:
            problem = error
        try:
            if self._control.status().get("connected"):
                self._control.halt()
        except Exception as error:
            problem = problem or error
        if problem is not None:
            raise RuntimeError(str(problem)) from problem

    def _playback_command(self, sample_rate: int) -> list[str]:
        return [
            self._pacat, "--playback", "--raw", "--format=s16le", "--channels=1",
            "--latency-msec=100", "--process-time-msec=20",
            f"--rate={sample_rate}", f"--device={self.SINK_NAME}",
        ]

    def _playback_environment(self) -> dict[str, str]:
        return self._environment.copy() if self._environment is not None else os.environ.copy()

    @staticmethod
    def _pulse_environment() -> tuple[dict[str, str], str]:
        import pwd

        environment = os.environ.copy()
        uid = os.getuid()
        try:
            username = pwd.getpwuid(uid).pw_name
        except KeyError:
            username = str(uid)
        default_runtime = Path(f"/run/user/{uid}")
        runtime = environment.get("XDG_RUNTIME_DIR") or str(default_runtime)
        detail = f"PulseAudio user {username} (UID {uid}), runtime {runtime}"
        if environment.get("PULSE_SERVER"):
            return environment, detail
        candidates = []
        if environment.get("XDG_RUNTIME_DIR"):
            candidates.append(Path(environment["XDG_RUNTIME_DIR"]))
        if default_runtime not in candidates:
            candidates.append(default_runtime)
        for candidate in candidates:
            if not candidate.is_absolute():
                continue
            endpoint = candidate / "pulse" / "native"
            try:
                directory = candidate.stat()
                server = endpoint.stat()
            except OSError:
                continue
            if (stat.S_ISDIR(directory.st_mode) and directory.st_uid == uid
                    and stat.S_IMODE(directory.st_mode) == 0o700
                    and stat.S_ISSOCK(server.st_mode) and server.st_uid == uid):
                environment["PULSE_SERVER"] = f"unix:{endpoint}"
                return environment, f"PulseAudio user {username} (UID {uid}), runtime {candidate}, socket {endpoint}"
        checked = ", ".join(str(candidate / "pulse" / "native") for candidate in candidates)
        raise RuntimeError(f"No same-user PulseAudio socket is available for {username} (UID {uid}); checked {checked}. Start the user's PulseAudio service or configure PULSE_SERVER.")

    @staticmethod
    def _executable(value: str) -> str:
        resolved = shutil.which(value)
        if not resolved:
            raise ValueError(f"Required executable is unavailable: {value}")
        return resolved

    def _command(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=5, check=False, env=self._environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"{Path(args[0]).name} failed: {error}; {self._pulse_detail}") from error
        if result.returncode:
            reason = result.stderr.strip()[:300] or f"exit code {result.returncode}"
            raise RuntimeError(f"{Path(args[0]).name} failed: {reason}; {self._pulse_detail}")
        return result.stdout.strip()

    def _start_bus(self) -> None:
        daemon = self._executable("dbus-daemon")
        runtime = self._root / "runtime"
        runtime.mkdir(mode=0o700, exist_ok=True)
        runtime.chmod(0o700)
        self._environment["XDG_RUNTIME_DIR"] = str(runtime)
        self._bus_process = subprocess.Popen([daemon, "--session", "--nofork", "--print-address=1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self._log_file,
            env=self._environment, start_new_session=True)
        assert self._bus_process.stdout is not None
        deadline = time.monotonic() + 5
        address = bytearray()
        while time.monotonic() < deadline and b"\n" not in address:
            ready, _, _ = select.select([self._bus_process.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(self._bus_process.stdout.fileno(), 512)
                if not chunk or len(address) + len(chunk) > 1024:
                    raise RuntimeError("Private WSJT-X session bus did not start")
                address.extend(chunk)
        value = bytes(address).split(b"\n", 1)[0].decode("ascii")
        if not value.startswith(("unix:path=", "unix:abstract=")) or b"\n" not in address:
            raise RuntimeError("Private WSJT-X session bus did not return a local address")
        self._environment["DBUS_SESSION_BUS_ADDRESS"] = value
        self._environment.pop("AT_SPI_BUS_ADDRESS", None)

    def _settings(self) -> tuple[str, str, str, int, str, str]:
        settings = self._get_settings()
        executable = settings.get("executable", "wsjtx")
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError("Configure the WSJT-X executable before starting the engine.")
        callsign = str(settings.get("callsign", "")).strip().upper()
        grid = str(settings.get("grid_locator", "")).strip().upper()
        if not re.fullmatch(r"[A-Z0-9/]{1,32}", callsign):
            raise ValueError("Configure a valid station callsign before starting WSJT-X.")
        if not re.fullmatch(r"[A-R]{2}[0-9]{2}(?:[A-X]{2}(?:[0-9]{2})?)?", grid):
            raise ValueError("Configure a valid station grid locator before starting WSJT-X.")
        port = settings.get("udp_port", 2237)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Managed WSJT-X requires a UDP listener port from 1 to 65535.")
        if settings.get("udp_host", "127.0.0.1") not in ("127.0.0.1", "0.0.0.0", "localhost"):
            raise ValueError("Managed WSJT-X requires the UDP listener to bind 127.0.0.1 or 0.0.0.0.")
        mode = str(settings.get("mode", "FT8")).upper()
        if mode not in ("FT4", "FT8"):
            raise ValueError("Managed WSJT-X mode must be FT4 or FT8.")
        decode_depth = str(settings.get("decode_depth", "fast")).lower().strip()
        if decode_depth not in _DECODE_DEPTH_VALUES:
            raise ValueError("Managed WSJT-X decode depth must be fast, normal, or deep.")
        return self._executable(executable.strip()), callsign, grid, port, mode, decode_depth

    def _write_profile(self, callsign: str, grid: str, port: int, mode: str,
                       decode_depth: str = "fast") -> Path:
        if decode_depth not in _DECODE_DEPTH_VALUES:
            raise ValueError("Managed WSJT-X decode depth must be fast, normal, or deep.")
        config_dir = self._root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (self._root / "data").mkdir(parents=True, exist_ok=True)
        profile = config_dir / f"WSJT-X - {self.RIG_NAME}.ini"
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config["Configuration"] = {
            "Rig": "None", "MyCall": callsign, "MyGrid": grid,
            "UDPServer": "127.0.0.1", "UDPServerPort": str(port),
            "AcceptUDPRequests": "true", "PSKReporter": "false",
            "PSKReporterTCPIP": "false", "EQSLSend": "false",
            "SoundInName": f"{self.SINK_NAME}.monitor", "AudioInputChannel": "Mono",
            "SoundOutName": self.TX_SINK_NAME, "AudioOutputChannel": "Mono",
            "MonitorOFF": "false", "MonitorLastUsed": "false",
        }
        config["Common"] = {"AutoSeq": "false", "Mode": mode, "NDepth": str(_DECODE_DEPTH_VALUES[decode_depth]),
                            "HoldTxFreq": "true", "TxFreq": "1500"}
        # Stock decode bounds follow its Wide Graph, even on a private display.
        # Cover the supported audio range independently of Pi-Sat's filter span.
        config["WideGraph"] = {"StartFreq": "0", "BinsPerPixel": "8", "PlotWidth": "1000"}
        temporary = profile.with_suffix(".ini.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            config.write(stream, space_around_delimiters=False)
        os.replace(temporary, profile)
        return profile

    def _read_display(self, descriptor: int) -> str:
        deadline = time.monotonic() + 8
        data = bytearray()
        while time.monotonic() < deadline:
            if self._display_process.poll() is not None:
                raise RuntimeError("The private WSJT-X display exited during startup.")
            ready, _, _ = select.select([descriptor], [], [], min(0.2, deadline - time.monotonic()))
            if not ready:
                continue
            chunk = os.read(descriptor, 64)
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in data:
                line = bytes(data).split(b"\n", 1)[0].decode("ascii").strip()
                if line.isdecimal() and len(line) <= 5:
                    return f":{line}"
                break
            if len(data) > 64:
                break
        raise RuntimeError("The private WSJT-X display did not become ready.")

    def _start_display(self, executable: str) -> None:
        read_fd, write_fd = os.pipe()
        try:
            self._display_process = subprocess.Popen(
                [executable, "-displayfd", str(write_fd), "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                stdin=subprocess.DEVNULL, stdout=self._log_file, stderr=self._log_file,
                pass_fds=(write_fd,), start_new_session=True,
                env=self._environment,
            )
            os.close(write_fd)
            write_fd = -1
            self._display = self._read_display(read_fd)
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    @staticmethod
    def _terminate(process: subprocess.Popen | None) -> None:
        if process is None:
            return
        # Each owned process has its own session; this also stops its decoder helpers.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

    def _cleanup(self) -> list[str]:
        self._monitor_stop.set()
        failures = []
        try:
            self.halt_tx()
        except Exception as error:
            failures.append(f"Transmit halt failed: {error}")
        try:
            result = self._tx_audio.stop()
            # A retained claim means the transmit stream, and possibly PTT, was
            # never released; the operator must see that instead of "stopped".
            if isinstance(result, dict) and result.get("owns_transmit"):
                failures.append(f"Transmit cleanup failed: {result.get('detail') or 'the transmit stream is still claimed'}")
        except Exception as error:
            failures.append(f"Transmit cleanup failed: {error}")
        try:
            result = self._audio.stop()
            # The receive bridge reports a worker that outlived its stop as an
            # error snapshot rather than raising.
            if isinstance(result, dict) and (result.get("state") != "stopped" or result.get("playback_running")):
                failures.append(f"Receive audio cleanup failed: {result.get('detail') or 'the RX audio bridge did not stop'}")
        except Exception as error:
            failures.append(f"Audio cleanup failed: {error}")
        for name in ("_process", "_display_process", "_bus_process"):
            process = getattr(self, name)
            try:
                self._terminate(process)
                if process is not None and name == "_bus_process" and process.stdout is not None:
                    process.stdout.close()
                setattr(self, name, None)
            except Exception as error:
                failures.append(f"Process cleanup failed: {error}")
        for attribute, sink in (("_tx_module_id", self.TX_SINK_NAME), ("_module_id", self.SINK_NAME)):
            try:
                self._unload_sink(attribute, sink)
            except Exception as error:
                failures.append(f"Audio sink cleanup failed: {error}")
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
        self._display = ""
        self.spectrum.reset()
        self._rx_doppler.reset("Managed receive engine stopped")
        if failures:
            LOGGER.warning("Managed WSJT-X cleanup reported %d failure(s): %s", len(failures), "; ".join(failures))
        return failures

    @staticmethod
    def _module_entry(modules: str, module_id: str) -> str | None:
        for line in modules.splitlines():
            parts = line.split(None, 1)
            if parts and parts[0] == module_id:
                return line
        return None

    @staticmethod
    def _loads_sink(entry: str, sink_name: str) -> bool:
        """Report whether a module inventory line is the null sink we loaded."""
        try:
            fields = shlex.split(entry)
        except ValueError:
            return False
        return len(fields) > 1 and fields[1] == "module-null-sink" and f"sink_name={sink_name}" in fields

    def _unload_sink(self, attribute: str, sink_name: str) -> None:
        """Unload only a module still confirmed as the sink this engine loaded."""
        module_id = getattr(self, attribute)
        if module_id is None:
            return
        # Check ownership before unloading: a restarted PulseAudio server
        # reassigns module IDs, so an unverified ID may belong to another client.
        entry = self._module_entry(self._command([self._pactl, "list", "short", "modules"]), module_id)
        if entry is None:
            setattr(self, attribute, None)
            return
        if not self._loads_sink(entry, sink_name):
            # Never unload a module this engine cannot attribute. Fail when our
            # own sink is somehow still loaded under another module.
            sinks = self._command([self._pactl, "list", "short", "sinks"])
            loaded = [line.split() for line in sinks.splitlines()]
            if any(len(fields) > 1 and fields[1] == sink_name for fields in loaded):
                raise RuntimeError(f"{sink_name} is still loaded but module {module_id} is no longer a Pi-Sat WSJT-X sink")
            setattr(self, attribute, None)
            return
        try:
            self._command([self._pactl, "unload-module", module_id])
        except RuntimeError:
            # The module can disappear between the ownership check and the
            # unload. Only forget it once its absence is confirmed.
            if self._module_entry(self._command([self._pactl, "list", "short", "modules"]), module_id) is not None:
                raise
        setattr(self, attribute, None)

    def _snapshot_current(self) -> dict[str, Any]:
        with self._state_lock:
            process = self._process
            running = process is not None and process.poll() is None
            state, detail = self._state, self._detail
            display = self._display
            module_id, generation = self._module_id, self._generation
            mode = self._mode
            decode_depth = self._decode_depth
            pending = self._pending_tx is not None
        stock = self._control.status()
        tx = self._tx_audio.snapshot()
        return {
            "running": running, "state": state, "detail": detail,
            "pid": process.pid if running else None, "display": display,
            "profile": str(self._root / "config" / f"WSJT-X - {self.RIG_NAME}.ini"),
            "sink": self.SINK_NAME if module_id is not None else "",
            "decode_depth": decode_depth,
            "audio": {**self._audio.snapshot(), "doppler": self._rx_doppler.snapshot()},
            "tx": {**tx, "available": self._tx_available(), "connected": stock.get("connected", False),
                   "offset_hz": stock.get("tx_df_hz", 1500), "mode": mode,
                   "generation": generation, "client_key": stock.get("client_key", ""),
                   "stock_transmitting": stock.get("transmitting", False),
                   "configuration_pending": pending},
        }

    def snapshot(self) -> dict[str, Any]:
        return self._snapshot_current()

    def rx_audio_frequency_hz(self, offset_hz: int) -> int | None:
        if self._state != "running" or not self._audio.snapshot().get("running"):
            return None
        return self._rx_doppler.rx_audio_frequency_hz(offset_hz)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._snapshot_current()
            # Always reclaim outstanding work first: a previous stop can leave a
            # live audio worker with no module or process reference left behind.
            failures = self._cleanup()
            if failures:
                with self._state_lock:
                    self._state, self._detail = "error", "; ".join(failures)
                return self._snapshot_current()
            with self._state_lock:
                self._state, self._detail = "starting", "Preparing the managed WSJT-X receive engine."
            try:
                reason = self._get_block_reason()
                if reason:
                    raise RuntimeError(reason)
                executable, callsign, grid, port, mode, decode_depth = self._settings()
                self._mode, self._decode_depth = mode, decode_depth
                self._pactl = self._executable("pactl")
                self._pacat = self._executable("pacat")
                xvfb = self._executable("Xvfb")
                self._environment, self._pulse_detail = self._pulse_environment()
                sinks = self._command([self._pactl, "list", "short", "sinks"])
                if any(len(parts := line.split()) > 1 and parts[1] in (self.SINK_NAME, self.TX_SINK_NAME) for line in sinks.splitlines()):
                    raise RuntimeError("The Pi-Sat WSJT-X receive sink already exists; stop its owner before starting.")
                module = self._command([
                    self._pactl, "load-module", "module-null-sink", f"sink_name={self.SINK_NAME}",
                    "sink_properties=device.description=Pi-Sat_WSJT-X_RX", "rate=48000", "channels=1",
                ])
                if not module.isdecimal():
                    raise RuntimeError("PulseAudio did not return a receive sink module ID.")
                self._module_id = module
                tx_module = self._command([
                    self._pactl, "load-module", "module-null-sink", f"sink_name={self.TX_SINK_NAME}",
                    "sink_properties=device.description=Pi-Sat_WSJT-X_TX", "rate=48000", "channels=1",
                ])
                if not tx_module.isdecimal():
                    raise RuntimeError("PulseAudio did not return a transmit sink module ID.")
                self._tx_module_id = tx_module
                self._write_profile(callsign, grid, port, mode, decode_depth)
                self._log_file = (self._root / "engine.log").open("ab")
                self._start_bus()
                self._start_display(xvfb)
                env = self._environment.copy()
                env.update({
                    "DISPLAY": self._display, "XDG_CONFIG_HOME": str(self._root / "config"),
                    "XDG_DATA_HOME": str(self._root / "data"),
                    "PULSE_SOURCE": f"{self.SINK_NAME}.monitor", "PULSE_SINK": self.TX_SINK_NAME,
                    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1", "QT_ACCESSIBILITY": "1",
                    "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                })
                self._started_at = time.time()
                self._generation += 1
                self._process = subprocess.Popen(
                    [executable, f"--rig-name={self.RIG_NAME}"], stdin=subprocess.DEVNULL,
                    stdout=self._log_file, stderr=self._log_file, env=env,
                    cwd=self._root, start_new_session=True,
                )
                audio = self._audio.start()
                if audio.get("state") == "error":
                    raise RuntimeError(audio.get("detail") or "Receive audio bridge failed to start.")
                controller = self._get_controller()
                sample_rate = int(controller.config.sample_rate) if controller is not None else 48000
                self._tx_audio.start(sample_rate)
                self._state = "running"
                self._detail = "Stock WSJT-X is running in receive mode; waiting for native radio audio and UDP status."
                self._monitor_stop = threading.Event()
                threading.Thread(target=self._monitor, args=(self._monitor_stop,), name="wsjtx-engine", daemon=True).start()
            except Exception as error:
                failures = self._cleanup()
                with self._state_lock:
                    self._state = "error"
                    self._detail = "; ".join([str(error), *failures])
                LOGGER.warning("Managed WSJT-X start failed: %s", self._detail)
            return self._snapshot_current()

    def _monitor(self, stop: threading.Event) -> None:
        while not stop.wait(0.25):
            if stop.is_set():
                return
            with self._state_lock:
                process, display, bus = self._process, self._display_process, self._bus_process
                pending = self._pending_tx
            reason = ""
            if process is not None and process.poll() is not None:
                reason = f"WSJT-X exited with code {process.returncode}; inspect the engine log."
            elif display is not None and display.poll() is not None:
                reason = "The private WSJT-X display exited."
            elif bus is not None and bus.poll() is not None:
                reason = "The private WSJT-X session bus exited."
            else:
                audio = self._audio.snapshot()
                tx_audio = self._tx_audio.snapshot()
                if audio.get("state") == "error":
                    reason = audio.get("detail") or "Native receive audio bridge failed."
                elif tx_audio.get("state") == "error":
                    reason = tx_audio.get("detail") or "Native transmit audio bridge failed."
                elif pending is not None and not tx_audio["transmitting"] and not self._control.status().get("transmitting"):
                    with self._tx_operation_lock:
                        with self._state_lock:
                            current_pending = self._pending_tx
                            if current_pending is pending:
                                self._pending_tx = None
                        if current_pending is pending:
                            try:
                                self._configure_tx_epoch(*pending)
                            except WsjtxControlCancelled:
                                pass
                            except Exception as error:
                                reason = f"Transmit preparation failed: {error}"
            if reason:
                failures = self._cleanup()
                with self._state_lock:
                    self._state, self._detail = "error", "; ".join([reason, *failures])
                return

    def stop(self) -> dict[str, Any]:
        with self._lock:
            failures = self._cleanup()
            with self._state_lock:
                self._state = "error" if failures else "stopped"
                self._detail = "; ".join(failures) if failures else "Managed WSJT-X receive engine is stopped."
            return self._snapshot_current()
