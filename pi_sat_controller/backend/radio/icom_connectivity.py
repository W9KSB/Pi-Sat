from __future__ import annotations

"""Transport boundary for the native Icom radio controller.

Connectivity implementations move opaque control bytes and PCM. They do not
construct, parse, correlate, or make decisions about CI-V commands.
"""

from dataclasses import dataclass
from typing import Any, Protocol


class IcomError(RuntimeError):
    """Base error exposed by native Icom API and connectivity layers."""


class IcomConnectivityError(IcomError):
    """The selected physical connectivity path failed."""


@dataclass(frozen=True)
class IcomConnectivityRead:
    control_chunks: tuple[bytes, ...] = ()
    audio_chunks: tuple[bytes, ...] = ()


class IcomConnectivity(Protocol):
    kind: str

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def poll(self, timeout_s: float = 0.0) -> IcomConnectivityRead: ...

    def write_control(self, payload: bytes) -> None: ...

    def write_audio(self, pcm: bytes) -> int: ...

    def flush_audio(self) -> int: ...

    def discard_audio(self) -> int: ...

    def snapshot(self) -> dict[str, Any]: ...
