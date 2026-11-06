from __future__ import annotations

import importlib.util
import ipaddress
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "egress_observer.py"
SPEC = importlib.util.spec_from_file_location("egress_observer_tests", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = observer
SPEC.loader.exec_module(observer)


def _ethernet(payload: bytes, ether_type: int) -> bytes:
    return (
        b"\x00\x01\x02\x03\x04\x05"
        + b"\x06\x07\x08\x09\x0a\x0b"
        + struct.pack("!H", ether_type)
        + payload
    )


def _ipv4_frame(
    source: str,
    destination: str,
    protocol: int,
    transport: bytes,
    *,
    options: bytes = b"",
) -> bytes:
    header_length = 20 + len(options)
    header = struct.pack(
        "!BBHHHBBH4s4s",
        (4 << 4) | (header_length // 4),
        0,
        header_length + len(transport),
        1,
        0,
        64,
        protocol,
        0,
        ipaddress.IPv4Address(source).packed,
        ipaddress.IPv4Address(destination).packed,
    )
    return _ethernet(header + options + transport, observer.ETH_P_IP)


def _ipv6_frame(source: str, destination: str, next_header: int, transport: bytes) -> bytes:
    header = struct.pack(
        "!IHBB16s16s",
        6 << 28,
        len(transport),
        next_header,
        64,
        ipaddress.IPv6Address(source).packed,
        ipaddress.IPv6Address(destination).packed,
    )
    return _ethernet(header + transport, observer.ETH_P_IPV6)


def _tcp(
    source_port: int,
    destination_port: int,
    *,
    sequence: int = 10,
    flags: int = 0x02,
    payload=b"",
):
    header = struct.pack(
        "!HHIIHHHH",
        source_port,
        destination_port,
        sequence,
        0,
        (5 << 12) | flags,
        65_535,
        0,
        0,
    )
    return header + payload


def _udp(source_port: int, destination_port: int, payload: bytes) -> bytes:
    return struct.pack("!HHHH", source_port, destination_port, 8 + len(payload), 0) + payload


def _dns_query(name: str, qtype: int = 1) -> bytes:
    labels = b"".join(bytes((len(label),)) + label.encode("ascii") for label in name.split("."))
    return (
        struct.pack("!HHHHHH", 1, 0x0100, 1, 0, 0, 0)
        + labels
        + b"\0"
        + struct.pack("!HH", qtype, 1)
    )


def _tls_client_hello(name: str) -> bytes:
    encoded_name = name.encode("ascii")
    server_names = (
        struct.pack("!H", len(encoded_name) + 3)
        + b"\0"
        + struct.pack("!H", len(encoded_name))
        + encoded_name
    )
    extension = struct.pack("!HH", 0, len(server_names)) + server_names
    hello = (
        b"\x03\x03"
        + bytes(range(32))
        + b"\0"
        + struct.pack("!H", 2)
        + b"\x13\x01"
        + b"\x01\0"
        + struct.pack("!H", len(extension))
        + extension
    )
    handshake = b"\x01" + len(hello).to_bytes(3, "big") + hello
    return b"\x16\x03\x03" + struct.pack("!H", len(handshake)) + handshake


def _policy(service: str = "worker") -> observer.CapturePolicy:
    return observer.CapturePolicy(
        service=service,
        local_ips=frozenset({"10.0.0.2"}),
        owned_ips=frozenset({"10.0.0.2", "10.0.0.3"}),
        resolver_ips=frozenset({"127.0.0.11"}),
        allowed_dns_names=frozenset({"app", "worker", "localhost"}),
        listener_ports=frozenset({8765}) if service == "app" else frozenset(),
    )


def test_packet_parser_reports_ipv4_ipv6_and_udp_without_payload():
    ipv4 = observer.decode_packet(
        _ipv4_frame("10.0.0.2", "1.1.1.1", observer.IPPROTO_UDP, _udp(40000, 443, b"secret")),
        observed_at=1.0,
        policy=_policy(),
    )
    ipv6 = observer.decode_packet(
        _ipv6_frame(
            "2001:db8::2",
            "2001:db8::1",
            observer.IPPROTO_UDP,
            _udp(40001, 443, b"secret"),
        ),
        observed_at=2.0,
    )

    assert ipv4 == {
        "type": "packet",
        "at": 1.0,
        "protocol": "udp",
        "direction": "outbound",
        "local_ip": "10.0.0.2",
        "local_port": 40000,
        "remote_ip": "1.1.1.1",
        "remote_port": 443,
        "kind": "udp",
    }
    assert ipv6["remote_ip"] == "2001:db8::1"
    assert ipv6["protocol"] == "udp"
    assert "payload" not in ipv4


@pytest.mark.parametrize("mac_prefix", [0x02, 0x42, 0x46, 0x62, 0x66])
def test_ethernet_mac_prefix_is_not_interpreted_as_an_ip_version(mac_prefix):
    frame = _ipv4_frame("10.0.0.2", "1.1.1.1", observer.IPPROTO_UDP, _udp(40000, 443, b""))
    frame = bytes([mac_prefix]) + frame[1:]

    event = observer.decode_packet(frame, policy=_policy())

    assert event["remote_ip"] == "1.1.1.1"
    assert event["remote_port"] == 443
    assert event["protocol"] == "udp"


@pytest.mark.parametrize("mac_prefix", [0x42, 0x62])
def test_non_ip_ethernet_frame_is_ignored_regardless_of_mac_prefix(mac_prefix):
    frame = _ethernet(bytes(28), 0x0806)
    frame = bytes([mac_prefix]) + frame[1:]

    assert observer.decode_packet(frame, policy=_policy()) is None


def test_packet_parser_ignores_bridge_frame_that_does_not_involve_target():
    event = observer.decode_packet(
        _ipv4_frame(
            "192.168.50.1",
            "192.168.50.2",
            observer.IPPROTO_UDP,
            _udp(5353, 5353, b"multicast"),
        ),
        policy=_policy(),
    )

    assert event is None


def test_dns_query_is_decoded_and_normalized():
    event = observer.decode_packet(
        _ipv4_frame(
            "10.0.0.2",
            "127.0.0.11",
            observer.IPPROTO_UDP,
            _udp(41000, 53, _dns_query("Example.COM")),
        ),
        observed_at=3.0,
        policy=_policy(),
    )

    assert event is not None
    assert event["kind"] == "dns"
    assert event["dns_name"] == "example.com"
    assert event["remote_ip"] == "127.0.0.11"
    assert observer.policy_violation(event, policy=_policy()) is not None


@pytest.mark.parametrize(
    ("name", "allowed"),
    [("app", True), ("mobile.events.data.microsoft.com", False)],
)
def test_translated_docker_resolver_port_keeps_dns_name_policy(name, allowed):
    policy = _policy("app")
    request = observer.decode_packet(
        _ipv4_frame(
            "10.0.0.2",
            "127.0.0.11",
            observer.IPPROTO_UDP,
            _udp(49297, 49298, _dns_query(name)),
        ),
        observed_at=3.5,
        policy=policy,
    )
    response = observer.decode_packet(
        _ipv4_frame(
            "127.0.0.11",
            "10.0.0.2",
            observer.IPPROTO_UDP,
            _udp(53, 49297, b"dns-response"),
        ),
        observed_at=3.6,
        policy=policy,
    )

    assert request is not None and request["kind"] == "dns"
    assert request["remote_port"] == 49298
    assert (observer.policy_violation(request, policy=policy) is None) is allowed
    assert response is not None
    assert response["direction"] == "incoming"
    assert response["kind"] == "udp"
    assert "dns_name" not in response


def test_split_tls_sni_is_reassembled_without_emitting_payload():
    tracker = observer._TlsStreamTracker()
    hello = _tls_client_hello("mobile.events.data.microsoft.com")
    split = len(hello) // 2
    first = observer.decode_packet(
        _ipv4_frame(
            "10.0.0.2",
            "4.150.223.100",
            observer.IPPROTO_TCP,
            _tcp(42000, 443, payload=hello[:split], flags=0x18),
        ),
        observed_at=4.0,
        tracker=tracker,
        policy=_policy(),
    )
    second = observer.decode_packet(
        _ipv4_frame(
            "10.0.0.2",
            "4.150.223.100",
            observer.IPPROTO_TCP,
            _tcp(42000, 443, sequence=10 + split, payload=hello[split:], flags=0x18),
        ),
        observed_at=4.1,
        tracker=tracker,
        policy=_policy(),
    )

    assert first is None
    assert second is not None
    assert second["kind"] == "sni"
    assert second["sni"] == "mobile.events.data.microsoft.com"
    assert "payload" not in second
    assert observer.policy_violation(second, policy=_policy()) is not None


@pytest.mark.parametrize(
    "event",
    [
        {
            "kind": "syn",
            "type": "packet",
            "at": 1.0,
            "protocol": "tcp",
            "direction": "outbound",
            "local_ip": "10.0.0.2",
            "local_port": 40000,
            "remote_ip": "192.168.50.123",
            "remote_port": 443,
        },
        {
            "kind": "udp",
            "type": "packet",
            "at": 1.0,
            "protocol": "udp",
            "direction": "outbound",
            "local_ip": "10.0.0.2",
            "local_port": 40000,
            "remote_ip": "192.168.50.123",
            "remote_port": 443,
        },
        {
            "kind": "udp",
            "type": "packet",
            "at": 1.0,
            "protocol": "udp",
            "direction": "outbound",
            "local_ip": "10.0.0.2",
            "local_port": 40000,
            "remote_ip": "1.1.1.1",
            "remote_port": 443,
        },
    ],
    ids=["unowned-private-tcp", "unowned-private-udp", "public-udp"],
)
def test_policy_rejects_unowned_private_and_public_destinations(event):
    assert observer.policy_violation(event, policy=_policy()) is not None


def test_policy_allows_exact_owned_stack_and_resolver_but_not_internal_suffixes():
    policy = _policy()
    owned = {
        "kind": "udp",
        "type": "packet",
        "at": 1.0,
        "protocol": "udp",
        "direction": "outbound",
        "local_ip": "10.0.0.2",
        "local_port": 40000,
        "remote_ip": "10.0.0.3",
        "remote_port": 8765,
    }
    internal_dns = {
        **owned,
        "kind": "dns",
        "remote_ip": "127.0.0.11",
        "remote_port": 53,
        "dns_name": "worker",
        "dns_type": 1,
    }
    unowned_name = {**internal_dns, "dns_name": "unowned.internal"}

    assert observer.policy_violation(owned, policy=policy) is None
    assert observer.policy_violation(internal_dns, policy=policy) is None
    assert observer.policy_violation(unowned_name, policy=policy) is not None
    assert observer.is_internal_ip("192.168.50.123") is False


def test_app_listener_incoming_client_and_response_are_not_egress():
    policy = _policy("app")
    incoming = {
        "kind": "syn",
        "type": "packet",
        "at": 1.0,
        "protocol": "tcp",
        "direction": "incoming",
        "local_ip": "10.0.0.2",
        "local_port": 8765,
        "remote_ip": "192.168.50.123",
        "remote_port": 51000,
    }
    response = {**incoming, "direction": "response", "local_port": 8765, "remote_port": 51000}

    assert observer.policy_violation(incoming, policy=policy) is None
    assert observer.policy_violation(response, policy=policy) is None


def test_worker_does_not_treat_unowned_incoming_lan_packet_as_client_traffic():
    event = {
        "kind": "udp",
        "type": "packet",
        "at": 1.0,
        "protocol": "udp",
        "direction": "incoming",
        "local_ip": "10.0.0.2",
        "local_port": 40123,
        "remote_ip": "192.168.50.123",
        "remote_port": 443,
    }

    assert observer.policy_violation(event, policy=_policy("worker")) is not None


def test_malformed_packets_and_payload_or_drop_evidence_fail_closed():
    with pytest.raises(observer.MalformedPacket):
        observer.decode_packet(b"", policy=_policy())
    with pytest.raises(observer.MalformedPacket):
        observer.decode_packet(
            _ipv4_frame("10.0.0.2", "127.0.0.11", observer.IPPROTO_UDP, _udp(41000, 53, b"bad")),
            policy=_policy(),
        )
    with pytest.raises(observer.ObserverError):
        observer.validate_event({"type": "packet", "payload": "secret"})
    with pytest.raises(observer.ObserverError):
        observer.validate_event({"type": "sample", "at": 1.0, "connections": [], "dropped": 1})


def test_packet_drop_statistics_are_queried_and_nonzero_drops_fail():
    class Capture:
        def getsockopt(self, _level, _option, _size):
            return struct.pack("=III", 4, 2, 0)

    assert observer._packet_drop_count(Capture()) == 2

    class Unavailable:
        def getsockopt(self, _level, _option, _size):
            raise OSError("not supported")

    with pytest.raises(observer.ObserverError, match="unavailable"):
        observer._packet_drop_count(Unavailable())


def test_observer_command_has_only_approved_capabilities_and_shared_owned_namespaces():
    command = observer.observer_command(
        "asr-studio:issue92-preflight",
        "target123",
        project="asr-offline-smoke-abcd1234",
        service="worker",
        local_ips=("10.0.0.3",),
        owned_ips=("10.0.0.2", "10.0.0.3"),
        resolver_ips=("127.0.0.11",),
        listener_ports=(),
        allowed_dns_names=("app", "worker"),
    )

    assert command[command.index("--network") + 1] == "container:target123"
    assert command[command.index("--pid") + 1] == "container:target123"
    assert command.count("--cap-add") == 3
    assert {command[index + 1] for index, value in enumerate(command) if value == "--cap-add"} == {
        "NET_RAW",
        "SYS_PTRACE",
        "DAC_READ_SEARCH",
    }
    assert "--privileged" not in command
    assert "--volume" not in command
    assert "host" not in command[command.index("--pid") + 1]
