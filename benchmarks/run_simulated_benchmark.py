#!/usr/bin/env python3
"""Pure graph microbenchmark. No simulated network delays or packet-loss claims."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import random
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ryu_controller.topology_graph import TopologyGraph


def build_graph(name):
    graph = TopologyGraph()
    ports = {}

    def link(a, b):
        ports[a] = ports.get(a, 0) + 1
        ports[b] = ports.get(b, 0) + 1
        graph.add_link(a, ports[a], b, ports[b])

    if name in ("ring", "mesh"):
        count = 4 if name == "ring" else 5
        for i in range(1, count + 1):
            link(i, i % count + 1)
        if name == "mesh":
            link(1, 3)
            link(2, 5)
    elif name == "fattree":
        core, agg, edge = list(range(1, 5)), list(range(5, 13)), list(range(13, 21))
        for pod in range(4):
            for local, a in enumerate(agg[pod * 2:pod * 2 + 2]):
                for e in edge[pod * 2:pod * 2 + 2]:
                    link(a, e)
                for c in core[local * 2:local * 2 + 2]:
                    link(a, c)
    else:
        raise ValueError(name)
    return graph


def run_benchmark(topo_name, num_failures=10, seed=42):
    if num_failures <= 0:
        raise ValueError("Failure count must be positive")
    graph = build_graph(topo_name)
    rng = random.Random(seed)
    samples, unreachable = [], 0
    original = graph.to_dict()
    for _ in range(num_failures):
        src, dst = rng.sample(sorted(graph.adj), 2)
        path = graph.weighted_dijkstra(src, dst)
        a, b = rng.choice(list(zip(path, path[1:])))
        forward, reverse = graph.adj[a][b], graph.adj[b][a]
        try:
            graph.remove_link(a, b)
            start = time.perf_counter()
            paths = graph.ecmp_paths(src, dst)
            samples.append((time.perf_counter() - start) * 1000)
            unreachable += not bool(paths)
        finally:
            graph.adj[a][b], graph.adj[b][a] = forward, reverse
    assert graph.to_dict() == original
    return {"topology": topo_name, "measurement": "graph_computation_only",
            "samples_ms": samples, "unreachable_count": unreachable,
            "mean_ms": statistics.mean(samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topo", choices=["ring", "mesh", "fattree"])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--failures", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="benchmarks/algorithm_results.json")
    args = parser.parse_args()
    if not args.all and not args.topo:
        parser.error("Specify --topo or --all")
    if args.failures <= 0:
        parser.error("--failures must be positive")
    names = ["ring", "mesh", "fattree"] if args.all else [args.topo]
    results = [run_benchmark(name, args.failures, args.seed) for name in names]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
