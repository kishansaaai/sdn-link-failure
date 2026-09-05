from chaos_test import analyze_ping, percentile
from pcap_analysis import OFEvent, assert_recovery_sequence, parse_pdml


def test_ping_errors_are_not_replies_and_terminal_loss_is_counted():
    text = ("[100.1] 64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=1 ms\n"
            "[100.2] From 10.0.0.1 icmp_seq=2 Destination Host Unreachable\n"
            "10 packets transmitted, 1 received, 90% packet loss\n")
    result = analyze_ping(text, 100.0, 101.0)
    assert result["received"] == 1
    assert result["loss_pct"] == 90
    assert round(result["max_reply_gap_ms"]) == 900


def test_missing_measurement_is_not_zero_loss():
    result = analyze_ping("")
    assert result["loss_pct"] is None and not result["recovered"]
    assert percentile([], 95) is None


def test_no_port_down_is_not_success():
    assert not assert_recovery_sequence([])


def test_only_installs_on_same_stream_count():
    down = OFEvent(1, 12, stream=2, port_down=True)
    delete = OFEvent(1.01, 14, stream=2, command=3)
    unrelated = OFEvent(1.01, 14, stream=3, command=0)
    assert not assert_recovery_sequence([down, delete, unrelated])
    assert assert_recovery_sequence([down, OFEvent(1.02, 14, stream=2, command=0)])


def test_pdml_retains_coalesced_openflow_messages():
    xml = """<pdml><packet><proto name="frame">
    <field name="frame.time_epoch" show="10.5"/></proto><proto name="tcp">
    <field name="tcp.stream" show="2"/></proto>
    <proto name="openflow_v4"><field name="openflow_v4.type" show="12"/>
    <field name="openflow_v4.port.state.link_down" show="1"/></proto>
    <proto name="openflow_v4"><field name="openflow_v4.type" show="14"/>
    <field name="openflow_v4.flowmod.command" show="0"/></proto></packet></pdml>"""
    events = parse_pdml(xml)
    assert len(events) == 2 and events[0].port_down
    assert events[1].command == 0

def test_integration_recovery_retains_transient_packet_loss(monkeypatch):
    from types import SimpleNamespace
    from tests.integration_lab import ping
    replies = iter([
        "3 packets transmitted, 2 received, 33.3333% packet loss",
        "3 packets transmitted, 3 received, 0% packet loss",
    ])
    source = SimpleNamespace(cmd=lambda command: next(replies))
    destination = SimpleNamespace(IP=lambda: "10.0.0.2")
    monkeypatch.setattr("tests.integration_lab.time.sleep", lambda seconds: None)
    assert ping(source, destination) == [
        {"sent": 3, "received": 2}, {"sent": 3, "received": 3}]


def test_integration_recovery_has_a_deadline(monkeypatch):
    import pytest
    from types import SimpleNamespace
    from tests.integration_lab import ping
    clock = iter([0, 0, 2])
    monkeypatch.setattr("tests.integration_lab.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("tests.integration_lab.time.sleep", lambda seconds: None)
    source = SimpleNamespace(cmd=lambda command: "3 packets transmitted, 0 received")
    destination = SimpleNamespace(IP=lambda: "10.0.0.2")
    with pytest.raises(AssertionError, match="Packet recovery failed"):
        ping(source, destination, timeout=1)
