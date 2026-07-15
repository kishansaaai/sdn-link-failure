"""
test_weight_function.py — Unit tests for the LinkMetrics cost function.

Tests the composite cost = α·latency + β·(1/bw) + γ·loss_rate
and validates that TopologyGraph correctly uses it for routing decisions.
"""
import pytest
from ryu_controller.topology_graph import (
    DEFAULT_ALPHA, DEFAULT_BETA, DEFAULT_GAMMA, LinkMetrics, TopologyGraph
)


# ---------------------------------------------------------------------------
# Cost function unit tests
# ---------------------------------------------------------------------------

def test_default_cost_positive():
    m = LinkMetrics()
    assert m.cost() > 0


def test_high_latency_increases_cost():
    low  = LinkMetrics(latency_ms=1.0)
    high = LinkMetrics(latency_ms=100.0)
    assert high.cost() > low.cost()


def test_low_bandwidth_increases_cost():
    fat  = LinkMetrics(bandwidth_mbps=10000.0)
    thin = LinkMetrics(bandwidth_mbps=1.0)
    assert thin.cost() > fat.cost()


def test_high_loss_increases_cost():
    clean = LinkMetrics(loss_rate=0.0)
    lossy = LinkMetrics(loss_rate=0.5)
    assert lossy.cost() > clean.cost()


def test_zero_bandwidth_does_not_divide_by_zero():
    m = LinkMetrics(bandwidth_mbps=0.0)
    cost = m.cost()
    assert cost == pytest.approx(
        DEFAULT_ALPHA * m.latency_ms + DEFAULT_BETA * (1 / 0.001) + DEFAULT_GAMMA * m.loss_rate,
        rel=1e-6,
    )


def test_custom_weights():
    m = LinkMetrics(latency_ms=10.0, bandwidth_mbps=100.0, loss_rate=0.05)
    cost = m.cost(alpha=1.0, beta=0.0, gamma=0.0)
    assert cost == pytest.approx(10.0)


def test_beta_weight_only():
    m = LinkMetrics(latency_ms=10.0, bandwidth_mbps=200.0, loss_rate=0.0)
    cost = m.cost(alpha=0.0, beta=1.0, gamma=0.0)
    assert cost == pytest.approx(1.0 / 200.0)


def test_gamma_weight_only():
    m = LinkMetrics(latency_ms=0.0, bandwidth_mbps=1000.0, loss_rate=0.25)
    cost = m.cost(alpha=0.0, beta=0.0, gamma=1.0)
    assert cost == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Weight influences routing in graph
# ---------------------------------------------------------------------------

def test_routing_prioritises_low_latency_when_alpha_high():
    """With α=1, β=γ=0, routing should always prefer lowest latency."""
    g = TopologyGraph(alpha=1.0, beta=0.0, gamma=0.0)
    fast   = LinkMetrics(latency_ms=1.0,  bandwidth_mbps=10.0)
    slow   = LinkMetrics(latency_ms=50.0, bandwidth_mbps=10000.0)

    # Two-path graph: 1→2→4 (fast) vs 1→3→4 (slow)
    g.add_link(1, 1, 2, 1, fast)
    g.add_link(2, 2, 4, 1, fast)
    g.add_link(1, 2, 3, 1, slow)
    g.add_link(3, 2, 4, 2, slow)

    path = g.weighted_dijkstra(1, 4)
    assert path == [1, 2, 4]


def test_routing_prioritises_high_bandwidth_when_beta_high():
    """With β=1, α=γ=0, routing should prefer highest bandwidth."""
    g = TopologyGraph(alpha=0.0, beta=1.0, gamma=0.0)
    fat    = LinkMetrics(latency_ms=50.0, bandwidth_mbps=10000.0)
    thin   = LinkMetrics(latency_ms=1.0,  bandwidth_mbps=1.0)

    g.add_link(1, 1, 2, 1, fat)
    g.add_link(2, 2, 4, 1, fat)
    g.add_link(1, 2, 3, 1, thin)
    g.add_link(3, 2, 4, 2, thin)

    path = g.weighted_dijkstra(1, 4)
    assert path == [1, 2, 4]


def test_update_metrics_changes_routing():
    """Dynamically updating metrics should change the selected path."""
    g = TopologyGraph(alpha=1.0, beta=0.0, gamma=0.0)
    m_normal = LinkMetrics(latency_ms=1.0)
    m_high   = LinkMetrics(latency_ms=999.0)

    g.add_link(1, 1, 2, 1, m_normal)
    g.add_link(2, 2, 4, 1, m_normal)
    g.add_link(1, 2, 3, 1, m_normal)
    g.add_link(3, 2, 4, 2, m_normal)

    # Initially could be either path; after making 1→2 expensive, prefer 1→3
    g.update_metrics(1, 2, m_high)
    g.update_metrics(2, 4, m_high)
    path = g.weighted_dijkstra(1, 4)
    assert path == [1, 3, 4]
