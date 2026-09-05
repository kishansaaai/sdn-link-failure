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
import math
from dataclasses import dataclass
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

    def __post_init__(self):
        values = (self.latency_ms, self.bandwidth_mbps, self.loss_rate, self.utilization)
        if any(not math.isfinite(v) or v < 0 for v in values):
            raise ValueError("Link metrics must be finite and nonnegative")
        if self.loss_rate > 1 or self.utilization > 1:
            raise ValueError("Loss and utilization must be fractions in [0, 1]")

    def cost(self, alpha: float = DEFAULT_ALPHA,
             beta: float  = DEFAULT_BETA,
             gamma: float = DEFAULT_GAMMA) -> float:
        """Composite cost: lower is better."""
        bw_term = 1.0 / max(self.bandwidth_mbps * (1 - self.utilization), 0.001)
        return max(alpha * self.latency_ms + beta * bw_term + gamma * self.loss_rate, 1e-9)


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
        if any(not math.isfinite(v) or v < 0 for v in (alpha, beta, gamma, ecmp_tolerance)):
            raise ValueError("Routing weights and tolerance must be finite and nonnegative")

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

    def remove_switch(self, dpid: int) -> None:
        for neighbor in list(self.adj.get(dpid, {})):
            self.remove_link(dpid, neighbor)
        self.adj.pop(dpid, None)
        self.hosts = {mac: loc for mac, loc in self.hosts.items() if loc.dpid != dpid}

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
        if src not in self.adj or dst not in self.adj:
            return None
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
        """Up to four near-equal paths in a strictly destination-decreasing DAG.

        Every next hop decreases the shortest distance to the destination, so
        merging paths into per-switch SELECT groups cannot introduce a loop.
        Searches never mutate the live graph.
        """
        if src not in self.adj or dst not in self.adj:
            return []
        distances = {dst: 0.0}
        heap = [(0.0, dst)]
        while heap:
            cost, node = heapq.heappop(heap)
            if cost > distances[node]:
                continue
            for neighbor in self.adj[node]:
                candidate = cost + self._link_cost(neighbor, node)
                if candidate < distances.get(neighbor, float("inf")):
                    distances[neighbor] = candidate
                    heapq.heappush(heap, (candidate, neighbor))
        if src not in distances:
            return []
        limit = distances[src] * (1 + self.ecmp_tolerance) + 1e-12
        pending = [(0.0, [src])]
        found: List[List[int]] = []
        while pending and len(found) < 4:
            cost, path = heapq.heappop(pending)
            node = path[-1]
            if node == dst:
                found.append(path)
                continue
            for neighbor in sorted(self.adj[node]):
                remaining = distances.get(neighbor, float("inf"))
                next_cost = cost + self._link_cost(node, neighbor)
                if remaining < distances[node] and next_cost + remaining <= limit:
                    heapq.heappush(pending, (next_cost, path + [neighbor]))
        return found

    def _path_cost(self, path: List[int]) -> float:
        return sum(self._link_cost(path[i], path[i + 1])
                   for i in range(len(path) - 1))

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
        return {"adj": adj_serial, "hosts": hosts_serial,
                "routing": {"alpha": self.alpha, "beta": self.beta,
                            "gamma": self.gamma, "ecmp_tolerance": self.ecmp_tolerance}}

    @classmethod
    def from_dict(cls, data: dict,
                  alpha: Optional[float] = None,
                  beta: Optional[float] = None,
                  gamma: Optional[float] = None) -> "TopologyGraph":
        routing = data.get("routing", {})
        g = cls(alpha=alpha if alpha is not None else routing.get("alpha", DEFAULT_ALPHA),
                beta=beta if beta is not None else routing.get("beta", DEFAULT_BETA),
                gamma=gamma if gamma is not None else routing.get("gamma", DEFAULT_GAMMA),
                ecmp_tolerance=routing.get("ecmp_tolerance", ECMP_COST_TOLERANCE))
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
