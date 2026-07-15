# Benchmark Results

> **How these numbers were produced:** `benchmarks/run_simulated_benchmark.py`
> exercises the exact same `topology_graph.py` routing engine the live
> controller uses (weighted Dijkstra + ECMP), with a calibrated per-flow-mod
> delay standing in for OVS/Ryu round-trip cost. Requires no Mininet, no
> root — runs in CI. For real end-to-end measurement through Mininet + OVS
> + the live control channel, use `chaos_test.py` (needs sudo + Linux/OVS).

## Results Table (simulated control-plane + calibrated flow-mod cost)

| Topology | Failures | Mean Recovery | p50 | p95 | p99 | Est. Loss During Failure |
|---|---|---|---|---|---|---|
| Ring | 8 | 10.0 ms | 10.4 ms | 12.1 ms | 12.1 ms | 1.2% |
| Mesh | 11 | 10.2 ms | 10.0 ms | 12.3 ms | 12.3 ms | 1.2% |
| Fattree | 14 | 10.3 ms | 10.3 ms | 12.3 ms | 12.3 ms | 1.2% |

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
