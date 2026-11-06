#!/usr/bin/env python3
"""Metadata-only network observer for the opt-in offline smoke guard.

The observer runs in a disposable helper container that shares one target
container's network and PID namespaces. It reads socket tables and captures
packet headers with the standard-library AF_PACKET interface. It extracts DNS
names and TLS SNI in memory, but it never emits or writes packet payloads.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import math
import os
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SAMPLE_INTERVAL = 0.2
SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 2.0
SHUTDOWN_ACK_TIMEOUT_SECONDS = 20.0
MAX_CAPTURE_FRAME_BYTES = 262_144
MAX_STREAM_BYTES = 65_536
MAX_EVENT_LINE_BYTES = 32_768
MAX_CONNECTIONS_PER_SAMPLE = 4096
MAX_PROCESS_SCAN = 8192
ETH_P_ALL = 0x0003
ETH_P_IP = 0x0800
ETH_P_IPV6 = 0x86DD
ETH_P_8021Q = 0x8100
ETH_P_8021AD = 0x88A8
ETH_P_QINQ = 0x9100
IPPROTO_TCP = 6
IPPROTO_UDP = 17
SO_RXQ_OVFL = getattr(socket, "SO_RXQ_OVFL", 40)
SOL_PACKET = getattr(socket, "SOL_PACKET", 263)
PACKET_STATISTICS = 6
UNKNOWN = "unknown"
KNOWN_INTERNAL_DNS_NAMES = frozenset({"app", "worker", "localhost"})
APP_LISTENER_PORT = 8765


class ObserverError(RuntimeError):
    """A capture or evidence error that must fail the smoke closed."""


class MalformedPacket(ObserverError):
    """A captured frame cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class SocketRecord:
    protocol: str
    family: str
    local_ip: str
    local_port: int
    remote_ip: str
    remote_port: int
    state: str
    inode: str

    def as_dict(self, owner: dict[str, Any], *, direction: str) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "local_ip": self.local_ip,
            "local_port": self.local_port,
            "remote_ip": self.remote_ip,
            "remote_port": self.remote_port,
            "state": self.state,
            "process": owner,
            "direction": direction,
        }


def _decode_ipv4(value: str) -> str:
    raw = bytes.fromhex(value)
    if len(raw) != 4:
        raise ValueError("invalid IPv4 address")
    return str(ipaddress.IPv4Address(raw[::-1]))


def _decode_ipv6(value: str) -> str:
    raw = bytes.fromhex(value)
    if len(raw) != 16:
        raise ValueError("invalid IPv6 address")
    # Linux prints each 32-bit word in host byte order in /proc/net/tcp6.
    decoded = b"".join(raw[offset : offset + 4][::-1] for offset in range(0, 16, 4))
    return str(ipaddress.IPv6Address(decoded))


def _parse_proc_endpoint(value: str, family: str) -> tuple[str, int]:
    address, separator, port = value.partition(":")
    if not separator or len(address) not in {8, 32}:
        raise ValueError("malformed proc endpoint")
    decoder = _decode_ipv4 if family == "ipv4" else _decode_ipv6
    number = int(port, 16)
    if not 0 <= number <= 65_535:
        raise ValueError("invalid proc port")
    return decoder(address), number


def parse_proc_net_sockets(
    text: str, *, family: str = "ipv4", protocol: str = "tcp"
) -> list[SocketRecord]:
    """Parse Linux TCP or UDP socket metadata without retaining payloads."""
    if family not in {"ipv4", "ipv6"}:
        raise ValueError("unknown proc TCP family")
    if protocol not in {"tcp", "udp"}:
        raise ValueError("unknown socket protocol")
    records: list[SocketRecord] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("sl"):
            continue
        fields = stripped.split()
        if len(fields) < 10:
            raise ValueError(f"malformed /proc/net/tcp row {line_number}")
        local_ip, local_port = _parse_proc_endpoint(fields[1], family)
        remote_ip, remote_port = _parse_proc_endpoint(fields[2], family)
        state = fields[3]
        inode = fields[9]
        if not inode.isdigit():
            raise ValueError(f"malformed socket inode {line_number}")
        records.append(
            SocketRecord(
                protocol=protocol,
                family=family,
                local_ip=local_ip,
                local_port=local_port,
                remote_ip=remote_ip,
                remote_port=remote_port,
                state=state,
                inode=inode,
            )
        )
    return records


def parse_proc_net_tcp(text: str, *, family: str = "ipv4") -> list[SocketRecord]:
    """Parse Linux TCP socket metadata without retaining command payloads."""
    return parse_proc_net_sockets(text, family=family, protocol="tcp")


def parse_proc_net_udp(text: str, *, family: str = "ipv4") -> list[SocketRecord]:
    """Parse Linux UDP socket metadata without retaining command payloads."""
    return parse_proc_net_sockets(text, family=family, protocol="udp")


def _unknown_process() -> dict[str, Any]:
    return {"pid": None, "comm": UNKNOWN, "executable": UNKNOWN, "role": UNKNOWN}


