"""Ordered PCM and control transport for the isolated SSTVFlow process."""
from __future__ import annotations

import json
import os
import sys
from typing import BinaryIO, Any

MAX_FRAME_BYTES = 1024 * 1024


def _frame(kind: bytes, payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("SSTV input frame exceeds its size limit")
    return kind + len(payload).to_bytes(4, "big") + payload


def frame_pcm(pcm: bytes) -> bytes:
    return _frame(b"P", pcm)


def frame_control(command: dict[str, Any]) -> bytes:
    return _frame(b"C", json.dumps(command, allow_nan=False).encode("utf-8"))


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            raise ValueError("Truncated SSTV input frame")
        data.extend(block)
    return bytes(data)


def main() -> int:
    def emit(event: dict[str, Any]) -> None:
        print(json.dumps(event, separators=(",", ":"), allow_nan=False), flush=True)

    try:
        from sstv_decoder.decoder import Decoder

        if os.name == "nt":
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        decoder = Decoder(int(sys.argv[1]), emit)
        stream = sys.stdin.buffer
        while True:
            kind = stream.read(1)
            if not kind:
                decoder.eof()
                return 0
            if kind not in (b"P", b"C"):
                raise ValueError("Unknown SSTV input frame type")
            size = int.from_bytes(_read_exact(stream, 4), "big")
            if size > MAX_FRAME_BYTES:
                raise ValueError("SSTV input frame exceeds its size limit")
            payload = _read_exact(stream, size)
            if kind == b"P":
                decoder.feed(payload)
            else:
                decoder.control(json.loads(payload))
    except BrokenPipeError:
        return 1
    except Exception as exc:
        print(f"SSTVFlow worker failed: {exc}", file=sys.stderr, flush=True)
        emit({"type": "error", "code": "worker_failed", "message": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
