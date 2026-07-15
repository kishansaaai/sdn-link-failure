"""
test_topology_graph.py — Comprehensive unit tests for TopologyGraph.

Tests weighted Dijkstra, ECMP, serialisation/deserialisation,
and the cost function in complete isolation (no Ryu, no Mininet).
"""
import pytest
from ryu_controller.topology_graph import LinkMetrics, TopologyGraph


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def linear():
    """1 – 2 – 3 (linear chain)"""
    g = TopologyGraph()
    g.add_link(1, 1, 2, 1)
    g.add_link(2, 2, 3, 1)
    return g


@pytest.fixture
def diamond():
    """
    Diamond graph: two equal-cost paths from 1 to 4.

         2
        / \\
    1 -     - 4
        \\ /
         3

    Edges: 1-2, 1-3, 2-4, 3-4
    """
    g = TopologyGraph()
    g.add_link(1, 1, 2, 1)
    g.add_link(1, 2, 3, 1)
    g.add_link(2, 2, 4, 1)
    g.add_link(3, 2, 4, 2)
    return g


@pytest.fixture
def mesh():
    """5-node partial mesh with varied topology."""
    g = TopologyGraph()
    g.add_link(1, 1, 2, 1)
    g.add_link(2, 2, 3, 1)
    g.add_link(1, 2, 4, 1)
    g.add_link(4, 2, 5, 1)
    g.add_link(5, 2, 6, 1)
    g.add_link(3, 2, 6, 2)
    return g


# ---------------------------------------------------------------------------
# Basic shortest path
# ---------------------------------------------------------------------------

def test_direct_path(linear):
    path = linear.weighted_dijkstra(1, 3)
    assert path == [1, 2, 3]


def test_same_node_path(linear):
    path = linear.weighted_dijkstra(1, 1)
    assert path == [1]


def test_no_path_disconnected():
    g = TopologyGraph()
    g.add_switch(1)
    g.add_switch(2)
    assert g.weighted_dijkstra(1, 2) is None


def test_path_after_edge_removal(linear):
    path_before = linear.weighted_dijkstra(1, 3)
    assert path_before == [1, 2, 3]
    linear.remove_link(1, 2)
    path_after = linear.weighted_dijkstra(1, 3)
    assert path_after is None   # only route was via 1-2


def test_path_restored_after_readd(linear):
    linear.remove_link(1, 2)
    assert linear.weighted_dijkstra(1, 3) is None
    linear.add_link(1, 1, 2, 1)
    path = linear.weighted_dijkstra(1, 3)
    assert path == [1, 2, 3]


# ---------------------------------------------------------------------------
# Weighted routing — cost function influences path selection
# ---------------------------------------------------------------------------

def test_weighted_prefers_low_latency(diamond):
    """
    Make 1→2→4 cheap (low latency) vs 1→3→4 expensive (high latency).
    Dijkstra should pick 1→2→4.
    """
    cheap  = LinkMetrics(latency_ms=1.0,  bandwidth_mbps=1000)
    pricey = LinkMetrics(latency_ms=50.0, bandwidth_mbps=1000)

    diamond.update_metrics(1, 2, cheap)
    diamond.update_metrics(2, 4, cheap)
    diamond.update_metrics(1, 3, pricey)
    diamond.update_metrics(3, 4, pricey)

    path = diamond.weighted_dijkstra(1, 4)
    assert path == [1, 2, 4]


def test_weighted_avoids_high_loss():
    """High loss_rate on one path should push traffic to the other."""
    g = TopologyGraph(alpha=0.1, beta=0.1, gamma=0.8)
    g.add_link(1, 1, 2, 1, LinkMetrics(latency_ms=1, bandwidth_mbps=1000, loss_rate=0.0))
    g.add_link(2, 2, 4, 1, LinkMetrics(latency_ms=1, bandwidth_mbps=1000, loss_rate=0.0))
    g.add_link(1, 2, 3, 1, LinkMetrics(latency_ms=1, bandwidth_mbps=1000, loss_rate=0.9))
    g.add_link(3, 2, 4, 2, LinkMetrics(latency_ms=1, bandwidth_mbps=1000, loss_rate=0.9))

    path = g.weighted_dijkstra(1, 4)
    assert path == [1, 2, 4]


def test_cost_function_values():
    m = LinkMetrics(latency_ms=10.0, bandwidth_mbps=100.0, loss_rate=0.05)
    cost = m.cost(alpha=0.5, beta=0.3, gamma=0.2)
    expected = 0.5 * 10.0 + 0.3 * (1 / 100.0) + 0.2 * 0.05
    assert abs(cost - expected) < 1e-9


# ---------------------------------------------------------------------------
# ECMP
# ---------------------------------------------------------------------------

def test_ecmp_returns_multiple_paths(diamond):
    """Diamond graph always has two equal-cost (hop-count) paths."""
    paths = diamond.ecmp_paths(1, 4)
    assert len(paths) >= 1   # at minimum the best path
    # Both paths should start at 1 and end at 4
    for p in paths:
        assert p[0] == 1
        assert p[-1] == 4


def test_ecmp_single_path_when_only_one_exists(linear):
    paths = linear.ecmp_paths(1, 3)
    assert len(paths) == 1
    assert paths[0] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------

def test_serialise_deserialise(mesh):
    mesh.learn_host("aa:bb:cc:dd:ee:ff", 1, 3)
    d = mesh.to_dict()
    g2 = TopologyGraph.from_dict(d)

    # Graph structure preserved
    assert set(g2.adj.keys()) == set(mesh.adj.keys())
    # Host preserved
    assert "aa:bb:cc:dd:ee:ff" in g2.hosts
    assert g2.hosts["aa:bb:cc:dd:ee:ff"].dpid == 1

    # Routing still works
    path = g2.weighted_dijkstra(1, 6)
    assert path is not None


def test_serialise_preserves_metrics(diamond):
    m = LinkMetrics(latency_ms=5.5, bandwidth_mbps=500.0, loss_rate=0.01)
    diamond.update_metrics(1, 2, m)
    d = diamond.to_dict()
    g2 = TopologyGraph.from_dict(d)
    _, rm = g2.adj[1][2]
    assert abs(rm.latency_ms - 5.5) < 1e-6
    assert abs(rm.bandwidth_mbps - 500.0) < 1e-6


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def test_path_to_port_sequence(diamond):
    seq = diamond.path_to_port_sequence([1, 2, 4])
    # seq should be [(dpid=1, port=out_to_2), (dpid=2, port=out_to_4)]
    assert len(seq) == 2
    dpid1, port1 = seq[0]
    assert dpid1 == 1
    dpid2, port2 = seq[1]
    assert dpid2 == 2


def test_host_learning(diamond):
    diamond.learn_host("de:ad:be:ef:00:01", 1, 5)
    assert "de:ad:be:ef:00:01" in diamond.hosts
    assert diamond.hosts["de:ad:be:ef:00:01"].port == 5
