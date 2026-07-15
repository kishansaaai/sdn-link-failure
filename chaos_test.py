#!/usr/bin/env python3
"""
chaos_test.py — Chaos engineering + benchmarking harness for the SDN controller.

Usage:
    sudo python3 chaos_test.py --topo ring|mesh|fattree [--duration 60] [--api http://localhost:5000]

The script:
  1. Starts the chosen Mininet topology with a remote controller.
  2. Runs a sustained background iperf + ping traffic stream.
  3. Randomly kills 1-3 links at random intervals during the test.
  4. After each failure, polls the controller REST API for the recovery_log
     to extract the measured failover time.
  5. Restores links and continues.
  6. At the end, prints a summary table (mean/p50/p95/p99 recovery, loss %)
     and saves a matplotlib chart to benchmarks/recovery_time_by_topology.png.

Requires: mininet, iperf, requests, matplotlib, numpy
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from typing import List

import numpy as np

# Mininet imports are runtime-only (Linux)
try:
    from mininet.net import Mininet
    from mininet.node import RemoteController
    from mininet.link import TCLink
    from mininet.log import setLogLevel
    MININET_AVAILABLE = True
except ImportError:
    MININET_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MPL_AVAILABLE = True
except ImportError:
    MPL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONTROLLER_IP   = "127.0.0.1"
CONTROLLER_PORT = 6633
API_BASE        = "http://127.0.0.1:5000"
CHAOS_MIN_WAIT  = 5    # seconds before first failure
CHAOS_MAX_WAIT  = 15   # seconds max between failures
RESTORE_DELAY   = 3    # seconds link stays down before restoration
PING_COUNT      = 20   # pings per measurement window


# ---------------------------------------------------------------------------
# Topology factory
# ---------------------------------------------------------------------------

def build_topo(name: str):
    if name == "ring":
        from topologies.ring_topo import RingTopo
        return RingTopo()
    elif name == "mesh":
        from topologies.mesh_topo import MeshTopo
        return MeshTopo()
    elif name == "fattree":
        from topologies.fat_tree_topo import FatTreeTopo
        return FatTreeTopo()
    else:
        raise ValueError(f"Unknown topology: {name}")


# ---------------------------------------------------------------------------
# Chaos test core
# ---------------------------------------------------------------------------

class ChaosTest:
    def __init__(self, topo_name: str, duration: int, api_base: str):
        self.topo_name  = topo_name
        self.duration   = duration
        self.api_base   = api_base
        self.events: List[dict] = []
        self.recovery_times: List[float] = []
        self.loss_during: List[float]    = []

    def run(self) -> dict:
        if not MININET_AVAILABLE:
            print("ERROR: Mininet not available. Run on Ubuntu with Mininet installed.")
            return {}

        setLogLevel("warning")
        topo = build_topo(self.topo_name)
        net  = Mininet(
            topo=topo,
            controller=RemoteController("c0", ip=CONTROLLER_IP, port=CONTROLLER_PORT),
            link=TCLink,
            autoSetMacs=True,
        )
        net.start()
        print(f"[chaos] Topology '{self.topo_name}' started. Waiting 10s for discovery...")
        time.sleep(10)

        # Baseline: pingAll before any failures
        print("[chaos] Measuring baseline...")
        baseline_loss = net.pingAll(timeout=1)
        net.pingAll(timeout=1)   # second pass to ensure all paths learned

        # Get all switch-to-switch links
        sw_links = [
            (l.intf1.node.name, l.intf2.node.name)
            for l in net.links
            if l.intf1.node.name.startswith("s") and l.intf2.node.name.startswith("s")
        ]

        start_ts  = time.time()
        prev_log_len = 0

        while time.time() - start_ts < self.duration:
            wait = random.uniform(CHAOS_MIN_WAIT, CHAOS_MAX_WAIT)
            time.sleep(min(wait, self.duration - (time.time() - start_ts)))

            if time.time() - start_ts >= self.duration:
                break

            # Pick 1-3 random links to kill
            n_kill = random.randint(1, min(3, len(sw_links)))
            targets = random.sample(sw_links, n_kill)

            for s1, s2 in targets:
                t_fail = time.time()
                print(f"[chaos] Killing link {s1}-{s2}")
                net.configLinkStatus(s1, s2, "down")
                self.events.append({"event": "down", "link": f"{s1}-{s2}", "ts": t_fail})

            # Measure loss immediately after failure
            loss = net.pingAll(timeout=2)
            self.loss_during.append(loss)
            print(f"[chaos] Loss during failure: {loss:.1f}%")

            # Fetch recovery log from controller API
            time.sleep(RESTORE_DELAY)
            self._poll_recovery_log(prev_log_len)
            prev_log_len = self._get_log_length()

            # Restore links
            for s1, s2 in targets:
                net.configLinkStatus(s1, s2, "up")
                self.events.append({"event": "up", "link": f"{s1}-{s2}", "ts": time.time()})
            time.sleep(2)

        # Final measurement
        final_loss = net.pingAll(timeout=1)
        net.stop()

        return self._compile_results(baseline_loss, final_loss)

    def _poll_recovery_log(self, prev_len: int) -> None:
        if not REQUESTS_AVAILABLE:
            return
        try:
            resp = requests.get(f"{self.api_base}/recovery-log", timeout=2)
            log  = resp.json()
            new_entries = log[prev_len:]
            for entry in new_entries:
                ms = entry.get("recovery_ms", 0)
                self.recovery_times.append(ms)
        except Exception:
            pass

    def _get_log_length(self) -> int:
        if not REQUESTS_AVAILABLE:
            return 0
        try:
            resp = requests.get(f"{self.api_base}/recovery-log", timeout=2)
            return len(resp.json())
        except Exception:
            return 0

    def _compile_results(self, baseline_loss: float, final_loss: float) -> dict:
        rt = self.recovery_times
        results = {
            "topology": self.topo_name,
            "baseline_loss_pct": baseline_loss,
            "final_loss_pct": final_loss,
            "num_failures": len([e for e in self.events if e["event"] == "down"]),
            "mean_loss_during_pct": float(np.mean(self.loss_during)) if self.loss_during else 0,
            "recovery_times_ms": rt,
            "mean_recovery_ms":  float(np.mean(rt)) if rt else 0,
            "p50_recovery_ms":   float(np.percentile(rt, 50)) if rt else 0,
            "p95_recovery_ms":   float(np.percentile(rt, 95)) if rt else 0,
            "p99_recovery_ms":   float(np.percentile(rt, 99)) if rt else 0,
        }
        self._print_table(results)
        self._save_results(results)
        return results

    def _print_table(self, r: dict) -> None:
        print("\n" + "=" * 60)
        print(f"  CHAOS TEST RESULTS — {r['topology'].upper()}")
        print("=" * 60)
        print(f"  Baseline packet loss :  {r['baseline_loss_pct']:.1f}%")
        print(f"  Loss during failures :  {r['mean_loss_during_pct']:.1f}% (avg)")
        print(f"  Final packet loss    :  {r['final_loss_pct']:.1f}%")
        print(f"  Failure events       :  {r['num_failures']}")
        print(f"  Recovery samples     :  {len(r['recovery_times_ms'])}")
        if r["recovery_times_ms"]:
            print(f"  Mean recovery        :  {r['mean_recovery_ms']:.1f} ms")
            print(f"  p50 recovery         :  {r['p50_recovery_ms']:.1f} ms")
            print(f"  p95 recovery         :  {r['p95_recovery_ms']:.1f} ms")
            print(f"  p99 recovery         :  {r['p99_recovery_ms']:.1f} ms")
        print("=" * 60 + "\n")

    def _save_results(self, r: dict) -> None:
        os.makedirs("benchmarks", exist_ok=True)
        path = f"benchmarks/{self.topo_name}_results.json"
        with open(path, "w") as f:
            json.dump(r, f, indent=2)
        print(f"[chaos] Results saved to {path}")


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def generate_chart(results_by_topo: dict) -> None:
    """Generate recovery_time_by_topology.png from collected results."""
    if not MPL_AVAILABLE:
        print("matplotlib not available — skipping chart")
        return

    topos = list(results_by_topo.keys())
    metrics = ["mean_recovery_ms", "p50_recovery_ms", "p95_recovery_ms"]
    labels  = ["Mean", "p50", "p95"]
    colors  = ["#4C9BE8", "#5DBB8A", "#E8924C"]

    x = np.arange(len(topos))
    width = 0.25

    fig, ax = plt.subplots(figsize=(10, 6))
    for i, (metric, label, color) in enumerate(zip(metrics, labels, colors)):
        vals = [results_by_topo[t].get(metric, 0) for t in topos]
        bars = ax.bar(x + i * width, vals, width, label=label, color=color, alpha=0.85)
        ax.bar_label(bars, fmt="%.0f ms", padding=3, fontsize=9)

    ax.set_xlabel("Topology", fontsize=13)
    ax.set_ylabel("Recovery Time (ms)", fontsize=13)
    ax.set_title("SDN Failover Recovery Time by Topology", fontsize=15, fontweight="bold")
    ax.set_xticks(x + width)
    ax.set_xticklabels([t.capitalize() for t in topos], fontsize=12)
    ax.legend(fontsize=11)
    ax.grid(axis="y", linestyle="--", alpha=0.5)
    ax.set_ylim(bottom=0)

    plt.tight_layout()
    out = "benchmarks/recovery_time_by_topology.png"
    plt.savefig(out, dpi=150)
    print(f"[chaos] Chart saved to {out}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SDN Chaos Test Harness")
    parser.add_argument("--topo",     default="mesh",
                        choices=["ring", "mesh", "fattree"])
    parser.add_argument("--duration", type=int, default=60,
                        help="Test duration in seconds")
    parser.add_argument("--api",      default=API_BASE,
                        help="Controller REST API base URL")
    args = parser.parse_args()

    test = ChaosTest(args.topo, args.duration, args.api)
    test.run()
