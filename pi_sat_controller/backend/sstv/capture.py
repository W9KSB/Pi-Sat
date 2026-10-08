from __future__ import annotations

import json
from pathlib import Path
from queue import Queue
from tempfile import NamedTemporaryFile
from threading import Event, Lock, RLock, Thread
from typing import Any, BinaryIO
import wave


class SstvAudioCapture:
    """Record received and decoder-input mono s16le PCM without blocking reception."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "sstv_audio_capture"
        self._lock = Lock()
        self._lifecycle = RLock()
        self._queue: Queue = Queue()
        self._thread: Thread | None = None
        self._stopped = Event()
        self._stopped.set()
        self._active = False
        self._started = False
        self._error: str | None = None
        self._rates = {"source": 0, "decoder": 0}
        self._samples = {"source": 0, "decoder": 0}

    def start(self) -> None:
        with self._lifecycle:
            self.close()
            with self._lock:
                self._queue = Queue()
                self._rates = {"source": 0, "decoder": 0}
                self._samples = {"source": 0, "decoder": 0}
                self._error = None
                self._started = False
            files: dict[str, BinaryIO] = {}
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                if self.root.is_symlink():
                    raise OSError("SSTV capture directory cannot be a symbolic link")
                for kind, name in (("source", "source.pcm"), ("decoder", "decoder.pcm"),
                                   ("metadata", "source.jsonl")):
                    path = self.root / name
                    if path.is_symlink():
                        raise OSError("SSTV capture file cannot be a symbolic link")
                    files[kind] = path.open("wb")
                with self._lock:
                    self._active = True
                    self._stopped.clear()
                    self._thread = Thread(target=self._run, args=(files,),
                                          name="sstv-audio-capture", daemon=True)
                    self._thread.start()
                    self._started = True
            except Exception as exc:
                with self._lock:
                    self._active = False
                    self._thread = None
                    self._stopped.set()
                for stream in files.values():
                    try:
                        stream.close()
                    except Exception:
                        pass
                self._set_error(exc)
                raise

    def append_source(self, pcm: bytes, sample_rate: int, metadata: dict) -> bool:
        return self._append("source", pcm, sample_rate, dict(metadata))

    def append_decoder(self, pcm: bytes, sample_rate: int) -> bool:
        return self._append("decoder", pcm, sample_rate, None)

    def _append(self, kind: str, pcm: bytes, sample_rate: int,
                metadata: dict | None) -> bool:
        with self._lock:
            if not self._active or self._error:
                return False
            if not isinstance(sample_rate, int) or sample_rate <= 0 or len(pcm) % 2:
                self._error = "SSTV capture requires positive-rate mono 16-bit PCM"
                return False
            if self._rates[kind] and self._rates[kind] != sample_rate:
                self._error = (
                    f"SSTV {kind} capture sample rate changed from "
                    f"{self._rates[kind]} to {sample_rate}; start a new capture session"
                )
                return False
            self._rates[kind] = sample_rate
            self._queue.put(("pcm", kind, bytes(pcm), sample_rate, metadata))
            return True

    def _set_error(self, error: Exception) -> None:
        with self._lock:
            if self._error is None:
                self._error = f"SSTV audio capture failed: {error}"

    @staticmethod
    def _write_all(stream: BinaryIO, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            count = stream.write(payload[offset:])
            if count is None or count <= 0:
                raise OSError("Capture write made no progress")
            offset += count

    def _run(self, files: dict[str, BinaryIO]) -> None:
        io_failed = False
        try:
            while True:
                task = self._queue.get()
                try:
                    if task[0] == "stop":
                        break
                    if task[0] == "barrier":
                        _, ready, result = task
                        try:
                            if not io_failed:
                                for stream in files.values():
                                    stream.flush()
                                result["sizes"] = {kind: stream.tell()
                                                   for kind, stream in files.items()}
                            with self._lock:
                                result["rates"] = dict(self._rates)
                                result["samples"] = dict(self._samples)
                        except Exception as exc:
                            io_failed = True
                            self._set_error(exc)
                        finally:
                            ready.set()
                        continue
                    if io_failed:
                        continue
                    _, kind, pcm, rate, metadata = task
                    if kind == "source":
                        with self._lock:
                            start_sample = self._samples[kind]
                        record = dict(metadata)
                        record.update(source_start_sample=start_sample,
                                      sample_count=len(pcm) // 2, sample_rate=rate)
                        self._write_all(files["metadata"],
                                        (json.dumps(record) + "\n").encode("utf-8"))
                    self._write_all(files[kind], pcm)
                    with self._lock:
                        self._samples[kind] += len(pcm) // 2
                except Exception as exc:
                    io_failed = True
                    self._set_error(exc)
                finally:
                    self._queue.task_done()
        finally:
            for stream in files.values():
                try:
                    stream.close()
                except Exception as exc:
                    self._set_error(exc)
            self._stopped.set()

    def _snapshot(self, timeout: float | None = None) -> dict[str, Any] | None:
        ready, result = Event(), {}
        with self._lock:
            if not self._started:
                return {"rates": dict(self._rates), "samples": dict(self._samples),
                        "sizes": {"source": 0, "decoder": 0, "metadata": 0},
                        "error": self._error}
            if self._active:
                self._queue.put(("barrier", ready, result))
                wait_stopped = False
            else:
                wait_stopped = True
        if wait_stopped:
            if not self._stopped.wait(timeout):
                return None
            with self._lock:
                result["rates"] = dict(self._rates)
                result["samples"] = dict(self._samples)
            result["sizes"] = {}
            for kind, name in (("source", "source.pcm"), ("decoder", "decoder.pcm"),
                               ("metadata", "source.jsonl")):
                path = self.root / name
                result["sizes"][kind] = path.stat().st_size if path.exists() else 0
        elif not ready.wait(timeout):
            return None
        with self._lock:
            result["error"] = self._error
        return result

    def flush(self, timeout: float | None = None) -> bool:
        with self._lifecycle:
            snapshot = self._snapshot(timeout)
            return snapshot is not None and not snapshot["error"]

    def close(self, timeout: float | None = None) -> bool:
        with self._lifecycle:
            with self._lock:
                if self._active:
                    self._active = False
                    self._queue.put(("stop",))
            if not self._stopped.wait(timeout):
                return False
            with self._lock:
                return self._error is None

    def export(self, kind: str) -> Path | None:
        if kind not in ("source", "decoder"):
            raise ValueError("Capture kind must be source or decoder")
        return self._export(kind)

    def export_metadata(self) -> Path | None:
        return self._export("metadata")

    def _export(self, kind: str) -> Path | None:
        with self._lifecycle:
            snapshot = self._snapshot()
            if snapshot["error"]:
                raise RuntimeError(snapshot["error"])
            size = snapshot["sizes"][kind]
            if not size:
                return None
            suffix = ".jsonl" if kind == "metadata" else ".wav"
            source = self.root / ("source.jsonl" if kind == "metadata" else f"{kind}.pcm")
            with NamedTemporaryFile(dir=self.root, prefix=f"{kind}-", suffix=suffix,
                                    delete=False) as temporary:
                output = Path(temporary.name)
                try:
                    with source.open("rb") as original:
                        if kind == "metadata":
                            self._copy_prefix(original, temporary, size)
                        else:
                            with wave.open(temporary, "wb") as recording:
                                recording.setnchannels(1)
                                recording.setsampwidth(2)
                                recording.setframerate(snapshot["rates"][kind])
                                recording.setnframes(size // 2)
                                self._copy_prefix(original, recording, size, wav=True)
                except Exception:
                    temporary.close()
                    output.unlink(missing_ok=True)
                    raise
            return output

    @staticmethod
    def _copy_prefix(source: BinaryIO, destination: Any, size: int,
                     wav: bool = False) -> None:
        remaining = size
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("Capture snapshot ended before its recorded length")
            if wav:
                destination.writeframesraw(chunk)
            else:
                SstvAudioCapture._write_all(destination, chunk)
            remaining -= len(chunk)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"error": self._error, "active": self._active,
                    "source_samples": self._samples["source"],
                    "decoder_samples": self._samples["decoder"],
                    "source_rate": self._rates["source"],
                    "decoder_rate": self._rates["decoder"],
                    "pending_tasks": self._queue.qsize()}
