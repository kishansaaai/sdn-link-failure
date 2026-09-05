# Verified results

Recorded on 2026-09-05 with actual Mininet hosts, Open vSwitch 3.3.4 userspace
datapaths and Ryu 4.34/Python 3.10.20 on Ubuntu 24.04 under WSL2. Bandwidth
shaping was disabled. These are functional lab observations, not hardware
latency guarantees.

Raw evidence: [validated-results.json](validated-results.json).

## Full lifecycle integration

| Topology | Switches / hosts | Baseline loss | Final loss | ECMP routes | Lifecycle result |
|---|---:|---:|---:|---:|---|
| Ring | 4 / 4 | 0% | 0% | 4 | Passed |
| Mesh | 5 / 6 | 0% | 0% | 8 | Passed |
| Fat-tree k=4 | 20 / 16 | 0% | 0% | 224 | Passed |

Each run verifies a used link failing, restoration, complete destination-switch
partition and healing, active-controller termination, recovery on the backup,
a new link failure after takeover, and the former primary restarting as standby.
There were no unexpected OpenFlow errors in these runs.

A separate reuse test kept one controller alive while replacing mesh with ring
and then fat-tree networks. All three first-pass all-pairs checks had 0% loss,
verifying that previous switch-port classifications do not leak into a new lab.

The time from killing the primary until takeover and three successful test pings
was 5.71 s (ring), 5.69 s (mesh), and 8.03 s (fat-tree). These values include
the five-second lease, readiness polling, reconciliation and ping verification.
They are not isolated controller election durations.

Likewise, the 415–425 ms link-failure-to-verified-ping observations include
200 ms polling and a three-ping validation. They must not be presented as
precise packet outage measurements.

## Continuous mesh traffic

A separate 12-second chaos run performed three failures with continuous ping at
20 ms intervals. It verified packet delivery while each link remained down.

| Failure | Packets sent / received | Sample loss | Largest observed reply gap |
|---|---:|---:|---:|
| 3–2 | 220 / 220 | 0% | 40.44 ms |
| 3–1 | 212 / 212 | 0% | 40.29 ms |
| 5–4 | 222 / 221 | 0.45% | 59.49 ms |

Reply gaps include normal ping spacing and scheduler variation. These three
samples do not establish a p95 network-recovery guarantee. Controller enqueue
time averaged 1.07 ms in that run; it is a different measurement.

## Reproduce

```bash
sudo python3 tests/integration_lab.py --controller-python "$PWD/.venv/bin/python"
# WSL fallback:
sudo python3 tests/integration_lab.py --controller-python "$PWD/.venv/bin/python" --datapath user

# Against a separately started standalone controller:
sudo python3 chaos_test.py --topo mesh --duration 60
```

For an algorithm-only benchmark without Ryu, root or Mininet:

```bash
python3 benchmarks/run_simulated_benchmark.py --all
```

The historical script name is retained, but it now measures graph computation
only. It adds no fabricated flow-message delays or packet-loss estimates and
does not overwrite this report.
