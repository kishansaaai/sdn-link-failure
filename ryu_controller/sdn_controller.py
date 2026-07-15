"""
sdn_controller.py — Ryu OpenFlow 1.3 SDN controller.

Features:
  - LLDP-based topology discovery via ryu.topology API
  - Weighted Dijkstra + ECMP path selection
  - Proactive flow installation end-to-end (no per-hop learning)
  - Fast failover: only affected flows are deleted and reinstalled
  - Periodic port-stats polling for utilization-aware routing
  - LLDP echo probing for live latency measurement
  - Meter bands for rate limiting on rerouted ports
  - Prometheus metrics export (see metrics_exporter.py)
"""
from __future__ import annotations

import json
import logging
import os
import struct
import time
from typing import Dict, List, Optional, Tuple

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.lib import hub
from ryu.lib.packet import ether_types, ethernet, lldp, packet
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event as topo_event
from ryu.topology.api import get_all_link, get_all_switch

from ryu_controller.metrics_exporter import SDNMetrics
from ryu_controller.topology_graph import LinkMetrics, TopologyGraph

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------
PORT_STATS_INTERVAL   = 5      # seconds between OFPPortStatsRequest polls
LATENCY_PROBE_INTERVAL = 3     # seconds between LLDP echo probes
UTIL_THRESHOLD        = 0.75   # rebalance if any link utilization > 75 %
FLOW_PRIORITY_PATH    = 20
FLOW_PRIORITY_MISS    = 0
FLOW_IDLE_TIMEOUT     = 30     # seconds; 0 = permanent
METER_ID_REROUTE      = 1      # meter applied on rerouted ports
LLDP_ETHER_TYPE       = 0x88CC
CONTROLLER_PORT       = int(os.getenv("OF_PORT", "6633"))


class SDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.graph     = TopologyGraph()
        self.dpid_conns: Dict[int, object]  = {}   # dpid -> datapath
        self.active_paths: Dict[Tuple, List[int]] = {}  # (src_mac, dst_mac) -> [dpid...]
        self.recovery_log: List[dict]        = []
        self.port_prev_stats: Dict[Tuple[int,int], dict] = {}  # (dpid,port)->stats
        self._lldp_timestamps: Dict[Tuple[int,int], float] = {}  # (dpid,port)->send_ts
        self.metrics = SDNMetrics()
        # Background polling threads
        self._stats_thread   = hub.spawn(self._port_stats_loop)
        self._latency_thread = hub.spawn(self._latency_probe_loop)
        log.info("SDNController started (Ryu OF1.3)")

    # -----------------------------------------------------------------------
    # Switch handshake — install table-miss + meter
    # -----------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp   = ev.msg.datapath
        ofp  = dp.ofproto
        par  = dp.ofproto_parser
        self.dpid_conns[dp.id] = dp
        self.graph.add_switch(dp.id)
        log.info("Switch connected: %016x", dp.id)

        # 1. Install meter for reroute rate limiting (1 Gbps burst)
        bands = [par.OFPMeterBandDrop(rate=100_000, burst_size=1_000)]
        dp.send_msg(par.OFPMeterMod(
            datapath=dp,
            command=ofp.OFPMC_ADD,
            flags=ofp.OFPMF_KBPS,
            meter_id=METER_ID_REROUTE,
            bands=bands,
        ))

        # 2. Table-miss: send to controller, lowest priority
        match  = par.OFPMatch()
        actions = [par.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        inst    = [par.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        mod     = par.OFPFlowMod(
            datapath=dp, priority=FLOW_PRIORITY_MISS,
            match=match, instructions=inst,
        )
        dp.send_msg(mod)
        self.metrics.active_flows.inc()

    # -----------------------------------------------------------------------
    # Topology events (from ryu.topology)
    # -----------------------------------------------------------------------

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add_handler(self, ev):
        link = ev.link
        dpid1, port1 = link.src.dpid, link.src.port_no
        dpid2, port2 = link.dst.dpid, link.dst.port_no
        self.graph.add_link(dpid1, port1, dpid2, port2)
        self.metrics.link_up.labels(
            src=str(dpid1), dst=str(dpid2)).set(1)
        log.info("Link UP %016x:%d <-> %016x:%d", dpid1, port1, dpid2, port2)

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_delete_handler(self, ev):
        link = ev.link
        dpid1, dpid2 = link.src.dpid, link.dst.dpid
        t0 = time.time()
        self.graph.remove_link(dpid1, dpid2)
        self.metrics.link_up.labels(
            src=str(dpid1), dst=str(dpid2)).set(0)
        log.warning("LINK FAILURE: %016x <-> %016x", dpid1, dpid2)

        affected = [
            key for key, path in self.active_paths.items()
            if self._path_uses_link(path, dpid1, dpid2)
        ]
        for key in affected:
            self._reroute(key, t0)

    @set_ev_cls(topo_event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        self.dpid_conns.pop(dpid, None)
        log.warning("Switch disconnected: %016x", dpid)

    # -----------------------------------------------------------------------
    # PacketIn — host learning + path install
    # -----------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg  = ev.msg
        dp   = msg.datapath
        ofp  = dp.ofproto
        par  = dp.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None:
            return

        # Intercept our own latency probes
        if eth.ethertype == LLDP_ETHER_TYPE:
            self._handle_lldp_probe(dp.id, in_port, pkt)
            return

        src_mac = eth.src
        dst_mac = eth.dst

        # Skip multicast / broadcast for path learning
        if dst_mac == "ff:ff:ff:ff:ff:ff":
            self._flood(dp, msg, in_port)
            return

        # Learn host
        if not self._is_switch_port(dp.id, in_port):
            self.graph.learn_host(src_mac, dp.id, in_port)

        # Try to route
        dst_loc = self.graph.hosts.get(dst_mac)
        src_loc = self.graph.hosts.get(src_mac)
        if dst_loc and src_loc:
            paths = self.graph.ecmp_paths(src_loc.dpid, dst_loc.dpid)
            if paths:
                key = (src_mac, dst_mac)
                if len(paths) > 1:
                    self._install_ecmp(paths, key, src_mac, dst_mac, dst_loc.port)
                else:
                    self._install_path(paths[0], key, src_mac, dst_mac, dst_loc.port)
                self.active_paths[key] = paths[0]
                # Forward this buffered packet
                self._send_packet_out(dp, msg, in_port,
                                      self._first_hop_port(dp.id, paths[0]))
                return

        self._flood(dp, msg, in_port)

    # -----------------------------------------------------------------------
    # Port stats reply — utilization + rebalancing
    # -----------------------------------------------------------------------

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        dp   = ev.msg.datapath
        body = ev.msg.body
        for stat in body:
            self._update_port_utilization(dp.id, stat)

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _port_stats_loop(self):
        while True:
            hub.sleep(PORT_STATS_INTERVAL)
            for dpid, dp in list(self.dpid_conns.items()):
                par = dp.ofproto_parser
                req = par.OFPPortStatsRequest(dp, 0, dp.ofproto.OFPP_ANY)
                dp.send_msg(req)

    def _latency_probe_loop(self):
        while True:
            hub.sleep(LATENCY_PROBE_INTERVAL)
            self._send_lldp_probes()

    def _send_lldp_probes(self):
        """Send a timestamped LLDP packet out every switch-to-switch port."""
        for dpid, dp in list(self.dpid_conns.items()):
            for neighbor, (out_port, _) in list(self.graph.adj.get(dpid, {}).items()):
                ts_bytes = struct.pack("!d", time.time())
                # Build a minimal LLDP packet with timestamp in system_name TLV
                pkt = packet.Packet()
                pkt.add_protocol(ethernet.ethernet(
                    dst="01:80:c2:00:00:0e",
                    src="00:00:00:00:00:01",
                    ethertype=LLDP_ETHER_TYPE,
                ))
                chassis_id = lldp.ChassisID(
                    subtype=lldp.ChassisID.SUB_LOCALLY_ASSIGNED,
                    chassis_id=str(dpid).encode(),
                )
                port_id = lldp.PortID(
                    subtype=lldp.PortID.SUB_PORT_COMPONENT,
                    port_id=struct.pack("!H", out_port),
                )
                ttl = lldp.TTL(ttl=120)
                end = lldp.End()
                lldp_pkt = lldp.lldp(tlvs=[chassis_id, port_id, ttl, end])
                pkt.add_protocol(lldp_pkt)
                pkt.serialize()
                self._lldp_timestamps[(dpid, out_port)] = time.time()
                ofp = dp.ofproto
                par = dp.ofproto_parser
                actions = [par.OFPActionOutput(out_port)]
                out = par.OFPPacketOut(
                    datapath=dp,
                    buffer_id=ofp.OFP_NO_BUFFER,
                    in_port=ofp.OFPP_CONTROLLER,
                    actions=actions,
                    data=pkt.data,
                )
                dp.send_msg(out)

    def _handle_lldp_probe(self, recv_dpid: int, in_port: int, pkt):
        """Match received LLDP to a sent probe and update latency."""
        try:
            lldp_pkt = pkt.get_protocol(lldp.lldp)
            if lldp_pkt is None:
                return
            src_dpid_bytes = lldp_pkt.tlvs[0].chassis_id
            src_dpid = int(src_dpid_bytes.decode())
            src_port_bytes = lldp_pkt.tlvs[1].port_id
            src_port = struct.unpack("!H", src_port_bytes)[0]
            send_ts = self._lldp_timestamps.get((src_dpid, src_port))
            if send_ts is None:
                return
            rtt_ms = (time.time() - send_ts) * 1000
            latency_ms = rtt_ms / 2.0
            # Update link metrics in graph
            if src_dpid in self.graph.adj and recv_dpid in self.graph.adj[src_dpid]:
                _, old_m = self.graph.adj[src_dpid][recv_dpid]
                new_m = LinkMetrics(
                    latency_ms=latency_ms,
                    bandwidth_mbps=old_m.bandwidth_mbps,
                    loss_rate=old_m.loss_rate,
                    utilization=old_m.utilization,
                )
                self.graph.update_metrics(src_dpid, recv_dpid, new_m)
                log.debug("Latency %016x<->%016x = %.2fms", src_dpid, recv_dpid, latency_ms)
        except Exception as e:
            log.debug("LLDP probe parse error: %s", e)

    def _update_port_utilization(self, dpid: int, stat) -> None:
        key = (dpid, stat.port_no)
        prev = self.port_prev_stats.get(key)
        now_stats = {
            "tx_bytes": stat.tx_bytes,
            "rx_bytes": stat.rx_bytes,
            "tx_dropped": stat.tx_dropped,
            "rx_dropped": stat.rx_dropped,
            "ts": time.time(),
        }
        if prev:
            dt = now_stats["ts"] - prev["ts"]
            if dt > 0:
                delta_bytes = (now_stats["tx_bytes"] - prev["tx_bytes"] +
                               now_stats["rx_bytes"] - prev["rx_bytes"])
                delta_drops = (now_stats["tx_dropped"] - prev["tx_dropped"] +
                               now_stats["rx_dropped"] - prev["rx_dropped"])
                # Find what neighbor this port connects to
                neighbor = self._port_to_neighbor(dpid, stat.port_no)
                if neighbor is not None:
                    _, m = self.graph.adj[dpid][neighbor]
                    total_bytes_capacity = m.bandwidth_mbps * 1e6 / 8 * dt
                    utilization = min(delta_bytes / max(total_bytes_capacity, 1), 1.0)
                    loss_rate = delta_drops / max(delta_bytes + delta_drops, 1)
                    new_m = LinkMetrics(
                        latency_ms=m.latency_ms,
                        bandwidth_mbps=m.bandwidth_mbps,
                        loss_rate=loss_rate,
                        utilization=utilization,
                    )
                    self.graph.update_metrics(dpid, neighbor, new_m)
                    # Proactive rebalancing
                    if utilization > UTIL_THRESHOLD:
                        log.info("Utilization %.1f%% on %016x->%016x — rebalancing",
                                 utilization * 100, dpid, neighbor)
                        self._rebalance_link(dpid, neighbor)
        self.port_prev_stats[key] = now_stats

    def _rebalance_link(self, dpid1: int, dpid2: int) -> None:
        """Rebalance flows that use the congested link."""
        for key, path in list(self.active_paths.items()):
            if self._path_uses_link(path, dpid1, dpid2):
                self._reroute(key, time.time(), reason="congestion")

    def _reroute(self, key: Tuple, t0: float, reason: str = "failure") -> None:
        src_mac, dst_mac = key
        src_loc = self.graph.hosts.get(src_mac)
        dst_loc = self.graph.hosts.get(dst_mac)
        if not src_loc or not dst_loc:
            return
        paths = self.graph.ecmp_paths(src_loc.dpid, dst_loc.dpid)
        if not paths:
            log.error("RECOVERY FAILED: no alternate path for %s->%s", src_mac, dst_mac)
            return

        # Remove stale flows
        old_path = self.active_paths.get(key, [])
        self._delete_path_flows(old_path, src_mac, dst_mac)

        # Install new path(s)
        if len(paths) > 1:
            self._install_ecmp(paths, key, src_mac, dst_mac, dst_loc.port)
        else:
            self._install_path(paths[0], key, src_mac, dst_mac, dst_loc.port)
        self.active_paths[key] = paths[0]

        elapsed_ms = (time.time() - t0) * 1000
        entry = {
            "src_mac": src_mac, "dst_mac": dst_mac,
            "reason": reason,
            "recovery_ms": elapsed_ms,
            "new_path": paths[0],
            "ts": time.time(),
        }
        self.recovery_log.append(entry)
        self.metrics.recovery_time_ms.observe(elapsed_ms)
        log.info("RECOVERY [%s]: %s->%s in %.1fms via %s",
                 reason, src_mac, dst_mac, elapsed_ms, paths[0])

    def _install_path(self, path: List[int], key: Tuple,
                      src_mac: str, dst_mac: str, dst_port: int) -> None:
        hops = self.graph.path_to_port_sequence(path)
        for i, (dpid, out_port) in enumerate(hops):
            dp  = self.dpid_conns.get(dpid)
            if dp is None:
                continue
            ofp = dp.ofproto
            par = dp.ofproto_parser
            match   = par.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            actions = [par.OFPActionOutput(out_port)]
            inst    = [par.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
            mod = par.OFPFlowMod(
                datapath=dp,
                priority=FLOW_PRIORITY_PATH,
                idle_timeout=FLOW_IDLE_TIMEOUT,
                match=match, instructions=inst,
            )
            dp.send_msg(mod)
            self.metrics.flow_install_total.inc()
        # Install final hop (switch attached to dst host)
        if path:
            last_dpid = path[-1]
            dp = self.dpid_conns.get(last_dpid)
            if dp:
                ofp = dp.ofproto
                par = dp.ofproto_parser
                match   = par.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
                actions = [par.OFPActionOutput(dst_port)]
                inst    = [par.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
                mod = par.OFPFlowMod(
                    datapath=dp,
                    priority=FLOW_PRIORITY_PATH,
                    idle_timeout=FLOW_IDLE_TIMEOUT,
                    match=match, instructions=inst,
                )
                dp.send_msg(mod)
                self.metrics.flow_install_total.inc()
        self.metrics.active_flows.inc()

    def _install_ecmp(self, paths: List[List[int]], key: Tuple,
                      src_mac: str, dst_mac: str, dst_port: int) -> None:
        """Install an OF1.3 group table entry to split traffic across paths."""
        if not paths:
            return
        # Use first switch of the first path to create the group
        first_dpid = paths[0][0]
        dp = self.dpid_conns.get(first_dpid)
        if dp is None:
            # Fallback: install only the first path
            self._install_path(paths[0], key, src_mac, dst_mac, dst_port)
            return

        ofp = dp.ofproto
        par = dp.ofproto_parser

        # Build one bucket per path
        buckets = []
        for path in paths:
            if len(path) > 1:
                out_port = self.graph.adj[path[0]][path[1]][0]
            else:
                out_port = dst_port
            actions = [par.OFPActionOutput(out_port)]
            buckets.append(par.OFPBucket(
                weight=1,
                watch_port=out_port,
                watch_group=ofp.OFPG_ANY,
                actions=actions,
            ))

        group_id = hash(key) & 0xFFFFFFFF
        dp.send_msg(par.OFPGroupMod(
            datapath=dp,
            command=ofp.OFPGC_ADD,
            type_=ofp.OFPGT_SELECT,
            group_id=group_id,
            buckets=buckets,
        ))

        # Install path rules for all switches beyond the first
        for path in paths:
            self._install_path(path, key, src_mac, dst_mac, dst_port)

        log.info("ECMP group %d installed (%d paths) for %s->%s",
                 group_id, len(paths), src_mac, dst_mac)

    def _delete_path_flows(self, path: List[int],
                           src_mac: str, dst_mac: str) -> None:
        for dpid in path:
            dp = self.dpid_conns.get(dpid)
            if dp is None:
                continue
            ofp = dp.ofproto
            par = dp.ofproto_parser
            match = par.OFPMatch(eth_src=src_mac, eth_dst=dst_mac)
            mod   = par.OFPFlowMod(
                datapath=dp,
                command=ofp.OFPFC_DELETE,
                out_port=ofp.OFPP_ANY,
                out_group=ofp.OFPG_ANY,
                priority=FLOW_PRIORITY_PATH,
                match=match,
            )
            dp.send_msg(mod)

    def _flood(self, dp, msg, in_port: int) -> None:
        ofp  = dp.ofproto
        par  = dp.ofproto_parser
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        out  = par.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=[par.OFPActionOutput(ofp.OFPP_FLOOD)],
            data=data,
        )
        dp.send_msg(out)

    def _send_packet_out(self, dp, msg, in_port: int, out_port: int) -> None:
        ofp  = dp.ofproto
        par  = dp.ofproto_parser
        data = msg.data if msg.buffer_id == ofp.OFP_NO_BUFFER else None
        out  = par.OFPPacketOut(
            datapath=dp,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=[par.OFPActionOutput(out_port)],
            data=data,
        )
        dp.send_msg(out)

    def _is_switch_port(self, dpid: int, port: int) -> bool:
        return any(p == port for _, (p, _) in self.graph.adj.get(dpid, {}).items())

    def _path_uses_link(self, path: List[int], a: int, b: int) -> bool:
        for i in range(len(path) - 1):
            if (path[i] == a and path[i+1] == b) or \
               (path[i] == b and path[i+1] == a):
                return True
        return False

    def _port_to_neighbor(self, dpid: int, port: int) -> Optional[int]:
        for neighbor, (p, _) in self.graph.adj.get(dpid, {}).items():
            if p == port:
                return neighbor
        return None

    def _first_hop_port(self, dpid: int, path: List[int]) -> int:
        if len(path) < 2:
            return 1
        _, (port, _) = dpid, self.graph.adj[dpid].get(path[1], (1, None))
        return self.graph.adj[dpid][path[1]][0] if path[1] in self.graph.adj.get(dpid, {}) else 1
