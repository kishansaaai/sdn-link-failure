"""Controller regressions using real Ryu packet and OpenFlow encoders."""
import os
os.environ["EVENTLET_NO_GREENDNS"] = "yes"
import eventlet.wsgi
if not hasattr(eventlet.wsgi, "ALREADY_HANDLED"):
    eventlet.wsgi.ALREADY_HANDLED = object()

import json
import time
from collections import deque
from types import SimpleNamespace
import pytest
from ryu.ofproto import ofproto_v1_3 as ofp, ofproto_v1_3_parser as parser
from ryu.lib.packet import ethernet, packet
from webob import Request
from ryu_controller.sdn_controller import SDNController, SDNApi
from ryu_controller.topology_graph import TopologyGraph
from ryu_controller.metrics_exporter import SDNMetrics
from ryu_controller.group_allocator import GroupIdAllocator


class Datapath:
    ofproto = ofp
    ofproto_parser = parser

    def __init__(self, dpid):
        self.id = dpid
        self.messages = []

    def send_msg(self, msg):
        msg.serialize()  # fail on invalid wire encoding, not just object construction
        self.messages.append(msg)


@pytest.fixture
def controller():
    c = SDNController.__new__(SDNController)
    c.graph = TopologyGraph()
    c.graph.add_link(1, 1, 2, 1)
    c.graph.add_link(1, 2, 3, 1)
    c.graph.add_link(2, 2, 4, 1)
    c.graph.add_link(3, 2, 4, 2)
    c.graph.learn_host("00:00:00:00:00:01", 1, 9)
    c.graph.learn_host("00:00:00:00:00:02", 4, 7)
    c.dpid_conns = {i: Datapath(i) for i in range(1, 5)}
    c.ready = set(c.dpid_conns)
    c.intents = set()
    c.active_paths = {}
    c.groups = {}
    c.group_alloc = GroupIdAllocator()
    c.recovery_log = deque(maxlen=1000)
    c.metrics = SDNMetrics()
    c.election = None
    c.ports = {i: {p: SimpleNamespace(state=0, config=0) for p in (1, 2, 7, 9)}
               for i in c.ready}
    c.trunk_ports = {(i, p) for i, edges in c.graph.adj.items() for p, _ in edges.values()}
    c.port_prev_stats = {}
    c.link_seen = {}
    c.capacity = 100
    return c


KEY = ("00:00:00:00:00:01", "00:00:00:00:00:02")


def test_ecmp_flow_references_group_and_group_modifies(controller):
    c = controller
    c.intents.add(KEY)
    c._recompute()
    dp = c.dpid_conns[1]
    groups = [m for m in dp.messages if isinstance(m, parser.OFPGroupMod)]
    assert len(groups) == 1 and len(groups[0].buckets) == 2
    flow = [m for m in dp.messages if isinstance(m, parser.OFPFlowMod)][-1]
    assert isinstance(flow.instructions[0].actions[0], parser.OFPActionGroup)
    c._recompute({KEY}, "packet")
    assert [m for m in dp.messages if isinstance(m, parser.OFPGroupMod)][-1].command == ofp.OFPGC_MODIFY


def test_failure_on_secondary_branch_removes_group(controller):
    c = controller
    c.intents.add(KEY)
    c._recompute()
    assert KEY in c._affected(3, 4)
    c.graph.remove_link(3, 4)
    c._recompute(c._affected(3, 4), "failure")
    assert c.active_paths[KEY] == [[1, 2, 4]]
    assert not c.groups
    assert any(isinstance(m, parser.OFPFlowMod) and m.command == ofp.OFPFC_DELETE
               for m in c.dpid_conns[3].messages)


def test_partition_deletes_stale_rules_and_restoration_recovers(controller):
    c = controller
    c.intents.add(KEY)
    c._recompute()
    c.graph.remove_link(2, 4)
    c.graph.remove_link(3, 4)
    c._recompute()
    assert KEY not in c.active_paths and KEY in c.intents
    assert c.recovery_log[-1]["status"] == "unreachable"
    c.graph.add_link(2, 2, 4, 1)
    c._recompute(reason="restoration")
    assert c.active_paths[KEY] == [[1, 2, 4]]


def test_broadcast_learns_source_and_never_outputs_to_trunks(controller):
    c = controller
    pkt = packet.Packet()
    pkt.add_protocol(ethernet.ethernet(src=KEY[0], dst="ff:ff:ff:ff:ff:ff", ethertype=0x806))
    pkt.serialize()
    msg = SimpleNamespace(datapath=c.dpid_conns[1], data=pkt.data, match={"in_port": 9})
    c.packet_in_handler(SimpleNamespace(msg=msg))
    assert c.graph.hosts[KEY[0]].port == 9
    for dpid, dp in c.dpid_conns.items():
        for out in dp.messages:
            for action in out.actions:
                assert (dpid, action.port) not in c.trunk_ports


