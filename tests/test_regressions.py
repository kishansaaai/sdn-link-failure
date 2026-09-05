from copy import deepcopy
import pytest
from ryu_controller.topology_graph import TopologyGraph, LinkMetrics
from ryu_controller.group_allocator import GroupIdAllocator


def diamond():
    graph = TopologyGraph()
    graph.add_link(1, 1, 2, 1)
    graph.add_link(1, 2, 3, 1)
    graph.add_link(2, 2, 4, 1)
    graph.add_link(3, 2, 4, 2)
    return graph


def test_ecmp_preserves_live_graph_and_returns_both_branches():
    graph = diamond()
    before = deepcopy(graph.to_dict())
    for _ in range(10):
        assert graph.ecmp_paths(1, 4) == [[1, 2, 4], [1, 3, 4]]
    assert graph.to_dict() == before


def test_unknown_same_switch_is_not_a_path():
    assert TopologyGraph().weighted_dijkstra(123, 123) is None


def test_switch_removal_cleans_links_and_hosts():
    graph = diamond()
    graph.learn_host("a", 2, 9)
    graph.remove_switch(2)
    assert "a" not in graph.hosts
    assert all(2 not in edges for edges in graph.adj.values())
    assert graph.weighted_dijkstra(1, 4) == [1, 3, 4]


@pytest.mark.parametrize("metrics", [
    {"latency_ms": -1}, {"bandwidth_mbps": float("nan")},
    {"loss_rate": 2}, {"utilization": float("inf")},
])
def test_invalid_metrics_rejected(metrics):
    with pytest.raises(ValueError):
        LinkMetrics(**metrics)


def test_congestion_increases_routing_cost():
    assert LinkMetrics(utilization=0.9).cost() > LinkMetrics(utilization=0.1).cost()


def test_allocator_idempotency_collision_and_reuse():
    allocator = GroupIdAllocator()
    assert allocator.allocate(("a", "b")) == allocator.allocate(("a", "b"))
    assert allocator.allocate(("c", "d")) != allocator.get(("a", "b"))
    old = allocator.get(("a", "b"))
    allocator.release(("a", "b"))
    assert allocator.allocate(("e", "f")) == old


def test_snapshot_preserves_routing_weights():
    graph = TopologyGraph(alpha=0.1, beta=0.8, gamma=0.1, ecmp_tolerance=0.02)
    restored = TopologyGraph.from_dict(graph.to_dict())
    assert (restored.alpha, restored.beta, restored.gamma, restored.ecmp_tolerance) == (0.1, 0.8, 0.1, 0.02)


def test_algorithm_benchmark_has_no_synthetic_network_measurements():
    from benchmarks.run_simulated_benchmark import run_benchmark
    result = run_benchmark("fattree", 3)
    assert len(result["samples_ms"]) == 3
    assert result["measurement"] == "graph_computation_only"
    assert "loss_pcts" not in result