def _process_role(command_line: str) -> str:
    if "asr_studio.diarization_runner" in command_line:
        return "diarization"
    if "asr_studio.runner" in command_line:
        return "transcription"
    if "asr_studio.worker" in command_line:
        return "worker"
    if "asr_studio.app" in command_line or "uvicorn" in command_line:
        return "app"
    return UNKNOWN


def _process_metadata(pid: int, proc_root: Path) -> dict[str, Any]:
    base = proc_root / str(pid)
    try:
        comm = (base / "comm").read_text(encoding="utf-8", errors="replace").strip()
        executable = os.path.basename(os.readlink(base / "exe"))
        command_line = (
            (base / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        )
    except OSError:
        return _unknown_process()
    return {
        "pid": pid,
        "comm": comm or UNKNOWN,
        "executable": executable or UNKNOWN,
        "role": _process_role(command_line),
    }


def _canonical_ip(value: object) -> str:
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError as error:
        raise ValueError(f"invalid IP address: {value}") from error


def _canonical_ip_set(values: object) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes)):
        values = (values,)
    try:
        return frozenset(_canonical_ip(value) for value in values)  # type: ignore[union-attr]
    except TypeError as error:
        raise ValueError("IP values must be iterable") from error


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    """Exact namespace policy used for both socket and packet evidence."""

    service: str = "unknown"
    local_ips: frozenset[str] = frozenset()
    owned_ips: frozenset[str] = frozenset()
    resolver_ips: frozenset[str] = frozenset()
    allowed_dns_names: frozenset[str] = frozenset()
    listener_ports: frozenset[int] = frozenset()
    allowlist: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "local_ips",
            _canonical_ip_set(self.local_ips) | frozenset({"127.0.0.1", "::1"}),
        )
        object.__setattr__(self, "owned_ips", _canonical_ip_set(self.owned_ips))
        object.__setattr__(self, "resolver_ips", _canonical_ip_set(self.resolver_ips))
        object.__setattr__(
            self,
            "allowed_dns_names",
            frozenset(normalize_dns_name(str(value)) for value in self.allowed_dns_names),
        )
        ports = frozenset(int(value) for value in self.listener_ports)
        if any(port < 0 or port > 65_535 for port in ports):
            raise ValueError("listener ports must be between 0 and 65535")
        object.__setattr__(self, "listener_ports", ports)

    @property
    def allowed_internal_ips(self) -> frozenset[str]:
        return self.owned_ips | self.resolver_ips

    def is_local_ip(self, value: str) -> bool:
        try:
            return _canonical_ip(value) in self.local_ips
        except ValueError:
            return False

    def is_client_traffic(self, event: dict[str, Any]) -> bool:
        return self.service == "app" and (
            event.get("direction") == "incoming"
            or (
                event.get("direction") == "response"
                and event.get("local_port") in self.listener_ports
            )
        )

    def orient_packet(
        self,
        source_ip: str,
        source_port: int,
        destination_ip: str,
        destination_port: int,
    ) -> dict[str, Any]:
        source_local = self.is_local_ip(source_ip)
        destination_local = self.is_local_ip(destination_ip)
        if destination_local and not source_local:
            return {
                "direction": "incoming",
                "local_ip": destination_ip,
                "local_port": destination_port,
                "remote_ip": source_ip,
                "remote_port": source_port,
            }
        if source_local and source_port in self.listener_ports:
            return {
                "direction": "response",
                "local_ip": source_ip,
                "local_port": source_port,
                "remote_ip": destination_ip,
                "remote_port": destination_port,
            }
        if source_local and destination_local:
            return {
                "direction": "outbound",
                "local_ip": source_ip,
                "local_port": source_port,
                "remote_ip": destination_ip,
                "remote_port": destination_port,
            }
        if source_local:
            return {
                "direction": "outbound",
                "local_ip": source_ip,
                "local_port": source_port,
                "remote_ip": destination_ip,
                "remote_port": destination_port,
            }
        return {
            "direction": "unknown",
            "local_ip": destination_ip,
            "local_port": destination_port,
            "remote_ip": source_ip,
            "remote_port": source_port,
        }


def _socket_direction(record: SocketRecord, policy: CapturePolicy | None) -> str:
    if policy is not None and record.local_port in policy.listener_ports:
        return "incoming"
    return "outbound"


def socket_owners(*, proc_root: Path = Path("/proc")) -> dict[str, dict[str, Any]]:
    """Map socket inode strings to bounded process metadata."""
    owners: dict[str, dict[str, Any]] = {}
    try:
        process_paths = list(proc_root.iterdir())
    except OSError as error:
        raise ObserverError("cannot read process namespace") from error
    scanned = 0
    for process_path in process_paths:
        if not process_path.name.isdigit():
            continue
        scanned += 1
        if scanned > MAX_PROCESS_SCAN:
            raise ObserverError("process namespace exceeds observer bound")
        try:
            pid = int(process_path.name)
            fd_paths = (process_path / "fd").iterdir()
            for fd_path in fd_paths:
                try:
                    target = os.readlink(fd_path)
                except OSError:
                    continue
                if not (target.startswith("socket:[") and target.endswith("]")):
                    continue
                inode = target[8:-1]
                if inode not in owners:
                    owners[inode] = _process_metadata(pid, proc_root)
        except OSError:
            continue
    return owners


