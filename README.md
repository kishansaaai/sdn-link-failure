# SDN Link-Failure Recovery with Dijkstra Rerouting + Controller HA

[![Tests](https://github.com/kishansaaai/sdn-link-failure/actions/workflows/tests.yml/badge.svg)](https://github.com/kishansaaai/sdn-link-failure/actions/workflows/tests.yml)
[![Lint](https://github.com/kishansaaai/sdn-link-failure/actions/workflows/lint.yml/badge.svg)](https://github.com/kishansaaai/sdn-link-failure/actions/workflows/lint.yml)
[![codecov](https://codecov.io/gh/kishansaaai/sdn-link-failure/branch/main/graph/badge.svg)](https://codecov.io/gh/kishansaaai/sdn-link-failure)

---

## What This Is — And Why It's Interesting

Most SDN student projects implement a learning switch: flood until you learn a MAC, then install a rule. When a link fails, either the flows time out and the switch relearns, or you wipe all flow tables and start over. Both approaches take 2–8 seconds per failure event.

This project does something different: it builds a **live, metric-weighted graph of the entire network topology** using LLDP discovery, runs **Dijkstra's algorithm** to pre-install optimal paths end-to-end (not switch-by-switch), and on any link failure **surgically replaces only the broken paths** — proactively installing new OF rules before the next packet arrives. The result is **sub-100ms data-plane failover** measured at the OpenFlow message level.

It also adds the one thing almost no student SDN project touches: **controller high availability** — a primary and a backup controller sharing topology state via Redis, with automatic leader election when the primary dies.

---

## Architecture

```
┌─────────────────── Control Plane ───────────────────────┐
│                                                          │
│   ┌─────────────────────┐     ┌──────────────────────┐  │
│   │   Ryu Primary       │     │   Ryu Backup         │  │
│   │   :6633 / :8000     │     │   :6634 / :8001      │  │
│   │   role=PRIMARY      │────▶│   role=WATCHING      │  │
│   └─────────┬───────────┘     └──────────┬───────────┘  │
│             │ heartbeat TTL               │ polls        │
│             ▼                             ▼              │
│          ┌──────────────── Redis ───────────────────┐    │
│          │  sdn:heartbeat (TTL=5s)                  │    │
│          │  sdn:topology_state (JSON graph)          │    │
│          └──────────────────────────────────────────┘    │
│                                                          │
│  Prometheus :9090  →  Grafana :3000                      │
└─────────────────────┬────────────────────────────────────┘
                      │ OpenFlow 1.3
         ┌────────────▼────────────┐
         │   Mininet Data Plane    │
         │   s1─s2─s3─s4─s5─s1    │
         │       cross-links       │
         │  h1 h2  h3  h4  h5 h6  │
         └─────────────────────────┘
```

**HA Design (3 sentences):** The primary controller writes a heartbeat key with a 5-second TTL to Redis every 2 seconds. The backup controller polls this key every second; if it expires, the backup loads the last-synced topology JSON from Redis and promotes itself to primary using OpenFlow role negotiation. This separates **control-plane failover time** (~2 s for TTL detection + state load) from **data-plane failover time** (~40 ms for path recomputation and flow installation).

---

## Key Results

| Topology | Failures | Mean Failover | p50 | p95 | Loss During Failure |
|---|---|---|---|---|---|
| Ring (4 sw) | 8 | **38 ms** | 35 ms | 68 ms | 4.2% |
| Mesh (5 sw) | 11 | **42 ms** | 40 ms | 75 ms | 5.8% |
| Fat-tree (k=4) | 14 | **55 ms** | 48 ms | 98 ms | 7.1% |

**vs. naive flood-and-relearn:** 2–8 seconds recovery, 100% loss until relearn.

> Full results and regeneration instructions: [`benchmarks/results.md`](benchmarks/results.md)

---

## How to Run (One Command)

> Requires Docker with Linux kernel support (Ubuntu/Debian recommended).

```bash
git clone https://github.com/kishansaaai/sdn-link-failure
cd sdn-link-failure
docker compose up
```

| Service | URL |
|---|---|
| Grafana Dashboard | http://localhost:3000 (admin/admin) |
| Prometheus | http://localhost:9090 |
| REST API — Topology | http://localhost:5000/topology |
| REST API — Recovery Log | http://localhost:5000/recovery-log |
| Primary Metrics | http://localhost:8000/metrics |

### Bare-metal (Ubuntu + Mininet)

```bash
# Terminal 1 — Primary controller
ryu-manager --observe-links ryu_controller/sdn_controller.py

# Terminal 2 — Mininet
sudo python3 topologies/mesh_topo.py

# Run chaos test
sudo python3 chaos_test.py --topo mesh --duration 60
```

---

## REST API

The controller exposes a lightweight Flask API in a background thread:

```bash
# Live topology as JSON
curl http://localhost:5000/topology

# Measured failover events
curl http://localhost:5000/recovery-log
```

**Example recovery-log output:**
```json
[
  {
    "src_mac": "00:00:00:00:00:01",
    "dst_mac": "00:00:00:00:00:05",
    "reason": "failure",
    "recovery_ms": 42.3,
    "new_path": [1, 5, 4, 3],
    "ts": 1720000000.123
  }
]
```

---

## Why This Is Not a Flood-and-Relearn Switch

| Capability | L2 Learning Switch | This Controller |
|---|---|---|
| Path computation | Per-hop, reactive | Global Dijkstra, proactive |
| Failure response | Wait for timeout / wipe all rules | Surgical delete + instant reinstall |
| Recovery time | 2–8 seconds | 35–55 ms |
| Link cost metric | None (hop count) | Latency + bandwidth + loss rate |
| Traffic engineering | None | Proactive rebalancing at >75% utilization |
| Load balancing | None | ECMP via OF1.3 Group Tables |
| Controller HA | None | Primary/backup with Redis state sync |
| Observability | None | Prometheus + Grafana, per-link gauges |

---

## Design Decisions & Tradeoffs

**Why Ryu over ONOS / OpenDaylight?**  
ONOS and ODL are production-grade platforms with clustered state, intent frameworks, and hundreds of thousands of lines of code. For a research/portfolio project validating a specific algorithm, Ryu gives direct access to the OpenFlow message layer with minimal overhead. ONOS would be the right choice if you needed to operate at carrier scale with real hardware.

**Why Redis over etcd / ZooKeeper for state sync?**  
etcd and ZooKeeper provide strong consistency guarantees (Raft consensus) which are essential when many controllers must agree on cluster state. Here, we have exactly two nodes (primary + one backup) and the state is eventually consistent by design — if the primary dies between two heartbeats, the backup may load topology state that is up to 2 seconds stale, which is acceptable because switches will re-advertise link events on reconnect. Redis's TTL-based expiry maps perfectly to the heartbeat pattern with zero configuration overhead.

**What I'd do differently at larger scale:**  
At 500+ switches, the centralized Dijkstra computation on every topology change becomes a bottleneck. I'd move to a hierarchical controller design (ONOS clusters with regional sub-controllers), switch to a distributed graph store (Apache TinkerPop / JanusGraph), and implement segment routing (SR-MPLS or SRv6) to encode paths in packet headers rather than installing per-flow rules on every switch.

---

## Future Work

- **P4 dataplane**: Move failure detection into the dataplane using P4 registers and INT (In-band Network Telemetry) — eliminates the controller round-trip entirely for detection.
- **Real hardware**: Test on a Zodiac FX OpenFlow switch or BMv2 software switch with a physical NIC for accurate latency measurements.
- **Inter-domain failover**: BGP-like route redistribution between SDN islands — relevant for multi-datacenter deployments.
- **ML-based traffic prediction**: Replace the fixed 75% utilization threshold with an LSTM model predicting link congestion 30 seconds ahead.

---

## Proof of Execution

- **`benchmarks/results.md`** — measured failover times across three topologies
- **`cn-screenshots.pdf`** — original v1 POX prototype screenshots
- **`legacy/`** — preserved v1 POX controller showing the engineering evolution

---

## References

- [Ryu SDN Framework](https://ryu-sdn.org/)
- [OpenFlow 1.3 Specification](https://opennetworking.org/wp-content/uploads/2014/10/openflow-spec-v1.3.0.pdf)
- [Mininet](http://mininet.org)
- [Dijkstra's Algorithm — Original Paper](https://doi.org/10.1007/BF01386390)
- [Fat-Tree Topology — Al-Fares et al. 2008](https://dl.acm.org/doi/10.1145/1402958.1402967)
