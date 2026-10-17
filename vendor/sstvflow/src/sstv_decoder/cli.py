"""SSTVFlow: a streaming SSTV decoder with automatic reception."""
import argparse
import json
import os
import queue
import sys
import threading
from .decoder import Decoder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample_rate_hz", type=int)
    parser.add_argument("--control-fd", type=int, help="inherited read descriptor >=3, newline JSON")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--demodulator", choices=("hilbert", "quadrature", "narrow", "sharp"), default="quadrature",
                        help="quadrature selects width automatically; narrow/sharp force receive filtering")
    parser.add_argument("--pixel-estimator",choices=("adaptive","phase","sinefit"),default="adaptive")
    parser.add_argument("--noise-reduction",type=float,default=None,help="override automatic noise reduction with a manual blend, 0..1; zero disables it")
    args = parser.parse_args()
    if os.name == "nt":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    if args.control_fd is not None and args.control_fd < 3:
        parser.error("control descriptor must be >=3 and separate from stdin/stdout/stderr")

    def emit(event):
        sys.stdout.write(json.dumps(event, separators=(",", ":"), allow_nan=False)+"\n")
        sys.stdout.flush()

    diagnostic = (lambda event: print(json.dumps(event,allow_nan=False),file=sys.stderr,flush=True)) if args.diagnostics else None
    try:
        decoder = Decoder(args.sample_rate_hz, emit, args.demodulator, diagnostic,args.pixel_estimator)
        if args.noise_reduction is not None:
            decoder.control({"command":"noise_reduction","strength":args.noise_reduction})
        control_fd = os.dup(args.control_fd) if args.control_fd is not None else None
    except (ValueError, OSError) as exc:
        emit(dict(type="error", code="initialization_failed", message=str(exc)))
        return 2
    audio = queue.Queue(maxsize=16)
    controls = queue.Queue(maxsize=64)

    def pcm_reader():
        try:
            while True:
                data = os.read(sys.stdin.fileno(), 8192)
                audio.put(data)
                if not data:
                    return
        except OSError as exc:
            audio.put(exc)

    def control_reader():
        pending = bytearray()
        oversized = False
        try:
            while True:
                block = os.read(control_fd, 4096)
                if not block:
                    if pending:
                        controls.put(ValueError("unterminated control line at EOF"))
                    return
                for byte in block:
                    if byte == 10:
                        if oversized:
                            controls.put(ValueError("control line exceeds 65536 bytes"))
                        elif pending:
                            try:
                                controls.put(json.loads(pending))
                            except (ValueError, UnicodeError) as exc:
                                controls.put(ValueError(str(exc)))
                        pending.clear()
                        oversized = False
                    elif not oversized:
                        pending.append(byte)
                        if len(pending) > 65536:
                            pending.clear()
                            oversized = True
        except OSError as exc:
            controls.put(exc)
        finally:
            os.close(control_fd)

    threading.Thread(target=pcm_reader, daemon=True).start()
    if control_fd is not None:
        threading.Thread(target=control_reader, daemon=True).start()
    try:
        while True:
            for _ in range(64):
                try:
                    command = controls.get_nowait()
                except queue.Empty:
                    break
                try:
                    if isinstance(command,Exception):
                        raise command
                    decoder.control(command)
                except (ValueError,TypeError,OSError) as exc:
                    emit(dict(type="error",code="invalid_control",message=str(exc)))
            try:
                data = audio.get(timeout=.05)
            except queue.Empty:
                continue
            if isinstance(data,Exception):
                emit(dict(type="error",code="pcm_read_failed",message=str(data)))
                decoder.eof()
                return 1
            if not data:
                decoder.eof()
                return 0
            decoder.feed(data)
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        decoder.eof()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
