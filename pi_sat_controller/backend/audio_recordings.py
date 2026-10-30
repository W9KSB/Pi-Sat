from __future__ import annotations

import array
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import sys
from threading import Event, Lock, RLock, Thread
from typing import Any, BinaryIO, Callable
from uuid import uuid4
import wave

LOGGER = logging.getLogger(__name__)

_RECORDING_ID = re.compile(r"^recording-\d{8}T\d{6}Z-[0-9a-f]{8}$")
_QUEUE_SECONDS = 30.0


class AudioRecordingManager:
    """Record the native SUB/RX audio fan-out independently of browser playback."""

    MAX_RECORDINGS = 10

    def __init__(self, data_dir: Path, get_controller: Callable[[], Any]) -> None:
        self.root = Path(data_dir)
        self.get_controller = get_controller
        self._lock = RLock()
        self._lifecycle = Lock()
        self._files = Lock()
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None
        self._recording = False
        self._started_at: datetime | None = None
        self._sample_rate = 0
        self._frames = 0
        self._last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            recording = self._recording
            duration = (
                self._frames / self._sample_rate
                if recording and self._sample_rate > 0
                else 0.0
            )
            return {
                "recording": recording,
                "started_at_utc": (
                    self._started_at.isoformat()
                    if recording and self._started_at is not None
                    else None
                ),
                "duration_seconds": round(duration, 3),
                "sample_rate": self._sample_rate if recording else None,
                "last_error": self._last_error,
            }

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        return self.start() if enabled else self.stop()

    def start(self) -> dict[str, Any]:
        with self._lifecycle:
            with self._lock:
                if self._recording:
                    return self.snapshot()

            controller = self.get_controller()
            if controller is None:
                raise ValueError("The native radio controller is unavailable")
            radio = self._radio_snapshot(controller)
            if radio is None or not radio.get("connected"):
                raise ValueError("Connect the radio before recording audio")
            if radio.get("audio_available") is False:
                raise ValueError("The radio receive audio path is unavailable")

            sample_rate = int(controller.config.sample_rate)
            if sample_rate <= 0:
                raise ValueError("The radio reported an invalid receive sample rate")
            channels = 2 if controller.config.rx_codec == "lpcm16_stereo" else 1
            self._ensure_root()

            now = datetime.now(timezone.utc)
            capture_id = uuid4().hex[:8]
            stem = f"recording-{now:%Y%m%dT%H%M%S}Z-{capture_id}"
            raw_path = self.root / f"{stem}.raw"
            output_path = self.root / f"{stem}.wav"
            raw_file: BinaryIO | None = None
            audio_queue = None
            try:
                raw_file = raw_path.open("xb")
                audio_queue = controller.subscribe_audio(
                    self._wake.set,
                    max_seconds=_QUEUE_SECONDS,
                    drop_oldest=True,
                )
                self._stop.clear()
                self._wake.clear()
                with self._lock:
                    self._recording = True
                    self._started_at = now
                    self._sample_rate = sample_rate
                    self._frames = 0
                    self._last_error = None
                    thread = Thread(
                        target=self._run,
                        args=(
                            controller,
                            audio_queue,
                            raw_file,
                            raw_path,
                            output_path,
                            sample_rate,
                            channels,
                        ),
                        name="audio-recording",
                        daemon=True,
                    )
                    self._thread = thread
                thread.start()
            except Exception:
                with self._lock:
                    self._recording = False
                    self._thread = None
                if audio_queue is not None:
                    try:
                        controller.unsubscribe_audio(audio_queue)
                    except Exception:
                        pass
                if raw_file is not None:
                    try:
                        raw_file.close()
                    except Exception:
                        pass
                    try:
                        raw_path.unlink(missing_ok=True)
                    except OSError:
                        LOGGER.warning(
                            "Could not remove an incomplete audio recording",
                            exc_info=True,
                        )
                raise

            LOGGER.info(
                "Audio recording started: %d Hz %s",
                sample_rate,
                "stereo SUB/RX right channel" if channels == 2 else "mono RX",
            )
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lifecycle:
            with self._lock:
                thread = self._thread
                if not self._recording or thread is None:
                    return self.snapshot()
                self._stop.set()
                self._wake.set()
            thread.join(timeout=10.0)
            if thread.is_alive():
                raise RuntimeError("Audio recording did not stop")
            return self.snapshot()

    def shutdown(self) -> None:
        try:
            self.stop()
        except Exception:
            LOGGER.exception("Could not stop audio recording during shutdown")

    def list(self) -> list[dict[str, Any]]:
        with self._files:
            return self._list_and_prune_locked()

    def recording_path(self, recording_id: str) -> Path | None:
        if not _RECORDING_ID.fullmatch(recording_id):
            return None
        with self._files:
            self._ensure_root()
            path = self.root / f"{recording_id}.wav"
            if path.is_symlink() or not path.is_file():
                return None
            return path

    def _run(
        self,
        controller: Any,
        audio_queue: Any,
        raw_file: BinaryIO,
        raw_path: Path,
        output_path: Path,
        sample_rate: int,
        channels: int,
    ) -> None:
        try:
            while not self._stop.is_set():
                if self.get_controller() is not controller:
                    break
                radio = self._radio_snapshot(controller)
                if radio is None or not radio.get("connected"):
                    break
                packets = controller.read_audio(audio_queue)
                if not packets:
                    self._wake.wait(0.25)
                    self._wake.clear()
                    continue
                for packet in packets:
                    pcm = self.extract_rx_pcm(packet, channels)
                    if not pcm:
                        continue
                    self._write_all(raw_file, pcm)
                    with self._lock:
                        self._frames += len(pcm) // 2
        except Exception as exc:
            with self._lock:
                self._last_error = f"Audio recording stopped: {exc}"
            LOGGER.exception("Audio recording consumer failed")
        finally:
            try:
                controller.unsubscribe_audio(audio_queue)
            except Exception:
                pass
            try:
                for packet in controller.read_audio(audio_queue):
                    pcm = self.extract_rx_pcm(packet, channels)
                    if not pcm:
                        continue
                    self._write_all(raw_file, pcm)
                    with self._lock:
                        self._frames += len(pcm) // 2
            except Exception as exc:
                with self._lock:
                    self._last_error = f"Audio recording stopped: {exc}"
                LOGGER.exception("Could not drain the final audio recording packets")
            try:
                raw_file.close()
            except Exception:
                LOGGER.exception("Could not close the audio recording buffer")

            with self._lock:
                frames = self._frames
                error = self._last_error
            if frames > 0:
                try:
                    self._finalize_wav(raw_path, output_path, sample_rate)
                    LOGGER.info(
                        "Audio recording saved: %s (%.1f seconds)",
                        output_path.name,
                        frames / sample_rate,
                    )
                except Exception as exc:
                    with self._lock:
                        self._last_error = f"Could not save audio recording: {exc}"
                    LOGGER.exception("Could not finalize audio recording")
            else:
                try:
                    raw_path.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning(
                        "Could not remove an empty audio recording",
                        exc_info=True,
                    )
                if error is None:
                    with self._lock:
                        self._last_error = None

            with self._lock:
                self._recording = False
                self._thread = None
                self._sample_rate = 0
            try:
                self.list()
            except Exception:
                LOGGER.exception("Could not enforce audio recording retention")

    @staticmethod
    def extract_rx_pcm(packet: bytes, channels: int) -> bytes:
        if channels not in (1, 2) or len(packet) < 24:
            return b""
        if int.from_bytes(packet[:4], "little") != len(packet):
            return b""
        pcm_length = int.from_bytes(packet[22:24], "big")
        pcm = packet[24:]
        if not pcm_length or pcm_length != len(pcm) or len(pcm) % (channels * 2):
            return b""
        if channels == 1:
            return pcm
        samples = array.array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        sub_samples = array.array("h", samples[1::2])
        if sys.byteorder != "little":
            sub_samples.byteswap()
        return sub_samples.tobytes()

    @staticmethod
    def _radio_snapshot(controller: Any) -> dict[str, Any] | None:
        try:
            snapshot_reader = getattr(controller, "try_snapshot", None)
            radio = (
                snapshot_reader()
                if snapshot_reader is not None
                else controller.snapshot()
            )
            return radio if isinstance(radio, dict) else None
        except Exception:
            return None

    @staticmethod
    def _write_all(stream: BinaryIO, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            count = stream.write(payload[offset:])
            if count is None or count <= 0:
                raise OSError("Audio recording write made no progress")
            offset += count

    @staticmethod
    def _finalize_wav(raw_path: Path, output_path: Path, sample_rate: int) -> None:
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            with raw_path.open("rb") as source:
                with wave.open(str(temporary), "wb") as recording:
                    recording.setnchannels(1)
                    recording.setsampwidth(2)
                    recording.setframerate(sample_rate)
                    while chunk := source.read(1024 * 1024):
                        recording.writeframesraw(chunk)
            os.replace(temporary, output_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Could not remove an audio recording temporary file", exc_info=True)
            try:
                raw_path.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Could not remove an audio recording buffer", exc_info=True)

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise OSError("Audio recordings directory is not a regular directory")

    def _list_and_prune_locked(self) -> list[dict[str, Any]]:
        self._ensure_root()
        recordings: list[tuple[tuple[int, str], dict[str, Any]]] = []
        for path in self.root.glob("recording-*.wav"):
            if path.is_symlink() or not path.is_file():
                continue
            if not _RECORDING_ID.fullmatch(path.stem):
                continue
            try:
                stat = path.stat()
                with wave.open(str(path), "rb") as recording:
                    sample_rate = recording.getframerate()
                    frames = recording.getnframes()
                    channels = recording.getnchannels()
                    sample_width = recording.getsampwidth()
                if sample_rate <= 0 or channels != 1 or sample_width != 2:
                    continue
                recordings.append(
                    (
                        (stat.st_mtime_ns, path.name),
                        {
                            "id": path.stem,
                            "filename": path.name,
                            "created_at_utc": datetime.fromtimestamp(
                                stat.st_mtime, tz=timezone.utc
                            ).isoformat(),
                            "duration_seconds": round(frames / sample_rate, 3),
                            "sample_rate": sample_rate,
                            "size_bytes": stat.st_size,
                        },
                    )
                )
            except (OSError, EOFError, wave.Error):
                continue
        recordings.sort(key=lambda item: item[0], reverse=True)
        for _, expired in recordings[self.MAX_RECORDINGS :]:
            try:
                (self.root / str(expired["filename"])).unlink(missing_ok=True)
            except OSError:
                LOGGER.warning(
                    "Could not remove expired audio recording %s",
                    expired["filename"],
                    exc_info=True,
                )
        return [item for _, item in recordings[: self.MAX_RECORDINGS]]
