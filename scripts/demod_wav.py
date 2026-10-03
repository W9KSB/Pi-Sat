"""Exercise the production FM demodulator on a PCM WAV recording.

Run from the repository root with ``python -m scripts.demod_wav INPUT OUTPUT``.
This adapter does not participate in the live radio or audio paths.
"""

import argparse
from pathlib import Path
import wave

import numpy as np

from pi_sat_controller.backend.radio.wide_fm import WideFmDemodulator


def demodulate_wav(
    input_path: Path,
    output_path: Path,
    *,
    block_size: int = 960,
    channel: int = 0,
    if_center_hz: float = 11_867.0,
    channel_bandwidth_hz: float = 21_000.0,
    output_cutoff_hz: float = 10_000.0,
    dc_cutoff_hz: float = 3.0,
    output_gain: float = 1.0 / 24_000.0,
) -> dict[str, int | float]:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("input and output paths must differ")
    if block_size <= 0 or channel < 0:
        raise ValueError("block size must be positive and channel must be nonnegative")

    with wave.open(str(input_path), "rb") as source:
        if source.getcomptype() != "NONE" or source.getsampwidth() != 2:
            raise ValueError("input must be uncompressed signed 16-bit PCM WAV")
        if channel >= source.getnchannels():
            raise ValueError(f"input has only {source.getnchannels()} channel(s)")

        demodulator = WideFmDemodulator(
            sample_rate=source.getframerate(),
            if_center_hz=if_center_hz,
            channel_bandwidth_hz=channel_bandwidth_hz,
            output_cutoff_hz=output_cutoff_hz,
            dc_cutoff_hz=dc_cutoff_hz,
            output_gain=output_gain,
        )
        with wave.open(str(output_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(source.getframerate())
            while raw := source.readframes(block_size):
                pcm = np.frombuffer(raw, dtype="<i2").reshape(-1, source.getnchannels())
                normalized = pcm[:, channel].astype(np.float32) / np.float32(32768.0)
                demodulated = demodulator.process(normalized)
                # Standard WAV PCM16 cannot represent values outside [-1, 1].
                encoded = np.rint(np.clip(demodulated, -1.0, 1.0) * 32767.0)
                output.writeframesraw(encoded.astype("<i2").tobytes())
    return demodulator.metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="real IF PCM16 WAV")
    parser.add_argument("output", type=Path, help="demodulated PCM16 WAV")
    parser.add_argument("--block-size", type=int, default=960)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--if-center-hz", type=float, default=11_867.0)
    parser.add_argument("--channel-bandwidth-hz", type=float, default=21_000.0)
    parser.add_argument("--output-cutoff-hz", type=float, default=10_000.0)
    parser.add_argument("--dc-cutoff-hz", type=float, default=3.0)
    parser.add_argument("--output-gain", type=float, default=1.0 / 24_000.0)
    args = parser.parse_args()
    try:
        metrics = demodulate_wav(
            args.input,
            args.output,
            block_size=args.block_size,
            channel=args.channel,
            if_center_hz=args.if_center_hz,
            channel_bandwidth_hz=args.channel_bandwidth_hz,
            output_cutoff_hz=args.output_cutoff_hz,
            dc_cutoff_hz=args.dc_cutoff_hz,
            output_gain=args.output_gain,
        )
    except (OSError, ValueError, wave.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(f"Saved: {args.output}")
    print(
        f"Samples: {metrics['samples_processed']}; "
        f"input RMS/peak: {metrics['input_rms']:.4f}/{metrics['input_peak']:.4f}; "
        f"output RMS/peak: {metrics['output_rms']:.4f}/{metrics['output_peak']:.4f}"
    )
    print(
        f"Input full-scale samples: {metrics['input_clipping_count']}; "
        f"output samples exceeding PCM16 range: {metrics['output_clipping_count']}; "
        f"mean discriminator: {metrics['mean_discriminator_hz']:.1f} Hz"
    )


if __name__ == "__main__":
    main()