def read_socket_snapshot(
    *, proc_root: Path = Path("/proc"), policy: CapturePolicy | None = None
) -> list[dict[str, Any]]:
    """Read TCP and connected UDP sockets with bounded process metadata."""
    try:
        ipv4 = (proc_root / "net" / "tcp").read_text(encoding="ascii")
        ipv6 = (proc_root / "net" / "tcp6").read_text(encoding="ascii")
        udp4 = (proc_root / "net" / "udp").read_text(encoding="ascii")
        udp6 = (proc_root / "net" / "udp6").read_text(encoding="ascii")
    except OSError as error:
        raise ObserverError("cannot read TCP and UDP socket tables") from error
    try:
        records = (
            parse_proc_net_tcp(ipv4, family="ipv4")
            + parse_proc_net_tcp(ipv6, family="ipv6")
            + parse_proc_net_udp(udp4, family="ipv4")
            + parse_proc_net_udp(udp6, family="ipv6")
        )
    except (TypeError, ValueError) as error:
        raise ObserverError("malformed TCP or UDP socket table") from error
    owners = socket_owners(proc_root=proc_root)
    return [
        record.as_dict(
            owners.get(record.inode, _unknown_process()),
            direction=_socket_direction(record, policy),
        )
        for record in records
        if record.remote_port
    ]


def _parse_link_frame(frame: bytes) -> tuple[bytes, int]:
    if not frame:
        raise MalformedPacket("empty packet")
    # Linux AF_PACKET/SOCK_RAW returns the link header, including the Ethernet-like
    # loopback header. A Docker MAC can start with 4 or 6; that is not an IP version.
    if len(frame) < 14:
        raise MalformedPacket("short link header")
    ethertype = struct.unpack("!H", frame[12:14])[0]
    offset = 14
    while ethertype in {ETH_P_8021Q, ETH_P_8021AD, ETH_P_QINQ}:
        if len(frame) < offset + 4:
            raise MalformedPacket("short VLAN header")
        ethertype = struct.unpack("!H", frame[offset + 2 : offset + 4])[0]
        offset += 4
    if ethertype not in {ETH_P_IP, ETH_P_IPV6}:
        return b"", ethertype
    return frame[offset:], ethertype


def _parse_ipv4(payload: bytes) -> tuple[str, str, int, bytes]:
    if len(payload) < 20:
        raise MalformedPacket("short IPv4 header")
    version_ihl = payload[0]
    if version_ihl >> 4 != 4:
        raise MalformedPacket("invalid IPv4 version")
    header_length = (version_ihl & 0x0F) * 4
    total_length = struct.unpack("!H", payload[2:4])[0]
    if header_length < 20 or total_length < header_length or total_length > len(payload):
        raise MalformedPacket("invalid IPv4 length")
    source = str(ipaddress.IPv4Address(payload[12:16]))
    destination = str(ipaddress.IPv4Address(payload[16:20]))
    return source, destination, payload[9], payload[header_length:total_length]


def _parse_ipv6(payload: bytes) -> tuple[str, str, int, bytes]:
    if len(payload) < 40 or payload[0] >> 4 != 6:
        raise MalformedPacket("short IPv6 header")
    source = str(ipaddress.IPv6Address(payload[8:24]))
    destination = str(ipaddress.IPv6Address(payload[24:40]))
    next_header = payload[6]
    offset = 40
    # Skip bounded IPv6 extension headers. Fragment payloads are still safe to
    # classify by their endpoint, but an incomplete extension chain is evidence
    # that cannot be parsed reliably.
    for _ in range(8):
        if next_header in {0, 43, 60}:
            if len(payload) < offset + 8:
                raise MalformedPacket("short IPv6 extension header")
            extension_length = (payload[offset + 1] + 1) * 8
            if len(payload) < offset + extension_length:
                raise MalformedPacket("truncated IPv6 extension header")
            next_header = payload[offset]
            offset += extension_length
        elif next_header == 44:
            if len(payload) < offset + 8:
                raise MalformedPacket("short IPv6 fragment header")
            next_header = payload[offset]
            offset += 8
        elif next_header == 51:
            if len(payload) < offset + 8:
                raise MalformedPacket("short IPv6 authentication header")
            extension_length = (payload[offset + 1] + 2) * 4
            if len(payload) < offset + extension_length:
                raise MalformedPacket("truncated IPv6 authentication header")
            next_header = payload[offset]
            offset += extension_length
        else:
            return source, destination, next_header, payload[offset:]
    raise MalformedPacket("too many IPv6 extension headers")


