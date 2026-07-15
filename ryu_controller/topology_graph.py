"""
topology_graph.py — Pure-Python, Ryu-independent topology and routing engine.

Responsibilities:
  - Maintain a weighted adjacency graph of switch-to-switch links
  - Track host-to-switch attachment points
  - Compute shortest paths via weighted Dijkstra
  - Compute ECMP (equal-cost multi-path) alternative sets
  - Expose a cost-function that combines latency, bandwidth, and loss rate
"""
from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Routing cost weights — tunable via constructor
# ---------------------------------------------------------------------------
DEFAULT_ALPHA = 0.5   # weight of latency
DEFAULT_BETA  = 0.3   # weight of inverse-bandwidth
DEFAULT_GAMMA = 0.2   # weight of loss rate
ECMP_COST_TOLERANCE = 0.05   # paths within 5 % of best are considered equal

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class LinkMetrics:
    latency_ms: float = 1.0          # measured LLDP round-trip / 2  (ms)
    bandwidth_mbps: float = 1000.0   # last sampled available BW      (Mbps)
    loss_rate: float = 0.0           # fraction [0, 1] from port drops
    utilization: float = 0.0         # fraction [0, 1] of link capacity in use

    def cost(self, alpha: float = DEFAULT_ALPHA,
             beta: float  = DEFAULT_BETA,
             gamma: float = DEFAULT_GAMMA) -> float:
        """Composite cost: lower is better."""
        bw_term = 1.0 / max(self.bandwidth_mbps, 0.001)
        return alpha * self.latency_ms + beta * bw_term + gamma * self.loss_rate


@dataclass
class HostLocation:
    dpid: int
    port: int


