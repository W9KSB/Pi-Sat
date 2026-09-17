from __future__ import annotations

"""IC-9700 LAN connectivity.

This module owns only the proprietary UDP session, authentication, keepalive,
serial-channel encapsulation, and audio-channel packetization. CI-V bytes are
opaque here; radio commands and replies belong to the controller core.
"""

from dataclasses import dataclass
import logging
import random
import select
import socket
import struct
from threading import Event
from time import monotonic, sleep
from typing import Any

from pi_sat_controller.backend.radio.icom_connectivity import (
    IcomConnectivityError,
    IcomConnectivityRead,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class IcomLanConfig:
    enabled: bool = False
    host: str = ""
    username: str = ""
    password: str = ""
    control_port: int = 50001
    serial_port: int = 50002
    audio_port: int = 50003
    civ_address: int = 0xA2
    controller_address: int = 0xE0
    sample_rate: int = 16000
    rx_codec: str = "lpcm16_stereo"
    tx_codec: str = "lpcm16_mono"
    full_duplex: bool = True
    scope_enabled: bool = True
    debug_logging: bool = False


class IcomLanConnectivity:
    kind = "lan"

    _PASSCODE_SEQUENCE = bytes.fromhex(
        "475d4c42662023464e57453d677660416239592d687e7c657d4929727378216e5a5e4a3e712c2a54"
        "3c3a634f437527795b3570486b566f34326c30616d7b2f4b64382b2e50403f55333725772426746a"
        "28534d69225c443136583b7a515f52"
    )
    _AUTH_INNER_SEQ_START = 0
    _CLIENT_IDENTIFIER = b"Pi-Sat"
    _BOOTSTRAP_TIMEOUT_S = 3.0
    _BOOTSTRAP_RETRY_INTERVAL_S = 0.4
    _BOOTSTRAP_DUPLICATE_GAP_S = 0.04
    _DEAUTH_REPLY_TIMEOUT_S = 0.5
    _DEAUTH_ACK_PAUSE_S = 0.05
    # Radio-capability record inside the capabilities packet: 16-byte
    # guid/commoncap, 32-byte name, 32-byte audio name, then connection type,
    # CI-V address, RX and TX sample-rate counts.
    _CAPABILITY_RECORD_OFFSET = 66
    _CAPABILITY_AUDIO_OFFSET = 0x30
    _CAPABILITY_CONNTYPE_OFFSET = 0x50
    _CAPABILITY_CIV_OFFSET = 0x52
    _CAPABILITY_RX_RATE_OFFSET = 0x53
    _CAPABILITY_TX_RATE_OFFSET = 0x55
    _CAPABILITY_PACKET_SIZE = 168

    def __init__(self, config: IcomLanConfig, stop_event: Event | None = None) -> None:
        self.config = config
        self._stop = stop_event or Event()
        self._control: socket.socket | None = None
        self._serial: socket.socket | None = None
        self._audio: socket.socket | None = None
        self._stream_ids: dict[socket.socket, tuple[int, int]] = {}
        self._stream_sequences: dict[socket.socket, int] = {}
        self._ping_sequences: dict[socket.socket, int] = {}
        self._sent_packets: dict[socket.socket, dict[int, bytes]] = {}
        self._civ_seq = 1
        self._session_seq = 1
        self._auth_inner_seq = self._AUTH_INNER_SEQ_START
        self._local_sid = 0
        self._remote_sid = 0
        self._auth_id = b""
        self._capability_id = b""
        self._radio_name = b""
        self._audio_capability: dict[str, Any] | None = None
        self._serial_remote_port = config.serial_port
        self._audio_remote_port = config.audio_port
        self._last_auth_renewal = 0.0
        self._pending_renewal: tuple[bytes, float] | None = None
        self._last_keepalive = 0.0
        self._last_ping = 0.0
        # Embedded TX-audio counter: a separate, zero-based per-fragment stream
        # counter (not the transport sequence) that rolls over through 0.
        self._audio_send_seq = 0
        self._tx_pcm_buffer = bytearray()
        self._stage = "idle"
        self._connected = False
        self._authenticated = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "transport": self.kind,
            "host": self.config.host,
            "control_port": self.config.control_port,
            "connection_stage": self._stage,
            "transport_connected": self._connected,
            "transport_authenticated": self._authenticated,
            "audio_available": self._connected and self._audio is not None,
            "audio_capability": self._audio_capability,
        }

    def connect(self) -> None:
        if not self.config.host:
            raise IcomConnectivityError("Icom radio host is not configured")
        self._close_sockets()
        self._session_seq = 1
        self._auth_inner_seq = self._AUTH_INNER_SEQ_START
        self._civ_seq = 1
        self._audio_send_seq = 0
        self._last_keepalive = monotonic()
        self._last_ping = self._last_keepalive
        self._pending_renewal = None
        self._auth_id = b""
        self._capability_id = b""
        self._radio_name = b""
        self._serial_remote_port = self.config.serial_port
        self._audio_remote_port = self.config.audio_port
        try:
            self._stage = "control_bootstrap"
            self._control = self._new_socket(self.config.control_port)
            self._stream_ids[self._control] = (self._session_id_for_socket(self._control), 0)
            self._local_sid = self._stream_ids[self._control][0]
            self._serial = self._new_socket(self.config.serial_port)
            self._stream_ids[self._serial] = (self._session_id_for_socket(self._serial), 0)
            self._audio = self._new_socket(self.config.audio_port)
            self._stream_ids[self._audio] = (self._session_id_for_socket(self._audio), 0)
            self._bootstrap(self._control, self.config.control_port)
            self._stage = "authentication"
            self._authenticate()
            self._stage = "serial_bootstrap"
            self._bootstrap(self._serial, self._serial_remote_port)
            self._stage = "serial_open"
            self._open_serial()
            self._stage = "audio_bootstrap"
            self._bootstrap(self._audio, self._audio_remote_port)
            self._connected = True
            self._authenticated = True
            self._stage = "connected"
            self._log_audio_capability()
        except Exception:
            self._shutdown_session()
            self._close_sockets()
            raise

    def disconnect(self) -> None:
        self._shutdown_session()
        self._close_sockets()
        self._stage = "idle"

    def poll(self, timeout_s: float = 0.0) -> IcomConnectivityRead:
        readable = [sock for sock in (self._control, self._audio, self._serial) if sock is not None]
        ready = select.select(readable, [], [], max(0.0, timeout_s))[0] if readable else []
        control_chunks: list[bytes] = []
        audio_chunks: list[bytes] = []
        for _ in range(64):
            if not ready:
                break
            for sock in ready:
                try:
                    packet, _ = sock.recvfrom(65535)
                except (BlockingIOError, OSError):
                    continue
                port = self.config.control_port if sock is self._control else (
                    self._serial_remote_port if sock is self._serial else self._audio_remote_port
                )
                if self._handle_transport(sock, packet, port):
                    continue
                if sock is self._control:
                    if (self._pending_renewal and len(packet) == 64 and packet[20:22] == b"\x02\x05"
                            and packet[22:24] == self._pending_renewal[0]
                            and packet[26:32] == self._auth_id):
                        if packet[48:52] != b"\x00" * 4:
                            raise IcomConnectivityError("Icom LAN token renewal rejected")
                        self._pending_renewal = None
                        self._debug("token renewal accepted")
                elif sock is self._audio and len(packet) >= 24:
                    pcm_length = struct.unpack_from(">H", packet, 22)[0]
                    if pcm_length == 0 or pcm_length != len(packet) - 24:
                        self._debug("ignored malformed audio packet length=%d", len(packet))
                        continue
                    audio_chunks.append(packet[24:])
                elif sock is self._serial:
                    if len(packet) >= 22 and packet[16] == 0xC1:
                        length = struct.unpack_from("<H", packet, 17)[0]
                        if length == len(packet) - 21:
                            control_chunks.append(packet[21:])
            ready = select.select(readable, [], [], 0)[0] if readable else []
        now = monotonic()
        self._keepalive()
        if self._pending_renewal and now > self._pending_renewal[1]:
            raise IcomConnectivityError("Timed out waiting for Icom LAN token renewal")
        if self._authenticated and now - self._last_auth_renewal >= 60.0:
            request = self._send_auth(0x05)
            self._pending_renewal = (request[22:24], now + 5.0)
            self._last_auth_renewal = now
            self._debug("token renewal sent")
        return IcomConnectivityRead(tuple(control_chunks), tuple(audio_chunks))

    def write_control(self, payload: bytes) -> None:
        if self._serial is None or not self._connected:
            raise IcomConnectivityError("Icom LAN serial channel is unavailable")
        envelope = bytearray(self._header(
            0, length=21 + len(payload), seq=self._claim_session_seq(self._serial), sock=self._serial
        ))
        envelope.extend(b"\xc1")
        envelope.append(len(payload))
        envelope.append(0)
        envelope.extend(struct.pack(">H", self._civ_seq))
        envelope.extend(payload)
        self._civ_seq = (self._civ_seq + 1) & 0xFFFF or 1
        self._send_tracked(self._serial, bytes(envelope), self._serial_remote_port)

    def write_audio(self, pcm: bytes) -> int:
        if self._audio is None or not self._connected:
            raise IcomConnectivityError("Icom LAN audio channel is unavailable")
        self._tx_pcm_buffer.extend(pcm)
        packets = 0
        frame_bytes = self._tx_frame_bytes()
        while len(self._tx_pcm_buffer) >= frame_bytes:
            block = bytes(self._tx_pcm_buffer[:frame_bytes])
            del self._tx_pcm_buffer[:frame_bytes]
            packets += self._send_audio_frame(block)
        return packets

    def flush_audio(self) -> int:
        if not self._tx_pcm_buffer:
            return 0
        if self._audio is None or not self._connected:
            raise IcomConnectivityError("Icom LAN audio channel is unavailable")
        padding = max(0, self._tx_frame_bytes() - len(self._tx_pcm_buffer))
        block = bytes(self._tx_pcm_buffer) + bytes(padding)
        self._tx_pcm_buffer.clear()
        return self._send_audio_frame(block)

    def _tx_frame_bytes(self) -> int:
        """Bytes in one 20 ms mono LPCM16 frame at the negotiated rate.

        The radio's transmit audio is framed in 20 ms blocks (640 bytes at
        16 kHz, 1920 bytes at 48 kHz). Sending a fixed 1920-byte block made
        every frame three times too long at the configured 16 kHz rate.
        """
        return max(2, int(self.config.sample_rate / 50) * 2)

    def _send_audio_frame(self, frame: bytes) -> int:
        """Send one 20 ms frame, split at the 1364-byte UDP payload limit."""
        packets = 0
        for offset in range(0, len(frame), 1364):
            self._send_audio_part(frame[offset:offset + 1364])
            packets += 1
        return packets

    def discard_audio(self) -> int:
        """Drop buffered TX PCM that has not reached the radio yet.

        A transmission ends before a partial frame fills, and carrying that
        audio into the next transmission would prepend stale samples.
        """
        dropped = len(self._tx_pcm_buffer)
        self._tx_pcm_buffer.clear()
        return dropped

    def _debug(self, message: str, *args: object) -> None:
        if self.config.debug_logging:
            LOGGER.info("icom_lan_debug stage=%s " + message, self._stage, *args)

    @classmethod
    def _parse_audio_capability(cls, packet: bytes) -> dict[str, Any] | None:
        """Read the selected radio's advertised audio capability record.

        The record sits inside the IC-9700 LAN capability packet, which carries
        the interface name, CI-V address, connection type and the counts of
        offered receive and transmit sample rates. Those counts matter for TX:
        a radio that advertises fewer than two transmit rates does not offer
        usable transmit audio, so Pi-Sat reports the values instead of
        failing silently.
        """
        base = cls._CAPABILITY_RECORD_OFFSET
        if len(packet) < cls._CAPABILITY_PACKET_SIZE:
            return None
        audio = bytes(packet[base + cls._CAPABILITY_AUDIO_OFFSET:
                             base + cls._CAPABILITY_CONNTYPE_OFFSET]).split(b"\x00", 1)[0]
        return {
            "audio": audio.decode("ascii", "replace"),
            "civ_address": packet[base + cls._CAPABILITY_CIV_OFFSET],
            "connection_type": struct.unpack_from("<H", packet, base + cls._CAPABILITY_CONNTYPE_OFFSET)[0],
            "rx_rate_count": struct.unpack_from("<H", packet, base + cls._CAPABILITY_RX_RATE_OFFSET)[0],
            "tx_rate_count": struct.unpack_from("<H", packet, base + cls._CAPABILITY_TX_RATE_OFFSET)[0],
        }

    def _log_audio_capability(self) -> None:
        capability = self._audio_capability
        if capability is None:
            LOGGER.warning(
                "Icom LAN: the radio did not report an audio capability record; "
                "configured %d Hz %s TX audio is unverified",
                self.config.sample_rate, self.config.tx_codec)
            return
        LOGGER.info(
            "Icom LAN audio capability: interface=%s rx_rates=%d tx_rates=%d civ=0x%02X; "
            "configured %d Hz %s RX / %s TX",
            capability["audio"], capability["rx_rate_count"], capability["tx_rate_count"],
            capability["civ_address"], self.config.sample_rate, self.config.rx_codec,
            self.config.tx_codec)
        if capability["tx_rate_count"] < 2:
            LOGGER.warning(
                "Icom LAN: the radio advertises %d transmit audio rate(s); transmit audio "
                "may be unavailable below two. Verify transmit audio on this radio.",
                capability["tx_rate_count"])

    def _new_socket(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.1)
        sock.bind(("", 0))
        sock.connect((self.config.host, port))
        return sock

    @staticmethod
    def _session_id_for_socket(sock: socket.socket) -> int:
        local_ip, local_port = sock.getsockname()[:2]
        ip_value = struct.unpack(">I", socket.inet_aton(local_ip))[0]
        return ((ip_value << 16) | int(local_port)) & 0xFFFFFFFF

    def _bootstrap(self, sock: socket.socket, port: int) -> None:
        address = (self.config.host, port)
        probe = self._header(0x0003, seq=0, remote_sid=0, sock=sock)
        sock.sendto(probe, address)
        sleep(self._BOOTSTRAP_DUPLICATE_GAP_S)
        sock.sendto(probe, address)
        reply = self._wait_bootstrap_reply(sock, port, 0x04, 0, probe)
        remote_sid = struct.unpack(">I", reply[8:12])[0]
        local_sid, _ = self._stream_ids[sock]
        self._stream_ids[sock] = (local_sid, remote_sid)
        if sock is self._control:
            self._remote_sid = remote_sid
        ready = self._header(0x0006, seq=1, sock=sock)
        sock.sendto(ready, address)
        sleep(self._BOOTSTRAP_DUPLICATE_GAP_S)
        sock.sendto(ready, address)
        self._wait_bootstrap_reply(sock, port, 0x06, 1, ready)

    def _wait_bootstrap_reply(self, sock: socket.socket, port: int, packet_type: int,
                              sequence: int, retry_packet: bytes | None = None) -> bytes:
        started = monotonic()
        deadline = started + self._BOOTSTRAP_TIMEOUT_S
        next_retry = started + self._BOOTSTRAP_RETRY_INTERVAL_S
        local_sid, remote_sid = self._stream_ids[sock]
        expected_prefix = struct.pack("<IHH", 16, packet_type, sequence)
        while monotonic() < deadline:
            if self._stop.is_set():
                raise IcomConnectivityError("Icom connection stopped")
            try:
                reply, _ = sock.recvfrom(2048)
            except socket.timeout:
                now = monotonic()
                if retry_packet is not None and now >= next_retry:
                    sock.sendto(retry_packet, (self.config.host, port))
                    next_retry = now + self._BOOTSTRAP_RETRY_INTERVAL_S
                continue
            if (len(reply) == 16 and reply[:8] == expected_prefix
                    and reply[12:16] == struct.pack(">I", local_sid)
                    and (packet_type == 0x04 or reply[8:12] == struct.pack(">I", remote_sid))):
                return reply
        phase = "probe" if packet_type == 0x04 else "ready"
        raise IcomConnectivityError(f"Timed out waiting for Icom LAN {phase} reply on UDP {port}")

    def _authenticate(self) -> None:
        if self._control is None or self._serial is None or self._audio is None:
            raise IcomConnectivityError("Icom session sockets are unavailable")
        login = self._control_request(128, 0)
        login[26:28] = bytes([random.randrange(256), random.randrange(256)])
        login[64:80] = self._passcode(self.config.username)
        login[80:96] = self._passcode(self.config.password)
        login[96:112] = self._CLIENT_IDENTIFIER.ljust(16, b"\x00")
        self._send_tracked(self._control, bytes(login), self.config.control_port)
        reply = self._wait_control_response(login, 96, "login")
        if reply[48:52] != b"\x00" * 4:
            raise IcomConnectivityError("Icom LAN login rejected by radio")
        self._auth_id = reply[26:32]
        self._authenticated = True
        self._last_auth_renewal = monotonic()
        self._debug("login accepted")
        self._stage = "capabilities"
        token_request = self._send_auth(0x02)
        deadline = monotonic() + 5.0
        availability: dict[bytes, bool] = {}
        while True:
            packet = self._receive_control_packet(deadline, "capabilities/availability")
            if packet[26:32] != self._auth_id:
                continue
            if (len(packet) == 168 and packet[20:22] == b"\x02\x02"
                    and packet[22:24] == token_request[22:24]):
                self._capability_id = packet[66:82]
                self._radio_name = packet[82:114]
                self._audio_capability = self._parse_audio_capability(packet)
            elif len(packet) == 144 and packet[20:22] == b"\x03\x00":
                availability[packet[32:48]] = packet[96:100] != b"\x00" * 4
            elif len(packet) == 64 and packet[20:22] == b"\x02\x02" and packet[48:52] != b"\x00" * 4:
                raise IcomConnectivityError("Icom LAN token request rejected")
            if self._capability_id in availability:
                if availability[self._capability_id]:
                    raise IcomConnectivityError("Icom radio reports that its stream is already in use")
                break
        self._stage = "stream_request"
        conninfo = self._control_request(144, 0x03)
        conninfo[32:48] = self._capability_id
        conninfo[64:96] = self._radio_name
        conninfo[96:112] = self._passcode(self.config.username)
        rx_codecs = {"lpcm16_mono": 0x04, "lpcm16_stereo": 0x10}
        if self.config.rx_codec not in rx_codecs:
            raise IcomConnectivityError("Unsupported Icom RX codec; use lpcm16_mono or lpcm16_stereo")
        if self.config.tx_codec not in ("lpcm16_mono", "lpcm16_stereo"):
            raise IcomConnectivityError("Unsupported Icom TX codec; use lpcm16_mono")
        conninfo[112:116] = bytes([1, 1, rx_codecs[self.config.rx_codec], 0x04])
        struct.pack_into(">IIIII", conninfo, 116, self.config.sample_rate, self.config.sample_rate,
                         self._serial.getsockname()[1], self._audio.getsockname()[1], 150)
        conninfo[136] = 1
        self._send_tracked(self._control, bytes(conninfo), self.config.control_port)
        response = self._wait_control_response(conninfo, 80, "stream status")
        if response[48:52] != b"\x00" * 4 or response[64] != 0:
            raise IcomConnectivityError("Icom LAN stream request rejected or disconnected by radio")
        self._serial_remote_port = struct.unpack_from(">H", response, 66)[0]
        self._audio_remote_port = struct.unpack_from(">H", response, 70)[0]
        if not self._serial_remote_port or not self._audio_remote_port:
            raise IcomConnectivityError("Icom LAN stream status did not supply serial/audio ports")
        self._serial.connect((self.config.host, self._serial_remote_port))
        self._audio.connect((self.config.host, self._audio_remote_port))
        self._debug("stream request accepted remote_serial_port=%d remote_audio_port=%d",
                    self._serial_remote_port, self._audio_remote_port)

    def _control_request(self, length: int, request_type: int) -> bytearray:
        if self._control is None:
            raise IcomConnectivityError("Icom control socket is unavailable")
        packet = bytearray(length)
        packet[:16] = self._header(0, length=length, seq=self._claim_session_seq(self._control), sock=self._control)
        struct.pack_into(">IBBH", packet, 16, length - 16, 1, request_type, self._auth_inner_seq)
        self._auth_inner_seq = (self._auth_inner_seq + 1) & 0xFFFF
        if self._auth_id:
            packet[26:32] = self._auth_id
        return packet

    def _send_auth(self, magic: int) -> bytes:
        packet = self._control_request(64, magic)
        struct.pack_into(">H", packet, 36, 0x0798)
        assert self._control is not None
        self._send_tracked(self._control, bytes(packet), self.config.control_port)
        return bytes(packet)

    def _wait_control_response(self, request: bytes, length: int, description: str) -> bytes:
        deadline = monotonic() + 5.0
        while True:
            packet = self._receive_control_packet(deadline, description)
            if (len(packet) == length and packet[20] == 2 and packet[21:24] == request[21:24]
                    and packet[26:28] == request[26:28]
                    and (request[21] == 0 or packet[28:32] == self._auth_id[2:])):
                return packet

    def _receive_control_packet(self, deadline: float, description: str) -> bytes:
        if self._control is None:
            raise IcomConnectivityError("Icom control socket is unavailable")
        while monotonic() < deadline:
            if self._stop.is_set():
                raise IcomConnectivityError("Icom connection stopped")
            self._keepalive()
            try:
                packet, _ = self._control.recvfrom(65535)
            except socket.timeout:
                continue
            if self._handle_transport(self._control, packet, self.config.control_port):
                continue
            if len(packet) >= 32 and struct.unpack_from(">I", packet, 16)[0] == len(packet) - 16:
                return packet
        raise IcomConnectivityError(f"Timed out waiting for Icom LAN {description} reply on UDP {self.config.control_port}")

    def _open_serial(self) -> None:
        if self._serial is None:
            raise IcomConnectivityError("Icom serial channel is unavailable")
        packet = self._header(0, length=22, seq=self._claim_session_seq(self._serial), sock=self._serial)
        packet += b"\xc0\x01\x00" + struct.pack(">H", self._civ_seq) + b"\x05"
        self._civ_seq = (self._civ_seq + 1) & 0xFFFF or 1
        self._send_tracked(self._serial, packet, self._serial_remote_port)

    def _keepalive(self) -> None:
        now = monotonic()
        if now - self._last_keepalive < 0.1:
            return
        self._last_keepalive = now
        send_ping = now - self._last_ping >= 0.5
        if send_ping:
            self._last_ping = now
        for sock, port in ((self._control, self.config.control_port),
                           (self._serial, self._serial_remote_port),
                           (self._audio, self._audio_remote_port)):
            if sock is not None and self._stream_ids.get(sock, (0, 0))[1]:
                if sock is not self._audio:
                    packet = self._header(0, seq=self._claim_session_seq(sock), sock=sock)
                    self._send_tracked(sock, packet, port)
                if send_ping:
                    sequence = self._ping_sequences.get(sock, 0)
                    self._ping_sequences[sock] = (sequence + 1) & 0xFFFF
                    packet = self._header(7, length=21, seq=sequence, sock=sock)
                    packet += b"\x00" + struct.pack("<I", int(now * 1000) & 0xFFFFFFFF)
                    sock.sendto(packet, (self.config.host, port))

    def _send_tracked(self, sock: socket.socket, packet: bytes, port: int) -> None:
        sequence = struct.unpack_from("<H", packet, 6)[0]
        cache = self._sent_packets.setdefault(sock, {})
        cache.pop(sequence, None)
        cache[sequence] = packet
        while len(cache) > 128:
            del cache[next(iter(cache))]
        sock.sendto(packet, (self.config.host, port))

    def _handle_transport(self, sock: socket.socket, packet: bytes, port: int) -> bool:
        if len(packet) < 16:
            return True
        local_sid, remote_sid = self._stream_ids[sock]
        if packet[8:16] != struct.pack(">II", remote_sid, local_sid):
            return True
        length, packet_type, sequence = struct.unpack_from("<IHH", packet)
        if length != len(packet) and not (packet_type == 7 and length == 0 and len(packet) == 21):
            return True
        if packet_type == 7:
            if len(packet) == 21 and packet[16] == 0:
                reply = self._header(7, length=21, seq=sequence, sock=sock) + b"\x01" + packet[17:21]
                sock.sendto(reply, (self.config.host, port))
            return True
        if packet_type == 1:
            cache = self._sent_packets.get(sock, {})
            if len(packet) == 16:
                requested = [sequence] if sequence in cache else []
            elif (len(packet) - 16) % 2 == 0:
                requested = list(dict.fromkeys(
                    seq for (seq,) in struct.iter_unpack("<H", packet[16:]) if seq in cache
                ))
            else:
                requested = []
            for seq in requested:
                sock.sendto(cache[seq], (self.config.host, port))
                sleep(self._BOOTSTRAP_DUPLICATE_GAP_S)
                sock.sendto(cache[seq], (self.config.host, port))
            return True
        if packet_type == 5:
            raise IcomConnectivityError("Icom radio disconnected the LAN session")
        return packet_type != 0 or len(packet) == 16

    def _header(self, packet_type: int, *, length: int = 16, seq: int | None = None,
                remote_sid: int | None = None, sock: socket.socket | None = None) -> bytes:
        local_value, remote_value = self._local_sid, self._remote_sid
        if sock is not None and sock in self._stream_ids:
            local_value, remote_value = self._stream_ids[sock]
        if remote_sid is not None:
            remote_value = remote_sid
        sequence = self._session_seq if seq is None else seq
        return struct.pack("<IHH", length, packet_type, sequence) + struct.pack(">II", local_value, remote_value)

    def _claim_session_seq(self, sock: socket.socket | None = None) -> int:
        sock = self._control if sock is None else sock
        sequence = self._stream_sequences.get(sock, 1) if sock is not None else self._session_seq
        next_sequence = (sequence + 1) & 0xFFFF
        if sock is not None:
            self._stream_sequences[sock] = next_sequence
        if sock is self._control:
            self._session_seq = next_sequence
        return sequence

    def _send_audio_part(self, pcm: bytes) -> None:
        if self._audio is None:
            return
        payload = bytearray(b"\x80\x00")
        payload.extend(struct.pack(">H", self._audio_send_seq & 0xFFFF))
        payload.extend(b"\x00\x00")
        payload.extend(struct.pack(">H", len(pcm)))
        payload.extend(pcm)
        packet = self._header(0, length=16 + len(payload),
                              seq=self._claim_session_seq(self._audio), sock=self._audio) + bytes(payload)
        self._send_tracked(self._audio, packet, self._audio_remote_port)
        self._audio_send_seq = (self._audio_send_seq + 1) & 0xFFFF

    @classmethod
    def _passcode(cls, value: str) -> bytes:
        raw = value.encode("utf-8")[:16]
        return bytes(
            cls._PASSCODE_SEQUENCE[(byte - 32 + index) % len(cls._PASSCODE_SEQUENCE)]
            for index, byte in enumerate(raw)
        ).ljust(16, b"\x00")

    def _shutdown_session(self) -> None:
        self._pending_renewal = None
        if self._serial is not None and self._stream_ids.get(self._serial, (0, 0))[1]:
            try:
                packet = self._header(0, length=22, seq=self._claim_session_seq(self._serial), sock=self._serial)
                packet += b"\xc0\x01\x00" + struct.pack(">H", self._civ_seq) + b"\x00"
                self._send_tracked(self._serial, packet, self._serial_remote_port)
            except OSError:
                pass
        if self._control is not None and self._stream_ids.get(self._control, (0, 0))[1] and self._auth_id:
            try:
                request = self._send_auth(0x01)
                self._wait_deauthentication(request)
            except OSError:
                pass
        for sock, port in ((self._audio, self._audio_remote_port),
                           (self._serial, self._serial_remote_port),
                           (self._control, self.config.control_port)):
            if sock is None or not self._stream_ids.get(sock, (0, 0))[1]:
                continue
            try:
                packet = self._header(5, seq=0, sock=sock)
                sock.sendto(packet, (self.config.host, port))
                sleep(self._BOOTSTRAP_DUPLICATE_GAP_S)
                sock.sendto(packet, (self.config.host, port))
            except OSError:
                pass

    def _wait_deauthentication(self, request: bytes) -> None:
        if self._control is None:
            return
        deadline = monotonic() + self._DEAUTH_REPLY_TIMEOUT_S
        while monotonic() < deadline:
            try:
                packet, _ = self._control.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                if self._handle_transport(self._control, packet, self.config.control_port):
                    continue
            except IcomConnectivityError:
                return
            if (len(packet) == 64 and packet[20] == 2 and packet[21:24] == request[21:24]
                    and packet[26:32] == self._auth_id):
                sleep(self._DEAUTH_ACK_PAUSE_S)
                return

    def _close_sockets(self) -> None:
        for attr in ("_control", "_serial", "_audio"):
            sock = getattr(self, attr)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, attr, None)
        self._stream_ids.clear()
        self._stream_sequences.clear()
        self._ping_sequences.clear()
        self._sent_packets.clear()
        self._auth_id = b""
        self._pending_renewal = None
        self._connected = False
        self._authenticated = False
        self._tx_pcm_buffer.clear()