def _parse_transport(protocol: int, payload: bytes) -> dict[str, Any] | None:
    if protocol == IPPROTO_TCP:
        if len(payload) < 20:
            raise MalformedPacket("short TCP header")
        source_port, destination_port, sequence = struct.unpack("!HHI", payload[:8])
        header_length = (payload[12] >> 4) * 4
        if header_length < 20 or header_length > len(payload):
            raise MalformedPacket("invalid TCP header length")
        flags = payload[13]
        return {
            "protocol": "tcp",
            "source_port": source_port,
            "destination_port": destination_port,
            "sequence": sequence,
            "flags": flags,
            "payload": payload[header_length:],
        }
    if protocol == IPPROTO_UDP:
        if len(payload) < 8:
            raise MalformedPacket("short UDP header")
        source_port, destination_port, length = struct.unpack("!HHH", payload[:6])
        if length < 8 or length > len(payload):
            raise MalformedPacket("invalid UDP length")
        return {
            "protocol": "udp",
            "source_port": source_port,
            "destination_port": destination_port,
            "payload": payload[8:length],
        }
    return None


def _decode_qname(payload: bytes) -> tuple[str, int] | None:
    if len(payload) < 12:
        raise MalformedPacket("short DNS header")
    question_count = struct.unpack("!H", payload[4:6])[0]
    if question_count < 1:
        return None
    offset = 12
    labels: list[str] = []
    for _ in range(128):
        if offset >= len(payload):
            raise MalformedPacket("truncated DNS name")
        length = payload[offset]
        offset += 1
        if length == 0:
            break
        if length & 0xC0:
            raise MalformedPacket("compressed DNS query name")
        if length > 63 or offset + length > len(payload):
            raise MalformedPacket("invalid DNS label")
        try:
            label = payload[offset : offset + length].decode("ascii")
        except UnicodeDecodeError as error:
            raise MalformedPacket("DNS label is not ASCII") from error
        labels.append(label.lower())
        offset += length
    else:
        raise MalformedPacket("DNS name exceeds bound")
    if not labels or offset + 4 > len(payload):
        raise MalformedPacket("incomplete DNS question")
    qtype = struct.unpack("!H", payload[offset : offset + 2])[0]
    return ".".join(labels), qtype


def extract_tls_sni(payload: bytes) -> str | None:
    """Extract only the ClientHello server name from a complete TLS record."""
    if len(payload) < 5 or payload[0] != 22 or payload[1] != 3:
        return None
    record_length = struct.unpack("!H", payload[3:5])[0]
    if record_length > MAX_STREAM_BYTES or len(payload) < 5 + record_length:
        return None
    handshake = payload[5 : 5 + record_length]
    if len(handshake) < 4 or handshake[0] != 1:
        return None
    hello_length = int.from_bytes(handshake[1:4], "big")
    if hello_length > len(handshake) - 4:
        return None
    hello = memoryview(handshake[4 : 4 + hello_length])
    if len(hello) < 34:
        return None
    offset = 2 + 32
    if offset >= len(hello):
        return None
    session_length = hello[offset]
    offset += 1 + session_length
    if offset + 2 > len(hello):
        return None
    cipher_length = int.from_bytes(hello[offset : offset + 2], "big")
    offset += 2 + cipher_length
    if offset >= len(hello):
        return None
    compression_length = hello[offset]
    offset += 1 + compression_length
    if offset + 2 > len(hello):
        return None
    extension_length = int.from_bytes(hello[offset : offset + 2], "big")
    offset += 2
    end = min(len(hello), offset + extension_length)
    while offset + 4 <= end:
        extension_type = int.from_bytes(hello[offset : offset + 2], "big")
        extension_size = int.from_bytes(hello[offset + 2 : offset + 4], "big")
        offset += 4
        if offset + extension_size > end:
            return None
        extension = hello[offset : offset + extension_size]
        if extension_type == 0:
            if len(extension) < 2:
                return None
            names_length = int.from_bytes(extension[:2], "big")
            name_offset = 2
            names_end = min(len(extension), 2 + names_length)
            while name_offset + 3 <= names_end:
                name_type = extension[name_offset]
                name_size = int.from_bytes(extension[name_offset + 1 : name_offset + 3], "big")
                name_offset += 3
                if name_offset + name_size > names_end:
                    return None
                if name_type == 0:
                    try:
                        name = bytes(extension[name_offset : name_offset + name_size]).decode(
                            "idna"
                        )
                    except UnicodeError:
                        return None
                    return name.rstrip(".").lower() or None
                name_offset += name_size
        offset += extension_size
    return None


