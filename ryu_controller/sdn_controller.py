"""OpenFlow 1.3 controller: loop-free discovery, weighted ECMP and fenced HA.

Use start_ryu.py. This app owns LLDP discovery; do not load ryu.topology
(--observe-links), whose independent writer is incompatible with slave roles.
"""
from __future__ import annotations

import json
import logging
import os
import struct
import time
from collections import deque

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, DEAD_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ethernet, lldp, packet
from ryu.ofproto import ofproto_v1_3
from webob import Response

from ha.backup import LeaderElection
from ryu_controller.group_allocator import GroupIdAllocator
from ryu_controller.metrics_exporter import SDNMetrics
from ryu_controller.topology_graph import LinkMetrics, TopologyGraph

log = logging.getLogger(__name__)
COOKIE = 0x53444E0000000000
COOKIE_MASK = 0xFFFFFF0000000000
FLOW_PRIORITY_PATH = 20
DISCOVERY_INTERVAL = 1
LINK_TIMEOUT = 4
STATS_INTERVAL = 5


class SDNApi(ControllerBase):
    def __init__(self, req, link, data, **config):
        super().__init__(req, link, data, **config)
        self.sdn = data["sdn_app"]

    @route("sdn", "/{endpoint}", methods=["GET"],
           requirements={"endpoint": "health|topology|paths|recovery-log|metrics"})
    def get(self, req, endpoint, **kwargs):
        if endpoint == "metrics":
            return Response(body=generate_latest(self.sdn.metrics.registry),
                            content_type=CONTENT_TYPE_LATEST)
        data = {
            "health": self.sdn.health,
            "topology": self.sdn.graph.to_dict,
            "paths": self.sdn.paths_json,
            "recovery-log": lambda: list(self.sdn.recovery_log),
        }[endpoint]()
        return Response(content_type="application/json", text=json.dumps(data))


class SDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.graph = TopologyGraph()
        self.dpid_conns = {}
        self.ports = {}
        self.ready = set()
        self.intents = set()
        self.active_paths = {}
        self.groups = {}
        self.group_alloc = GroupIdAllocator()
        self.port_prev_stats = {}
        self.link_seen = {}
        self.trunk_ports = set()
        self.recovery_log = deque(maxlen=1000)
        self.metrics = SDNMetrics()
        self.started = time.monotonic()
        self.capacity = float(os.getenv("LINK_CAPACITY_MBPS", "100"))
        if self.capacity <= 0:
            raise ValueError("LINK_CAPACITY_MBPS must be positive")
        role = os.getenv("CONTROLLER_ROLE", "standalone")
        if role not in ("standalone", "primary", "backup"):
            raise ValueError("CONTROLLER_ROLE must be standalone, primary or backup")
        self.election = None if role == "standalone" else LeaderElection(
            on_promote=self._promote, on_demote=self._demote)
        self.metrics.controller_role.set(self.is_primary)
        kwargs["wsgi"].register(SDNApi, {"sdn_app": self})
        self._worker = hub.spawn(self._maintenance)

    @property
    def is_primary(self):
        return self.election is None or self.election.is_primary

    def health(self):
        return {
            "status": "ok",
            "role": "primary" if self.is_primary else "backup",
            "mode": "standalone" if self.election is None else "ha",
            "switches": len(self.dpid_conns),
            "ready_switches": len(self.ready),
            "generation": self.election.generation if self.election else 0,
            "ha_error": self.election.last_error if self.election else None,
        }

    def paths_json(self):
        return [{"src_mac": s, "dst_mac": d, "paths": self.active_paths.get((s, d), []),
                 "status": "active" if (s, d) in self.active_paths else "unreachable"}
                for s, d in sorted(self.intents)]

    def _snapshot(self):
        return {"graph": self.graph.to_dict(), "intents": sorted(self.intents),
                "recovery_log": list(self.recovery_log)}

    def _promote(self, state):
        if state:
            self.graph = TopologyGraph.from_dict(state.get("graph", {}))
            self.intents = {tuple(key) for key in state.get("intents", [])}
            self.recovery_log = deque(state.get("recovery_log", []), maxlen=1000)
        now = time.monotonic()
        self.link_seen = {(a, b): now for a in self.graph.adj for b in self.graph.adj[a]}
        self.trunk_ports = {(a, p) for a in self.graph.adj for p, _ in self.graph.adj[a].values()}
        self.active_paths.clear()
        self.groups.clear()
        self.ready.clear()
        for dp in list(self.dpid_conns.values()):
            self._request_role(dp)

    def _demote(self):
        self.ready.clear()
        for dp in list(self.dpid_conns.values()):
            self._request_role(dp)

    def _request_role(self, dp):
        role = dp.ofproto.OFPCR_ROLE_MASTER if self.is_primary else dp.ofproto.OFPCR_ROLE_NOCHANGE
        generation = self.election.generation if self.election else 0
        # Read the switch's generation before requesting SLAVE. A restarted
        # standby does not yet know the current epoch; guessing causes STALE.
        dp.send_msg(dp.ofproto_parser.OFPRoleRequest(dp, role, generation))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        self.dpid_conns[dp.id] = dp
        self.graph.add_switch(dp.id)
        self.ports[dp.id] = {}
        dp.send_msg(dp.ofproto_parser.OFPPortDescStatsRequest(dp, 0))
        self._request_role(dp)

    @set_ev_cls(ofp_event.EventOFPRoleReply, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def role_reply_handler(self, ev):
        dp = ev.msg.datapath
        if not self.is_primary and ev.msg.role != dp.ofproto.OFPCR_ROLE_SLAVE:
            dp.send_msg(dp.ofproto_parser.OFPRoleRequest(
                dp, dp.ofproto.OFPCR_ROLE_SLAVE, ev.msg.generation_id))
        if ev.msg.role != dp.ofproto.OFPCR_ROLE_MASTER or not self.is_primary:
            self.ready.discard(dp.id)
            return
        self.ready.add(dp.id)
        ofp, par = dp.ofproto, dp.ofproto_parser
        # Dedicated lab switches: reconcile this app's flows and group table
        # on reconnect/promotion so unsynced old group IDs cannot survive.
        dp.send_msg(par.OFPFlowMod(dp, cookie=COOKIE, cookie_mask=COOKIE_MASK,
            command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))
        dp.send_msg(par.OFPGroupMod(dp, ofp.OFPGC_DELETE, ofp.OFPGT_ALL, ofp.OFPG_ALL))
        self.groups = {key: gid for key, gid in self.groups.items() if key[0] != dp.id}
        actions = [par.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        dp.send_msg(par.OFPFlowMod(dp, cookie=COOKIE, priority=0, match=par.OFPMatch(),
            instructions=[par.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]))
        self._recompute(reason="controller")
        self._send_discovery()

    @set_ev_cls(ofp_event.EventOFPStateChange, DEAD_DISPATCHER)
    def disconnect_handler(self, ev):
        dpid = ev.datapath.id
        if self.dpid_conns.get(dpid) is not ev.datapath:
            return
        self.dpid_conns.pop(dpid, None)
        self.ready.discard(dpid)
        self.ports.pop(dpid, None)
        self.graph.remove_switch(dpid)
        self._recompute(reason="switch_failure")

    @set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
    def port_desc_handler(self, ev):
        dp = ev.msg.datapath
        for desc in ev.msg.body:
            if desc.port_no < dp.ofproto.OFPP_MAX:
                self.ports.setdefault(dp.id, {})[desc.port_no] = desc
        if self.is_primary:
            self._send_discovery()

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status_handler(self, ev):
        dp, desc = ev.msg.datapath, ev.msg.desc
        if desc.port_no >= dp.ofproto.OFPP_MAX:
            return
        down = ev.msg.reason == dp.ofproto.OFPPR_DELETE or not self._port_up(dp, desc)
        if ev.msg.reason == dp.ofproto.OFPPR_DELETE:
            self.ports.get(dp.id, {}).pop(desc.port_no, None)
        else:
            self.ports.setdefault(dp.id, {})[desc.port_no] = desc
        if down:
            affected = set()
            for neighbor, (port, _) in list(self.graph.adj.get(dp.id, {}).items()):
                if port == desc.port_no:
                    affected |= self._affected(dp.id, neighbor)
                    self._remove_link(dp.id, neighbor)
            for mac, loc in list(self.graph.hosts.items()):
                if (loc.dpid, loc.port) == (dp.id, desc.port_no):
                    del self.graph.hosts[mac]
                    affected |= {key for key in self.intents if mac in key}
            self._recompute(affected, "failure")
        elif self.is_primary:
            self._send_discovery()

    @staticmethod
    def _port_up(dp, desc):
        return not (desc.state & dp.ofproto.OFPPS_LINK_DOWN or
                    desc.config & dp.ofproto.OFPPC_PORT_DOWN)

    def _send_discovery(self):
        if not self.is_primary:
            return
        for dpid in list(self.ready):
            dp = self.dpid_conns[dpid]
            for port, desc in list(self.ports.get(dpid, {}).items()):
                if not self._port_up(dp, desc):
                    continue
                pkt = packet.Packet()
                pkt.add_protocol(ethernet.ethernet(dst=lldp.LLDP_MAC_NEAREST_BRIDGE,
                                 src=desc.hw_addr, ethertype=0x88cc))
                pkt.add_protocol(lldp.lldp(tlvs=[
                    lldp.ChassisID(subtype=lldp.ChassisID.SUB_LOCALLY_ASSIGNED,
                                   chassis_id=("sdn:%d" % dpid).encode()),
                    lldp.PortID(subtype=lldp.PortID.SUB_PORT_COMPONENT,
                                port_id=struct.pack("!I", port)),
                    lldp.TTL(ttl=LINK_TIMEOUT), lldp.End()]))
                pkt.serialize()
                self._packet_out(dp, pkt.data, dp.ofproto.OFPP_CONTROLLER, [port])

    def _learn_link(self, dp, in_port, pkt):
        probe = pkt.get_protocol(lldp.lldp)
        if not probe or len(probe.tlvs) < 3:
            return
        try:
            chassis = probe.tlvs[0].chassis_id.decode()
            if not chassis.startswith("sdn:"):
                return
            src = int(chassis[4:])
            src_port = struct.unpack("!I", probe.tlvs[1].port_id)[0]
        except (AttributeError, ValueError, UnicodeError, struct.error):
            return
        if src == dp.id or src not in self.ready:
            return
        changed = dp.id not in self.graph.adj.get(src, {})
        if changed:
            self.graph.add_link(src, src_port, dp.id, in_port,
                                LinkMetrics(bandwidth_mbps=self.capacity))
        self.link_seen[(src, dp.id)] = time.monotonic()
        self.trunk_ports.update(((src, src_port), (dp.id, in_port)))
        for mac, loc in list(self.graph.hosts.items()):
            if (loc.dpid, loc.port) in self.trunk_ports:
                del self.graph.hosts[mac]
        for a, b in ((src, dp.id), (dp.id, src)):
            self.metrics.link_up.labels(str(a), str(b)).set(1)
        if changed:
            self._recompute(reason="restoration")

    def _remove_link(self, a, b):
        self.graph.remove_link(a, b)
        self.link_seen.pop((a, b), None)
        self.link_seen.pop((b, a), None)
        for src, dst in ((a, b), (b, a)):
            self.metrics.link_up.labels(str(src), str(dst)).set(0)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg, dp = ev.msg, ev.msg.datapath
        if not self.is_primary or dp.id not in self.ready:
            return
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return
        if eth.ethertype == 0x88cc:
            self._learn_link(dp, in_port, pkt)
            return
        if int(eth.src.split(":")[0], 16) & 1:
            return
        if (dp.id, in_port) not in self.trunk_ports:
            previous = self.graph.hosts.get(eth.src)
            self.graph.learn_host(eth.src, dp.id, in_port)
            if previous and (previous.dpid, previous.port) != (dp.id, in_port):
                self._recompute({key for key in self.intents if eth.src in key}, "host_move")
        if int(eth.dst.split(":")[0], 16) & 1:
            self._flood_access(dp, msg.data, in_port)
            return
        src, dst = self.graph.hosts.get(eth.src), self.graph.hosts.get(eth.dst)
        if not src or not dst:
            self._flood_access(dp, msg.data, in_port)
            return
        key = (eth.src, eth.dst)
        self.intents.add(key)
        self._recompute({key}, "packet")
        paths = self.active_paths.get(key, [])
        for path in paths:
            if dp.id in path:
                index = path.index(dp.id)
                out_port = dst.port if index == len(path) - 1 else self.graph.adj[dp.id][path[index + 1]][0]
                self._packet_out(dp, msg.data, in_port, [out_port])
                break

    def _flood_access(self, ingress, data, in_port):
        # Replicate once at each reachable access switch. Never emit to trunks;
        # cyclic topologies need neither flooding rules nor STP convergence.
        if (ingress.id, in_port) in self.trunk_ports:
            return
        for dpid in list(self.ready):
            if self.graph.weighted_dijkstra(ingress.id, dpid) is None:
                continue
            dp = self.dpid_conns[dpid]
            ports = [p for p, desc in self.ports.get(dpid, {}).items()
                     if (dpid, p) not in self.trunk_ports and self._port_up(dp, desc)
                     and (dpid, p) != (ingress.id, in_port)]
            if ports:
                self._packet_out(dp, data, dp.ofproto.OFPP_CONTROLLER, ports)

    def _packet_out(self, dp, data, in_port, ports):
        if not self.is_primary or dp.id not in self.ready:
            return
        par = dp.ofproto_parser
        dp.send_msg(par.OFPPacketOut(dp, buffer_id=dp.ofproto.OFP_NO_BUFFER,
            in_port=in_port, actions=[par.OFPActionOutput(p) for p in ports], data=data))

    @staticmethod
    def _path_uses_link(path, a, b):
        return any({u, v} == {a, b} for u, v in zip(path, path[1:]))

    def _affected(self, a, b):
        return {key for key, paths in self.active_paths.items()
                if any(self._path_uses_link(path, a, b) for path in paths)}

    def _recompute(self, keys=None, reason="failure"):
        if not self.is_primary:
            return
        for key in list(self.intents if keys is None else keys):
            started = time.monotonic()
            src, dst = (self.graph.hosts.get(mac) for mac in key)
            paths = self.graph.ecmp_paths(src.dpid, dst.dpid) if src and dst else []
            paths = [path for path in paths if all(dpid in self.ready for dpid in path)]
            old = self.active_paths.get(key, [])
            if paths == old and reason not in ("packet", "controller", "host_move"):
                continue
            self._replace_routes(key, paths, dst.port if dst else None)
            if paths:
                self.active_paths[key] = paths
            else:
                self.active_paths.pop(key, None)
            if reason != "packet" and (old or paths):
                elapsed = (time.monotonic() - started) * 1000
                self.recovery_log.append({
                    "src_mac": key[0], "dst_mac": key[1], "reason": reason,
                    "status": "rerouted" if paths else "unreachable",
                    "recovery_ms": elapsed, "measurement": "controller_enqueue",
                    "new_path": paths[0] if paths else [], "paths": paths, "ts": time.time(),
                })
                if paths:
                    self.metrics.recovery_time_ms.observe(elapsed)
        self.metrics.active_flows.set(sum(len({n for path in paths for n in path})
                                         for paths in self.active_paths.values()))

    def _replace_routes(self, key, paths, dst_port):
        outputs = {}
        rank = {}
        for path in paths:
            for index, dpid in enumerate(path):
                port = dst_port if index == len(path) - 1 else self.graph.adj[dpid][path[index + 1]][0]
                outputs.setdefault(dpid, set()).add(port)
                rank[dpid] = max(rank.get(dpid, 0), len(path) - index)
        # Send downstream rules before ingress rules; timing is explicitly
        # enqueue time, since OpenFlow 1.3 is not a cross-switch transaction.
        for dpid in sorted(outputs, key=lambda n: rank[n]):
            dp = self.dpid_conns[dpid]
            ofp, par = dp.ofproto, dp.ofproto_parser
            group_key = (dpid, *key)
            ports = sorted(outputs[dpid])
            if len(ports) > 1:
                gid = self.groups.get(group_key)
                command = ofp.OFPGC_MODIFY if gid is not None else ofp.OFPGC_ADD
                if gid is None:
                    gid = self.group_alloc.allocate((str(dpid) + "|" + key[0], key[1]))
                    self.groups[group_key] = gid
                buckets = [par.OFPBucket(weight=1, watch_port=ofp.OFPP_ANY,
                    watch_group=ofp.OFPG_ANY, actions=[par.OFPActionOutput(p)]) for p in ports]
                dp.send_msg(par.OFPGroupMod(dp, command, ofp.OFPGT_SELECT, gid, buckets))
                actions = [par.OFPActionGroup(gid)]
            else:
                actions = [par.OFPActionOutput(ports[0])]
            dp.send_msg(par.OFPFlowMod(dp, cookie=COOKIE, priority=FLOW_PRIORITY_PATH,
                match=par.OFPMatch(eth_src=key[0], eth_dst=key[1]),
                instructions=[par.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]))
            self.metrics.flow_install_total.inc()
        old_switches = {n for path in self.active_paths.get(key, []) for n in path}
        for dpid in old_switches - outputs.keys():
            dp = self.dpid_conns.get(dpid)
            if dp and dpid in self.ready:
                ofp, par = dp.ofproto, dp.ofproto_parser
                dp.send_msg(par.OFPFlowMod(dp, cookie=COOKIE, cookie_mask=COOKIE_MASK,
                    command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                    match=par.OFPMatch(eth_src=key[0], eth_dst=key[1])))
        for group_key, gid in list(self.groups.items()):
            dpid, src, dst = group_key
            if (src, dst) == key and len(outputs.get(dpid, [])) < 2:
                dp = self.dpid_conns.get(dpid)
                if dp and dpid in self.ready:
                    dp.send_msg(dp.ofproto_parser.OFPGroupMod(dp, dp.ofproto.OFPGC_DELETE,
                                dp.ofproto.OFPGT_SELECT, gid))
                del self.groups[group_key]
                self.group_alloc.release((str(dpid) + "|" + src, dst))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        if not self.is_primary:
            return
        changed = set()
        for stat in ev.msg.body:
            neighbor = self._update_port_utilization(ev.msg.datapath.id, stat)
            if neighbor is not None:
                changed |= self._affected(ev.msg.datapath.id, neighbor)
        if changed:
            self._recompute(changed, "congestion")

    def _update_port_utilization(self, dpid, stat):
        now = time.monotonic()
        key = (dpid, stat.port_no)
        values = (stat.tx_bytes, stat.tx_packets, stat.tx_dropped)
        previous = self.port_prev_stats.get(key)
        self.port_prev_stats[key] = (now, values)
        if previous is None or now <= previous[0]:
            return None
        delta = [current - old for current, old in zip(values, previous[1])]
        if any(value < 0 for value in delta):
            return None
        for neighbor, (port, metrics) in self.graph.adj.get(dpid, {}).items():
            if port != stat.port_no:
                continue
            utilization = min(1.0, delta[0] * 8 / ((now - previous[0]) * self.capacity * 1e6))
            loss = delta[2] / max(delta[1] + delta[2], 1)
            self.graph.adj[dpid][neighbor] = (port, LinkMetrics(
                latency_ms=metrics.latency_ms, bandwidth_mbps=self.capacity,
                utilization=utilization, loss_rate=loss))
            self.metrics.link_utilization.labels(str(dpid), str(neighbor)).set(utilization)
            if abs(utilization - metrics.utilization) >= 0.15:
                return neighbor
        return None

    @set_ev_cls(ofp_event.EventOFPErrorMsg, [CONFIG_DISPATCHER, MAIN_DISPATCHER])
    def error_handler(self, ev):
        dp = ev.msg.datapath
        if (not self.is_primary and ev.msg.type == dp.ofproto.OFPET_ROLE_REQUEST_FAILED
                and ev.msg.code == dp.ofproto.OFPRRFC_STALE):
            # Another controller may promote between our NOCHANGE query and
            # SLAVE request. Refresh the epoch and retry this expected race.
            self._request_role(dp)
            return
        self.metrics.openflow_errors.inc()
        log.error("OpenFlow error switch=%s type=%s code=%s data=%r",
                  ev.msg.datapath.id, ev.msg.type, ev.msg.code, ev.msg.data)

    def _maintenance(self):
        iteration = 0
        while self.is_active:
            if self.election:
                self.election.tick()
                if self.is_primary:
                    try:
                        if not self.election.sync.push_state(self.election.instance_id, self._snapshot()):
                            self.election.demote()
                    except Exception:
                        self.election.demote()
            self.metrics.controller_role.set(self.is_primary)
            if self.is_primary:
                self._send_discovery()
                now = time.monotonic()
                for (a, b), seen in list(self.link_seen.items()):
                    if now - seen > LINK_TIMEOUT:
                        affected = self._affected(a, b)
                        self._remove_link(a, b)
                        self._recompute(affected, "discovery_timeout")
                if iteration % STATS_INTERVAL == 0:
                    for dpid in list(self.ready):
                        dp = self.dpid_conns[dpid]
                        dp.send_msg(dp.ofproto_parser.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY))
            iteration += 1
            hub.sleep(DISCOVERY_INTERVAL)

    def close(self):
        if self.election:
            self.election.stop()
        if self._worker:
            hub.kill(self._worker)
        super().close()
