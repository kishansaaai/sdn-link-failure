# Legacy — POX v1 Prototype

This directory contains the **original proof-of-concept** controller built with
[POX](https://github.com/noxrepo/pox) and OpenFlow 1.0.

It demonstrates the problem framing and validates the core idea:  
detect link failures via `PortStatus` events and reroute traffic using
Dijkstra's algorithm on a hand-built topology graph.

## Why it was replaced

| POX / OF 1.0 (v1) | Ryu / OF 1.3 (v2) |
|---|---|
| Hand-rolled LLDP | `ryu.topology` discovery API |
| Hop-count Dijkstra | Weighted cost: latency + utilization + loss |
| No HA | Primary/backup with Redis state sync |
| No metrics | Prometheus + Grafana dashboard |
| Flood fallback | Table-miss entries + group tables for ECMP |

The v1 code is preserved here intentionally — it shows the engineering
journey, not just the end state.

## Running the legacy version

```bash
# Terminal 1 — POX controller
cd ~/pox
python3 pox.py openflow.discovery link_failure_recovery log.level --DEBUG

# Terminal 2 — Mininet topology
sudo python3 legacy/link_failure_topo.py
```