def test_same_switch_first_packet_uses_destination_port(controller):
    c = controller
    c.graph.learn_host(KEY[1], 1, 7)
    pkt = packet.Packet()
    pkt.add_protocol(ethernet.ethernet(src=KEY[0], dst=KEY[1], ethertype=0x800))
    pkt.serialize()
    c.packet_in_handler(SimpleNamespace(msg=SimpleNamespace(
        datapath=c.dpid_conns[1], data=pkt.data, match={"in_port": 9})))
    outputs = [m for m in c.dpid_conns[1].messages if isinstance(m, parser.OFPPacketOut)]
    assert outputs[-1].actions[0].port == 7


def test_standby_never_installs_paths(controller):
    c = controller
    c.election = SimpleNamespace(is_primary=False)
    c.intents.add(KEY)
    c._recompute()
    assert all(not dp.messages for dp in c.dpid_conns.values())


def test_tx_packet_loss_uses_packets_not_bytes_and_handles_reset(controller):
    c = controller
    c.port_prev_stats[(1, 1)] = (time.monotonic() - 1, (0, 0, 0))
    c._update_port_utilization(1, SimpleNamespace(port_no=1, tx_bytes=10000, tx_packets=90, tx_dropped=10))
    assert c.graph.adj[1][2][1].loss_rate == pytest.approx(0.1)
    c._update_port_utilization(1, SimpleNamespace(port_no=1, tx_bytes=0, tx_packets=0, tx_dropped=0))
    assert c.graph.adj[1][2][1].utilization >= 0


@pytest.mark.parametrize("endpoint", ["topology", "health", "paths", "recovery-log", "metrics"])
def test_api_serializes_actual_state(controller, endpoint):
    api = SDNApi(Request.blank("/" + endpoint), None, {"sdn_app": controller})
    response = api.get(Request.blank("/" + endpoint), endpoint)
    assert response.status_int == 200
    if endpoint != "metrics":
        json.loads(response.text)


def test_openflow_errors_are_observable(controller):
    controller.error_handler(SimpleNamespace(msg=SimpleNamespace(
        datapath=controller.dpid_conns[1], type=1, code=2, data=b"bad")))
    assert controller.metrics.openflow_errors._value.get() == 1


def test_standby_retries_generation_race(controller):
    c = controller
    c.election = SimpleNamespace(is_primary=False, generation=0)
    c.error_handler(SimpleNamespace(msg=SimpleNamespace(
        datapath=c.dpid_conns[1], type=ofp.OFPET_ROLE_REQUEST_FAILED,
        code=ofp.OFPRRFC_STALE, data=b"")))
    assert c.dpid_conns[1].messages[-1].role == ofp.OFPCR_ROLE_NOCHANGE
    assert c.metrics.openflow_errors._value.get() == 0


def test_role_reply_uses_switch_generation_for_standby(controller):
    c = controller
    c.election = SimpleNamespace(is_primary=False, generation=0)
    c.role_reply_handler(SimpleNamespace(msg=SimpleNamespace(
        datapath=c.dpid_conns[1], role=ofp.OFPCR_ROLE_EQUAL, generation_id=29)))
    assert c.dpid_conns[1].messages[-1].role == ofp.OFPCR_ROLE_SLAVE
    assert c.dpid_conns[1].messages[-1].generation_id == 29
    assert 1 not in c.ready


def test_port_down_removes_host_and_route(controller):
    c = controller
    c.intents.add(KEY)
    c._recompute()
    c.port_status_handler(SimpleNamespace(msg=SimpleNamespace(
        datapath=c.dpid_conns[4], reason=ofp.OFPPR_MODIFY,
        desc=SimpleNamespace(port_no=7, state=ofp.OFPPS_LINK_DOWN, config=0))))
    assert KEY[1] not in c.graph.hosts
    assert KEY not in c.active_paths


def test_discovery_builds_real_lldp_and_learns_trunks(controller):
    c = controller
    for ports in c.ports.values():
        for desc in ports.values():
            desc.hw_addr = "00:11:22:33:44:55"
    c._send_discovery()
    probe = c.dpid_conns[1].messages[0]
    pkt = packet.Packet(probe.data)
    c.graph.remove_link(1, 2)
    c._learn_link(c.dpid_conns[2], 1, pkt)
    assert 2 in c.graph.adj[1]
    assert (1, 1) in c.trunk_ports


def test_disconnect_removes_switch_and_routes(controller):
    c = controller
    c.intents.add(KEY)
    c._recompute()
    c.disconnect_handler(SimpleNamespace(datapath=c.dpid_conns[4]))
    assert 4 not in c.graph.adj
    assert KEY not in c.active_paths

def test_switch_reconnect_does_not_retain_previous_port_roles(controller):
    c = controller
    c.port_prev_stats[(1, 1)] = (0, (1, 2, 3))
    c.link_seen[(1, 2)] = 1
    c.disconnect_handler(SimpleNamespace(datapath=c.dpid_conns[1]))
    assert all(key[0] != 1 for key in c.trunk_ports)
    assert all(key[0] != 1 for key in c.port_prev_stats)
    assert all(1 not in edge for edge in c.link_seen)