class _TlsStreamTracker:
    def __init__(self) -> None:
        self._streams: dict[tuple[str, int, str, int], dict[str, Any]] = {}

    def add(
        self,
        key: tuple[str, int, str, int],
        sequence: int,
        payload: bytes,
    ) -> str | None:
        if not payload:
            return None
        stream = self._streams.setdefault(key, {"next": sequence, "data": bytearray()})
        expected = int(stream["next"])
        if sequence > expected:
            return None
        if sequence < expected:
            payload = payload[expected - sequence :]
        data: bytearray = stream["data"]
        data.extend(payload)
        stream["next"] = expected + len(payload)
        if len(data) > MAX_STREAM_BYTES:
            self._streams.pop(key, None)
            return None
        name = extract_tls_sni(bytes(data))
        if name is not None:
            self._streams.pop(key, None)
        return name


def decode_packet(
    frame: bytes,
    *,
    observed_at: float | None = None,
    tracker=None,
    policy: CapturePolicy | None = None,
) -> dict[str, Any] | None:
    """Decode one synthetic or live frame into endpoint/name metadata only."""
    payload, ether_type = _parse_link_frame(frame)
    if not payload:
        return None
    if ether_type == ETH_P_IP:
        source_ip, destination_ip, protocol, network_payload = _parse_ipv4(payload)
    elif ether_type == ETH_P_IPV6:
        source_ip, destination_ip, protocol, network_payload = _parse_ipv6(payload)
    else:
        return None
    transport = _parse_transport(protocol, network_payload)
    if transport is None:
        return None
    orientation = (
        policy.orient_packet(
            source_ip,
            transport["source_port"],
            destination_ip,
            transport["destination_port"],
        )
        if policy is not None
        else {
            "direction": "outbound",
            "local_ip": source_ip,
            "local_port": transport["source_port"],
            "remote_ip": destination_ip,
            "remote_port": transport["destination_port"],
        }
    )
    if policy is not None and orientation["direction"] == "unknown":
        # AF_PACKET also exposes bridge multicast and other frames that do
        # not involve this container. They are not container egress.
        return None
    event: dict[str, Any] = {
        "type": "packet",
        "at": observed_at if observed_at is not None else time.monotonic(),
        "protocol": transport["protocol"],
        **orientation,
    }
    if transport["protocol"] == "tcp":
        flags = int(transport["flags"])
        if flags & 0x02:
            event["kind"] = "syn"
        if (
            tracker is not None
            and transport["payload"]
            and transport["destination_port"] == 443
            and orientation["direction"] == "outbound"
        ):
            flow = (source_ip, transport["source_port"], destination_ip, 443)
            name = tracker.add(flow, int(transport["sequence"]), transport["payload"])
            if name:
                event["kind"] = "sni"
                event["sni"] = name
        return event if event.get("kind") else None
    resolver_request = (
        policy is not None
        and orientation["direction"] == "outbound"
        and destination_ip in policy.resolver_ips
    )
    if orientation["direction"] == "outbound" and (
        transport["destination_port"] == 53 or resolver_request
    ):
        parsed = _decode_qname(transport["payload"])
        if parsed is not None:
            event["kind"] = "dns"
            event["dns_name"], event["dns_type"] = parsed
            return event
    # DNS responses often use compressed names and do not add attribution. All
    # other UDP datagrams still provide an endpoint for strict policy checks.
    event["kind"] = "udp"
    return event


def _is_ip(value: object) -> bool:
    try:
        ipaddress.ip_address(str(value))
    except ValueError:
        return False
    return True


def normalize_dns_name(value: str) -> str:
    return value.rstrip(".").lower()


def is_internal_dns_name(
    value: str, *, allowed_names: frozenset[str] = KNOWN_INTERNAL_DNS_NAMES
) -> bool:
    """Return true only for an exact owned-stack DNS name."""
    return normalize_dns_name(value) in {normalize_dns_name(name) for name in allowed_names}


