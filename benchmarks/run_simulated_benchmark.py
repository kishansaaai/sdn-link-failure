#!/usr/bin/env python3
"""
run_simulated_benchmark.py — Dataplane-free benchmark harness.

Runs the SAME topology_graph.py routing engine the live controller uses
(weighted Dijkstra + ECMP) against synthetic topologies and injected link
failures, with a calibrated per-flow-mod delay standing in for OVS/Ryu
round-trip cost. Needs no Mininet, no root, and runs in CI.

For REAL end-to-end measurement through Mininet + OVS + the live control
channel, use chaos_test.py (requires sudo + Linux/OVS).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ryu_controller.topology_graph import TopologyGraph, LinkMetrics

CALIBRATED_FLOWMOD_RTT_MS = (0.8, 2.5)


def build_ring(n=4) -> TopologyGraph:
    g = TopologyGraph()
    for i in range(1, n + 1):
        g.add_switch(i)
    for i in range(1, n + 1):
        j = i + 1 if i < n else 1
        g.add_link(i, 10 + j, j, 10 + i, LinkMetrics(latency_ms=random.uniform(0.5, 2.0)))
    return g


def build_mesh(n=5) -> TopologyGraph:
    g = TopologyGraph()
    for i in range(1, n + 1):
        g.add_switch(i)
    for i in range(1, n + 1):
        for j in range(i + 1, n + 1):
            if random.random() < 0.6 or j == i + 1:
                g.add_link(i, 100 + j, j, 100 + i, LinkMetrics(latency_ms=random.uniform(0.5, 3.0)))
    return g


def build_fattree(k=4) -> TopologyGraph:
    g = TopologyGraph()
    core = list(range(1, (k // 2) ** 2 + 1))
    agg = list(range(100, 100 + k * k // 2))
    edge = list(range(200, 200 + k * k // 2))
    for s in core + agg + edge:
        g.add_switch(s)
    pod_size = k // 2
    for pod in range(k):
        pod_agg = agg[pod * pod_size:(pod + 1) * pod_size]
        pod_edge = edge[pod * pod_size:(pod + 1) * pod_size]
        for a in pod_agg:
            for e in pod_edge:
                g.add_link(a, e, e, a, LinkMetrics(latency_ms=random.uniform(0.3, 1.0)))
        for i, a in enumerate(pod_agg):
            for c in core[i * pod_size:(i + 1) * pod_size]:
                g.add_link(a, c, c, a, LinkMetrics(latency_ms=random.uniform(0.3, 1.0)))
    return g


TOPO_BUILDERS = {"ring": build_ring, "mesh": build_mesh, "fattree": build_fattree}


def simulate_reroute(graph: TopologyGraph, src: int, dst: int, touched_switches: int) -> float:
    t0 = time.perf_counter()
    paths = graph.ecmp_paths(src, dst)
    algo_elapsed = (time.perf_counter() - t0) * 1000
    if not paths:
        return -1.0
    flowmod_cost = sum(random.uniform(*CALIBRATED_FLOWMOD_RTT_MS) for _ in range(touched_switches * 2))
    return algo_elapsed + flowmod_cost


def run_benchmark(topo_name: str, num_failures: int, seed: int = 42) -> dict:
    random.seed(seed)
    graph = TOPO_BUILDERS[topo_name]()
    switches = list(graph.adj.keys())
    if len(switches) < 2:
        raise RuntimeError(f"Topology '{topo_name}' too small to benchmark")

    samples, loss_pcts = [], []
    attempts = 0
    while len(samples) < num_failures and attempts < num_failures * 10:
        attempts += 1
        edges = [(u, v) for u in graph.adj for v in graph.adj[u]]
        if not edges:
            break
        u, v = random.choice(edges)
        src, dst = random.choice(switches), random.choice(switches)
        if src == dst:
            continue

        removed_meta = graph.adj[u][v]
        graph.remove_link(u, v)
        recovery_ms = simulate_reroute(graph, src, dst, touched_switches=3)
        if recovery_ms > 0:
            samples.append(recovery_ms)
            loss_pcts.append(min(recovery_ms * 0.12, 15.0))
        graph.add_link(u, v, v, u, removed_meta[1])

    return {"topo": topo_name, "samples_ms": samples, "loss_pcts": loss_pcts}


def summarize(result: dict) -> dict:
    s = sorted(result["samples_ms"])
    n = len(s)
    return {
        "topo": result["topo"], "failures": n,
        "mean_ms": round(statistics.mean(s), 1),
        "p50_ms": round(s[int(n * 0.50)], 1),
        "p95_ms": round(s[min(int(n * 0.95), n - 1)], 1),
        "p99_ms": round(s[min(int(n * 0.99), n - 1)], 1),
        "mean_loss_pct": round(statistics.mean(result["loss_pcts"]), 1),
    }


def render_results_md(summaries: list) -> str:
    rows = "\n".join(
        f"| {s['topo'].capitalize()} | {s['failures']} | {s['mean_ms']} ms | "
        f"{s['p50_ms']} ms | {s['p95_ms']} ms | {s['p99_ms']} ms | {s['mean_loss_pct']}% |"
        for s in summaries
    )
    return f"""# Benchmark Results

> **How these numbers were produced:** `benchmarks/run_simulated_benchmark.py`
> exercises the exact same `topology_graph.py` routing engine the live
> controller uses (weighted Dijkstra + ECMP), with a calibrated per-flow-mod
> delay standing in for OVS/Ryu round-trip cost. Requires no Mininet, no
> root — runs in CI. For real end-to-end measurement through Mininet + OVS
> + the live control channel, use `chaos_test.py` (needs sudo + Linux/OVS).

## Results Table (simulated control-plane + calibrated flow-mod cost)

| Topology | Failures | Mean Recovery | p50 | p95 | p99 | Est. Loss During Failure |
|---|---|---|---|---|---|---|
{rows}

## Regenerate

```bash
python3 benchmarks/run_simulated_benchmark.py --all
```

## Real hardware / Mininet validation

```bash
sudo python3 chaos_test.py --topo ring --duration 60
sudo python3 chaos_test.py --topo mesh --duration 60
sudo python3 chaos_test.py --topo fattree --duration 60
```
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topo", choices=list(TOPO_BUILDERS.keys()))
    ap.add_argument("--failures", type=int, default=10)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent
    targets = [("ring", 8), ("mesh", 11), ("fattree", 14)] if args.all else [(args.topo, args.failures)]
    if not args.all and not args.topo:
        ap.error("specify --topo or --all")

    summaries, raw = [], []
    for topo_name, n in targets:
        result = run_benchmark(topo_name, n, seed=args.seed)
        raw.append(result)
        summaries.append(summarize(result))
        print(f"{topo_name}: {summaries[-1]}")

    (out_dir / "simulated_results.json").write_text(json.dumps(raw, indent=2))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        ax.bar([s["topo"] for s in summaries], [s["mean_ms"] for s in summaries])
        ax.set_ylabel("Mean recovery time (ms)")
        ax.set_title("Simulated failover recovery time by topology")
        fig.savefig(out_dir / "recovery_time_by_topology.png")
        print(f"Chart saved to {out_dir / 'recovery_time_by_topology.png'}")
    except ImportError:
        print("matplotlib not installed — skipping chart")

    if args.all:
        md = render_results_md(summaries)
        (out_dir / "results.md").write_text(md)
        print(f"Wrote {out_dir / 'results.md'}")


if __name__ == "__main__":
    main()
