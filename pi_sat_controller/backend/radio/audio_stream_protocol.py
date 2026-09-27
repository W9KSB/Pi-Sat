"""Wire format for the passive RX audio stream.

Pi-Sat already fans every consumer of the radio's receive audio out of one
native PCM path. This module defines the second, narrower format used to carry
that audio to an out-of-process listener over TCP: one connection preamble that
describes the PCM, then a continuous sequence of length-prefixed audio frames.

The preamble is what makes the stream self-describing. Pi-Sat's native receive
format is configuration-driven, so a client that assumed 16 kHz stereo would
misread the samples on a radio configured for something else. The connector
reads the preamble instead of assuming a format.

Samples are always signed 16-bit little-endian at the radio's configured rate.
The stream carries the SUB/RX side as mono, so the payload is one sample per
frame regardless of how many channels the radio delivers.
"""

from __future__ import annotations

import struct
import sys
from array import array

STREAM_MAGIC = b"PISATAUD"
PROTOCOL_VERSION = 1

# Payload encoding identifiers. Only S16LE exists today; the field is present so
# a future format can be added without breaking an older connector's parse.
FORMAT_S16LE = 1

FRAME_TYPE_AUDIO = 1

# magic, version, header size, sample rate, channels, sample format, reserved.
PREAMBLE_STRUCT = struct.Struct("<8sHHIBB6x")
PREAMBLE_SIZE = PREAMBLE_STRUCT.size

# frame type, flags, sample count, sequence, payload bytes.
FRAME_STRUCT = struct.Struct("<BBHII")
FRAME_HEADER_SIZE = FRAME_STRUCT.size

# One native chunk is 20 ms; this only bounds a malformed peer.
MAX_PAYLOAD_BYTES = 65535


def build_preamble(sample_rate: int, channels: int, sample_format: int = FORMAT_S16LE) -> bytes:
    """Return the connection preamble for one stream format."""
    if sample_rate <= 0:
        raise ValueError("Sample rate must be positive")
    if channels not in (1, 2):
        raise ValueError("Channels must be 1 or 2")
    return PREAMBLE_STRUCT.pack(
        STREAM_MAGIC,
        PROTOCOL_VERSION,
        PREAMBLE_SIZE,
        int(sample_rate),
        int(channels),
        int(sample_format),
    )


def parse_preamble(data: bytes) -> dict[str, int]:
    """Validate and decode a connection preamble.

    Raises ``ValueError`` for a foreign stream, an unsupported protocol
    version, a truncated header, or an unknown sample format, so a connector
    reports a clear reason instead of playing noise.
    """
    if len(data) < PREAMBLE_SIZE:
        raise ValueError("Stream preamble is truncated")
    magic, version, header_size, sample_rate, channels, sample_format = PREAMBLE_STRUCT.unpack_from(data)
    if magic != STREAM_MAGIC:
        raise ValueError("Not a Pi-Sat audio stream")
    if version != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported stream protocol version {version}")
    if header_size != PREAMBLE_SIZE:
        raise ValueError(f"Unsupported stream header size {header_size}")
    if sample_rate <= 0:
        raise ValueError("Stream sample rate is invalid")
    if channels not in (1, 2):
        raise ValueError("Stream channel count is invalid")
    if sample_format != FORMAT_S16LE:
        raise ValueError(f"Unsupported stream sample format {sample_format}")
    return {
        "version": version,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_format": sample_format,
    }


def build_audio_frame(sequence: int, sample_count: int, payload: bytes) -> bytes:
    """Return one framed audio message.

    The sequence is a monotonic per-server counter. A client that sees it run
    out of order can report the gap, which is what makes "no intentional drop"
    checkable from the far end.
    """
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("Audio frame payload is too large")
    header = FRAME_STRUCT.pack(
        FRAME_TYPE_AUDIO,
        0,
        int(sample_count) & 0xFFFF,
        int(sequence) & 0xFFFFFFFF,
        len(payload),
    )
    return header + payload


def parse_frame_header(data: bytes) -> dict[str, int]:
    """Decode a frame header. Raises ``ValueError`` when it is not audio."""
    if len(data) < FRAME_HEADER_SIZE:
        raise ValueError("Audio frame header is truncated")
    frame_type, flags, sample_count, sequence, payload_bytes = FRAME_STRUCT.unpack_from(data)
    if frame_type != FRAME_TYPE_AUDIO:
        raise ValueError(f"Unsupported stream frame type {frame_type}")
    if payload_bytes > MAX_PAYLOAD_BYTES:
        raise ValueError("Audio frame payload length is out of range")
    return {
        "flags": flags,
        "sample_count": sample_count,
        "sequence": sequence,
        "payload_bytes": payload_bytes,
    }


def _decode_pcm(pcm: bytes) -> "array":
    """Return one PCM block as native-order 16-bit samples."""
    usable = len(pcm) - (len(pcm) % 2)
    samples = array("h")
    samples.frombytes(pcm[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _encode_s16le(samples: "array") -> bytes:
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def extract_mono_pcm(packet: bytes, channels: int, channel: str = "right") -> bytes:
    """Return the SUB/RX side of one native audio packet as mono S16LE.

    The browser envelope wraps transport-neutral PCM: bytes 0-3 carry the total
    length, 22-23 the PCM length, and the samples follow at byte 24. Icom dual
    audio puts MAIN on the left sample and SUB on the right, matching the
    console's "MAIN = TX / SUB = RX" labelling, so the receive side is the right
    sample by default. ``channel`` stays configurable because which physical
    side lands on which sample is a radio/transport property.

    Returns ``b""`` for a malformed or empty block rather than raising, because
    one bad chunk must not end a listening session.
    """
    if channels not in (1, 2) or len(packet) < 24:
        return b""
    pcm_length = int.from_bytes(packet[22:24], "big")
    pcm = packet[24:]
    if not pcm_length or pcm_length != len(pcm) or len(pcm) % (channels * 2):
        return b""
    if channels == 1:
        return _encode_s16le(_decode_pcm(pcm))
    samples = _decode_pcm(pcm)
    if channel == "left":
        return _encode_s16le(array("h", samples[0::2]))
    return _encode_s16le(array("h", samples[1::2]))


def extract_pcm(packet: bytes, channels: int, channel: str = "right") -> tuple[bytes, int]:
    """Return selected PCM and its channel count without changing samples.

    ``both`` preserves the native interleaved stereo payload.  Keeping this
    option at the stream boundary makes channel mapping observable without
    changing the radio session or the existing mono decoder default.
    """
    if channels not in (1, 2) or len(packet) < 24:
        return b"", 0
    pcm_length = int.from_bytes(packet[22:24], "big")
    pcm = packet[24:]
    if not pcm_length or pcm_length != len(pcm) or len(pcm) % (channels * 2):
        return b"", 0
    if channels == 1 or channel == "both":
        return pcm, channels
    return extract_mono_pcm(packet, channels, channel), 1