def is_internal_ip(value: str, *, allowed_ips: frozenset[str] = frozenset()) -> bool:
    """Return true for loopback or an explicitly verified stack IP only."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_loopback or str(address) in allowed_ips


def is_explicitly_forbidden_name(value: str) -> bool:
    normalized = normalize_dns_name(value)
    return normalized == "mobile.events.data.microsoft.com" or normalized.endswith(
        ".mobile.events.data.microsoft.com"
    )


def _matches_allowlist(event: dict[str, Any], allowlist: tuple[dict[str, Any], ...]) -> bool:
    remote_ip = event.get("remote_ip")
    remote_port = event.get("remote_port")
    name = event.get("dns_name") or event.get("sni")
    normalized_name = normalize_dns_name(name) if isinstance(name, str) else None
    for rule in allowlist:
        rule_name = rule.get("name")
        if isinstance(rule_name, str):
            rule_name = normalize_dns_name(rule_name)
        if event.get("kind") == "dns" and normalized_name == rule_name:
            return True
        if rule.get("port") != remote_port:
            continue
        rule_ip = rule.get("ip")
        if isinstance(rule_ip, str):
            with contextlib.suppress(ValueError):
                rule_ip = str(ipaddress.ip_address(rule_ip))
        if rule_ip not in {None, remote_ip}:
            continue
        if rule_name not in {None, normalized_name}:
            continue
        return True
    return False


def policy_violation(
    event: dict[str, Any], *, allowlist: tuple[dict[str, Any], ...] = (), policy=None
) -> str | None:
    """Return a strict-deny reason for one normalized endpoint/name event."""
    if policy is None:
        policy = CapturePolicy(
            allowed_dns_names=KNOWN_INTERNAL_DNS_NAMES,
            allowlist=allowlist,
        )
    elif allowlist:
        policy = CapturePolicy(
            service=policy.service,
            local_ips=policy.local_ips,
            owned_ips=policy.owned_ips,
            resolver_ips=policy.resolver_ips,
            allowed_dns_names=policy.allowed_dns_names,
            listener_ports=policy.listener_ports,
            allowlist=allowlist,
        )
    if policy.is_client_traffic(event):
        return None
    name = event.get("dns_name") or event.get("sni")
    normalized_name = normalize_dns_name(name) if isinstance(name, str) else None
    remote_ip = event.get("remote_ip")
    if not isinstance(remote_ip, str) or not _is_ip(remote_ip):
        return "endpoint has no valid remote IP"
    if event.get("kind") == "dns":
        if normalized_name is not None and is_explicitly_forbidden_name(normalized_name):
            return f"forbidden DNS/SNI name: {normalized_name}"
        if (
            normalized_name is not None
            and not is_internal_dns_name(normalized_name, allowed_names=policy.allowed_dns_names)
            and not _matches_allowlist(event, policy.allowlist)
        ):
            return f"external DNS/SNI name is not allowlisted: {normalized_name}"
    elif normalized_name is not None and not is_internal_dns_name(
        normalized_name, allowed_names=policy.allowed_dns_names
    ):
        if is_explicitly_forbidden_name(normalized_name):
            return f"forbidden DNS/SNI name: {normalized_name}"
        if not _matches_allowlist(event, policy.allowlist) and not is_internal_ip(
            remote_ip, allowed_ips=policy.allowed_internal_ips
        ):
            return f"external DNS/SNI name is not allowlisted: {normalized_name}"
    if is_internal_ip(remote_ip, allowed_ips=policy.allowed_internal_ips):
        return None
    if _matches_allowlist(event, policy.allowlist):
        return None
    return f"external endpoint is not allowlisted: {remote_ip}:{event.get('remote_port')}"


def validate_event(event: object) -> dict[str, Any]:
    """Validate the helper protocol and reject payload-bearing evidence."""
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ObserverError("observer emitted malformed JSON evidence")
    if any(key in event for key in ("payload", "raw", "frame", "cmdline", "environment")):
        raise ObserverError("observer emitted payload-bearing evidence")
    event_type = event["type"]
    if event_type == "ready":
        if event.get("version") != 1 or not isinstance(event.get("service"), str):
            raise ObserverError("observer readiness evidence is malformed")
        return event
    if event_type == "stopped":
        if (
            not isinstance(event.get("at"), (int, float))
            or isinstance(event.get("at"), bool)
            or not math.isfinite(float(event["at"]))
        ):
            raise ObserverError("observer stop evidence is malformed")
        return event
    if event_type == "error":
        if not isinstance(event.get("error"), str) or not event["error"]:
            raise ObserverError("observer error evidence is malformed")
        return event
    if event_type == "sample":
        if (
            not isinstance(event.get("at"), (int, float))
            or isinstance(event.get("at"), bool)
            or not math.isfinite(float(event["at"]))
            or not isinstance(event.get("connections"), list)
            or event.get("dropped") != 0
            or len(event["connections"]) > MAX_CONNECTIONS_PER_SAMPLE
        ):
            raise ObserverError("observer sample evidence is malformed")
        for connection in event["connections"]:
            if not isinstance(connection, dict):
                raise ObserverError("observer connection evidence is malformed")
            if any(key in connection for key in ("payload", "raw", "frame")):
                raise ObserverError("observer connection evidence contains payload")
            required = (
                "protocol",
                "local_ip",
                "local_port",
                "remote_ip",
                "remote_port",
                "process",
                "direction",
            )
            if any(key not in connection for key in required):
                raise ObserverError("observer connection evidence is incomplete")
            if (
                connection["protocol"] not in {"tcp", "udp"}
                or not isinstance(connection["local_ip"], str)
                or not _is_ip(connection["local_ip"])
                or not isinstance(connection["remote_ip"], str)
                or not _is_ip(connection["remote_ip"])
                or not isinstance(connection["local_port"], int)
                or isinstance(connection["local_port"], bool)
                or not isinstance(connection["remote_port"], int)
                or isinstance(connection["remote_port"], bool)
                or not 0 <= connection["local_port"] <= 65_535
                or not 0 <= connection["remote_port"] <= 65_535
                or connection["direction"] not in {"outbound", "incoming", "response", "unknown"}
                or not isinstance(connection["process"], dict)
            ):
                raise ObserverError("observer connection evidence is malformed")
            process = connection["process"]
            if (
                not set(process) >= {"pid", "comm", "executable", "role"}
                or (
                    process["pid"] is not None
                    and (
                        not isinstance(process["pid"], int)
                        or isinstance(process["pid"], bool)
                        or process["pid"] <= 0
                    )
                )
                or any(
                    not isinstance(process[field], str) for field in ("comm", "executable", "role")
                )
            ):
                raise ObserverError("observer process evidence is malformed")
        return event
    if event_type == "packet":
        required = (
            "at",
            "protocol",
            "direction",
            "local_ip",
            "local_port",
            "remote_ip",
            "remote_port",
            "kind",
        )
        if any(key not in event for key in required):
            raise ObserverError("observer packet evidence is incomplete")
        if (
            not isinstance(event["at"], (int, float))
            or isinstance(event["at"], bool)
            or not math.isfinite(float(event["at"]))
            or event["protocol"] not in {"tcp", "udp"}
            or not isinstance(event["local_ip"], str)
            or not _is_ip(event["local_ip"])
            or not isinstance(event["remote_ip"], str)
            or not _is_ip(event["remote_ip"])
            or not isinstance(event["local_port"], int)
            or isinstance(event["local_port"], bool)
            or not isinstance(event["remote_port"], int)
            or isinstance(event["remote_port"], bool)
            or not 0 <= event["local_port"] <= 65_535
            or not 0 <= event["remote_port"] <= 65_535
            or event["direction"] not in {"outbound", "incoming", "response", "unknown"}
            or event["kind"] not in {"syn", "sni", "dns", "udp"}
        ):
            raise ObserverError("observer packet evidence is malformed")
        if event["kind"] in {"syn", "sni"} and event["protocol"] != "tcp":
            raise ObserverError("observer TCP evidence is malformed")
        if event["kind"] in {"dns", "udp"} and event["protocol"] != "udp":
            raise ObserverError("observer UDP evidence is malformed")
        if event["kind"] == "sni" and (
            event["protocol"] != "tcp" or not isinstance(event.get("sni"), str)
        ):
            raise ObserverError("observer SNI evidence is malformed")
        if event["kind"] == "dns" and (
            event["protocol"] != "udp"
            or not isinstance(event.get("dns_name"), str)
            or not isinstance(event.get("dns_type"), int)
            or isinstance(event.get("dns_type"), bool)
        ):
            raise ObserverError("observer DNS evidence is malformed")
        return event
    raise ObserverError(f"observer emitted unknown event type: {event_type}")


def observer_command(
    image: str,
    container_id: str,
    *,
    project: str,
    service: str,
    sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
    local_ips: tuple[str, ...] = (),
    owned_ips: tuple[str, ...] = (),
    resolver_ips: tuple[str, ...] = (),
    listener_ports: tuple[int, ...] = (),
    allowed_dns_names: tuple[str, ...] = (),
) -> list[str]:
    """Build a bounded disposable helper command without pulls or host changes."""
    name = f"{project}-{service}-egress-observer"
    command = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--pull=never",
        "--name",
        name,
        "--label",
        f"com.asr.offline-smoke.project={project}",
        "--label",
        "com.asr.offline-smoke.helper=egress-observer",
        "--network",
        f"container:{container_id}",
        "--pid",
        f"container:{container_id}",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "NET_RAW",
        "--cap-add",
        "SYS_PTRACE",
        "--cap-add",
        "DAC_READ_SEARCH",
        "--user",
        "0:0",
        "--security-opt",
        "no-new-privileges:true",
        "--entrypoint",
        "python",
        image,
        "-u",
        "-",
        "--service",
        service,
        "--sample-interval",
        str(sample_interval),
    ]
    for value in sorted(local_ips):
        command.extend(("--local-ip", value))
    for value in sorted(owned_ips):
        command.extend(("--owned-ip", value))
    for value in sorted(resolver_ips):
        command.extend(("--resolver-ip", value))
    for value in sorted(listener_ports):
        command.extend(("--listener-port", str(value)))
    for value in sorted(allowed_dns_names):
        command.extend(("--allowed-dns-name", value))
    return command


def observer_name(project: str, service: str) -> str:
    return f"{project}-{service}-egress-observer"


def _emit(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _packet_drop_count(capture: socket.socket) -> int:
    """Read AF_PACKET kernel statistics. PACKET_STATISTICS resets the counters."""
    try:
        raw = capture.getsockopt(SOL_PACKET, PACKET_STATISTICS, 12)
    except OSError as error:
        raise ObserverError("AF_PACKET drop statistics are unavailable") from error
    if len(raw) < 8:
        raise ObserverError("AF_PACKET returned malformed drop statistics")
    _packets, drops = struct.unpack("=II", raw[:8])
    return drops


def _run_observer(
    service: str,
    sample_interval: float,
    *,
    local_ips: tuple[str, ...] = (),
    owned_ips: tuple[str, ...] = (),
    resolver_ips: tuple[str, ...] = (),
    listener_ports: tuple[int, ...] = (),
    allowed_dns_names: tuple[str, ...] = (),
) -> int:
    if not 0.05 <= sample_interval <= 2.0 or not math.isfinite(sample_interval):
        _emit({"type": "error", "error": "invalid_sample_interval"})
        return 2
    stopping = False
    stop_deadline: float | None = None
    acknowledged = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stopping, stop_deadline
        if not stopping:
            stop_deadline = time.monotonic() + SHUTDOWN_DRAIN_TIMEOUT_SECONDS
        stopping = True

    def acknowledge_stop(_signum, _frame) -> None:
        nonlocal acknowledged
        acknowledged = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGUSR1, acknowledge_stop)
    try:
        policy = CapturePolicy(
            service=service,
            local_ips=frozenset(local_ips),
            owned_ips=frozenset(owned_ips),
            resolver_ips=frozenset(resolver_ips),
            listener_ports=frozenset(listener_ports),
            allowed_dns_names=frozenset(allowed_dns_names),
        )
    except (TypeError, ValueError):
        _emit({"type": "error", "error": "invalid_capture_policy"})
        return 2
    tracker = _TlsStreamTracker()
    try:
        capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
        capture.settimeout(min(0.05, sample_interval))
        capture.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        capture.setsockopt(socket.SOL_SOCKET, SO_RXQ_OVFL, 1)
        _packet_drop_count(capture)
    except (OSError, ObserverError):
        _emit({"type": "error", "error": "capture_start_failed"})
        return 2
    _emit(
        {
            "type": "ready",
            "version": 1,
            "service": service,
            "sample_interval": sample_interval,
        }
    )
    next_sample = time.monotonic()
    try:
        while True:
            try:
                frame, ancillary, _flags, _address = capture.recvmsg(MAX_CAPTURE_FRAME_BYTES, 256)
            except TimeoutError:
                frame = b""
                ancillary = []
            except OSError as error:
                if error.errno == getattr(os, "EINTR", 4):
                    continue
                raise ObserverError("capture read failed") from error
            for level, kind, value in ancillary:
                if level == socket.SOL_SOCKET and kind == SO_RXQ_OVFL and len(value) >= 4:
                    dropped = struct.unpack("I", value[:4])[0]
                    if dropped:
                        raise ObserverError("capture packet drops detected")
            now = time.monotonic()
            if stop_deadline is not None and now >= stop_deadline:
                raise ObserverError("capture drain exceeded its shutdown deadline")
            dropped = _packet_drop_count(capture)
            if dropped:
                raise ObserverError("capture packet drops detected")
            if frame:
                event = decode_packet(frame, observed_at=now, tracker=tracker, policy=policy)
                if event is not None:
                    _emit(event)
            if now >= next_sample:
                connections = read_socket_snapshot(policy=policy)
                if len(connections) > MAX_CONNECTIONS_PER_SAMPLE:
                    raise ObserverError("socket evidence exceeds observer bound")
                _emit(
                    {
                        "type": "sample",
                        "at": now,
                        "connections": connections,
                        "dropped": dropped,
                    }
                )
                next_sample = now + sample_interval
            if stopping and not frame:
                # Drain queued packets through one quiet read after the task ends.
                # Stopping at the signal alone could discard its final connection.
                break
    except MalformedPacket:
        _emit({"type": "error", "error": "malformed_packet"})
        return 2
    except ObserverError as error:
        error_name = "packet_drops" if "packet drops" in str(error) else "capture_error"
        _emit({"type": "error", "error": error_name})
        return 2
    finally:
        capture.close()
    _emit({"type": "stopped", "at": time.monotonic()})
    # Keep Docker's attach stream alive until the controller has consumed the
    # barrier and every preceding event. Immediate exit can truncate its tail.
    ack_deadline = time.monotonic() + SHUTDOWN_ACK_TIMEOUT_SECONDS
    while not acknowledged and time.monotonic() < ack_deadline:
        time.sleep(0.02)
    if not acknowledged:
        _emit({"type": "error", "error": "shutdown_ack_timeout"})
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Metadata-only namespace egress observer")
    parser.add_argument("--service", required=True, choices=("app", "worker"))
    parser.add_argument("--sample-interval", type=float, default=DEFAULT_SAMPLE_INTERVAL)
    parser.add_argument("--local-ip", action="append", default=[])
    parser.add_argument("--owned-ip", action="append", default=[])
    parser.add_argument("--resolver-ip", action="append", default=[])
    parser.add_argument("--listener-port", action="append", type=int, default=[])
    parser.add_argument("--allowed-dns-name", action="append", default=[])
    arguments = parser.parse_args(argv)
    return _run_observer(
        arguments.service,
        arguments.sample_interval,
        local_ips=tuple(arguments.local_ip),
        owned_ips=tuple(arguments.owned_ip),
        resolver_ips=tuple(arguments.resolver_ip),
        listener_ports=tuple(arguments.listener_port),
        allowed_dns_names=tuple(arguments.allowed_dns_name),
    )


if __name__ == "__main__":
    raise SystemExit(main())