class TopologyGraph:
    """
    Thread-safe-ish topology graph for use inside the Ryu event loop.
    All mutations happen in the single-threaded Ryu event dispatch —
    no locking needed for reads/writes from the controller hub thread.
    """

    def __init__(self,
                 alpha: float = DEFAULT_ALPHA,
                 beta:  float = DEFAULT_BETA,
                 gamma: float = DEFAULT_GAMMA,
                 ecmp_tolerance: float = ECMP_COST_TOLERANCE):
        # dpid -> {neighbor_dpid -> (out_port, LinkMetrics)}
        self.adj: Dict[int, Dict[int, Tuple[int, LinkMetrics]]] = {}
        # mac_str -> HostLocation
        self.hosts: Dict[str, HostLocation] = {}
        self.alpha = alpha
        self.beta  = beta
        self.gamma = gamma
        self.ecmp_tolerance = ecmp_tolerance

    # ------------------------------------------------------------------
    # Graph mutations
    # ------------------------------------------------------------------

    def add_switch(self, dpid: int) -> None:
        if dpid not in self.adj:
            self.adj[dpid] = {}
            log.debug("Graph: added switch %016x", dpid)

    def add_link(self, dpid1: int, port1: int,
                 dpid2: int, port2: int,
                 metrics: Optional[LinkMetrics] = None) -> None:
        if metrics is None:
            metrics = LinkMetrics()
        self.add_switch(dpid1)
        self.add_switch(dpid2)
        self.adj[dpid1][dpid2] = (port1, metrics)
        self.adj[dpid2][dpid1] = (port2, metrics)
        log.debug("Graph: link %016x:%d <-> %016x:%d  cost=%.3f",
                  dpid1, port1, dpid2, port2, metrics.cost())

    def remove_link(self, dpid1: int, dpid2: int) -> None:
        self.adj.get(dpid1, {}).pop(dpid2, None)
        self.adj.get(dpid2, {}).pop(dpid1, None)
        log.debug("Graph: removed link %016x <-> %016x", dpid1, dpid2)

    def update_metrics(self, dpid1: int, dpid2: int,
                       metrics: LinkMetrics) -> None:
        """Update link cost metrics in both directions."""
        if dpid2 in self.adj.get(dpid1, {}):
            port1, _ = self.adj[dpid1][dpid2]
            port2, _ = self.adj[dpid2][dpid1]
            self.adj[dpid1][dpid2] = (port1, metrics)
            self.adj[dpid2][dpid1] = (port2, metrics)

    def learn_host(self, mac: str, dpid: int, port: int) -> None:
        self.hosts[mac] = HostLocation(dpid=dpid, port=port)
        log.debug("Graph: host %s at switch %016x port %d", mac, dpid, port)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _link_cost(self, dpid: int, neighbor: int) -> float:
        entry = self.adj.get(dpid, {}).get(neighbor)
        if entry is None:
            return float("inf")
        _, metrics = entry
        return metrics.cost(self.alpha, self.beta, self.gamma)

    def weighted_dijkstra(self, src: int, dst: int
                          ) -> Optional[List[int]]:
        """Return the lowest-cost path [src, ..., dst] or None."""
        if src == dst:
            return [src]
        dist: Dict[int, float] = {src: 0.0}
        prev: Dict[int, int]   = {}
        visited: set[int]      = set()
        heap: List[Tuple[float, int]] = [(0.0, src)]

        while heap:
            d, u = heapq.heappop(heap)
            if u in visited:
                continue
            visited.add(u)
            if u == dst:
                break
            for v in self.adj.get(u, {}):
                nd = d + self._link_cost(u, v)
                if v not in dist or nd < dist[v]:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))

        if dst not in dist:
            return None

        path: List[int] = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        path.reverse()
        return path

    def ecmp_paths(self, src: int, dst: int) -> List[List[int]]:
        """
        Return all paths whose cost is within ecmp_tolerance of the best.
        Uses a K-shortest-path style approach (Yen's first pass simplified).
        At most 4 paths are returned to keep group-table entries bounded.
        """
        best = self.weighted_dijkstra(src, dst)
        if best is None:
            return []
        best_cost = self._path_cost(best)
        threshold = best_cost * (1.0 + self.ecmp_tolerance)

        found: List[List[int]] = [best]
        # Simple BFS-style alternative: try removing each edge of the best path
        for i in range(len(best) - 1):
            u, v = best[i], best[i + 1]
            # Save the full edge state (both directions) BEFORE probing.
            # remove_link() deletes both adj[u][v] and adj[v][u]; once both
            # are gone there is no way to reconstruct port/metric info from
            # graph state alone, so we snapshot it ourselves instead of
            # relying on add_link_from_state's best-effort recovery.
            saved_uv = self.adj.get(u, {}).get(v)
            saved_vu = self.adj.get(v, {}).get(u)
            self.remove_link(u, v)
            alt = self.weighted_dijkstra(src, dst)
            # Restore exactly what was there before, unconditionally.
            if saved_uv is not None:
                self.adj.setdefault(u, {})[v] = saved_uv
            if saved_vu is not None:
                self.adj.setdefault(v, {})[u] = saved_vu
            if alt and self._path_cost(alt) <= threshold:
                if alt not in found:
                    found.append(alt)
            if len(found) >= 4:
                break
        return found

    def _path_cost(self, path: List[int]) -> float:
        return sum(self._link_cost(path[i], path[i + 1])
                   for i in range(len(path) - 1))

    def add_link_from_state(self, dpid1: int, dpid2: int) -> None:
        """Unused — kept for reference. See ecmp_paths() for the real restore logic.

        The original best-effort restore is unsafe: remove_link() deletes both
        adj[u][v] and adj[v][u] atomically, so by the time this is called both
        directions are already gone and the 'if e1 is None and e2 is None: return'
        guard always fires, silently leaving the edge deleted.  ecmp_paths() now
        snapshots both directions before calling remove_link and restores them
        unconditionally without going through this function.
        """
        pass  # no-op

    # ------------------------------------------------------------------
    # Path → port sequence
    # ------------------------------------------------------------------

    def path_to_port_sequence(self, path: List[int]) -> List[Tuple[int, int]]:
        """
        Given a path [dpid_a, dpid_b, dpid_c, ...], return a list of
        (dpid, out_port) pairs for installing flow rules.
        The last hop uses the destination host's attachment port
        (caller must append it separately).
        """
        result = []
        for i in range(len(path) - 1):
            dpid = path[i]
            next_dpid = path[i + 1]
            port, _ = self.adj[dpid][next_dpid]
            result.append((dpid, port))
        return result

    # ------------------------------------------------------------------
    # Serialisation (for Redis state sync)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        adj_serial = {}
        for dpid, neighbors in self.adj.items():
            adj_serial[str(dpid)] = {
                str(ndpid): {
                    "port": port,
                    "latency_ms": m.latency_ms,
                    "bandwidth_mbps": m.bandwidth_mbps,
                    "loss_rate": m.loss_rate,
                    "utilization": m.utilization,
                }
                for ndpid, (port, m) in neighbors.items()
            }
        hosts_serial = {
            mac: {"dpid": loc.dpid, "port": loc.port}
            for mac, loc in self.hosts.items()
        }
        return {"adj": adj_serial, "hosts": hosts_serial}

    @classmethod
    def from_dict(cls, data: dict,
                  alpha: float = DEFAULT_ALPHA,
                  beta:  float = DEFAULT_BETA,
                  gamma: float = DEFAULT_GAMMA) -> "TopologyGraph":
        g = cls(alpha=alpha, beta=beta, gamma=gamma)
        for dpid_str, neighbors in data.get("adj", {}).items():
            dpid = int(dpid_str)
            g.add_switch(dpid)
            for ndpid_str, info in neighbors.items():
                ndpid = int(ndpid_str)
                g.add_switch(ndpid)
                m = LinkMetrics(
                    latency_ms=info["latency_ms"],
                    bandwidth_mbps=info["bandwidth_mbps"],
                    loss_rate=info["loss_rate"],
                    utilization=info.get("utilization", 0.0),
                )
                g.adj[dpid][ndpid] = (info["port"], m)
        for mac, loc in data.get("hosts", {}).items():
            g.hosts[mac] = HostLocation(dpid=loc["dpid"], port=loc["port"])
        return g
