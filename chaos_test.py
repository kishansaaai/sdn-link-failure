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
from typing import List, Tuple

import numpy as np

# Mininet imports are runtime-only (Linux)
try:
    from mininet.net import Mininet
    from mininet.node import RemoteController, OVSSwitch
    from mininet.link import Link
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
CONTROLLER_PORT = 6653
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
        self.data_plane_outages: List[float] = []
        self.data_plane_drops: List[int] = []
        
    def _start_bg_pings(self, pairs: List[Tuple], interval: float = 0.05) -> dict:
        procs = {}
        for h1, h2 in pairs:
            log_file = f"/tmp/ping_{h1.name}_{h2.name}.log"
            # start continuous ping in background
            p = h1.popen(f"ping -i {interval} -D {h2.IP()} > {log_file} 2>&1", shell=True)
            procs[(h1.name, h2.name)] = p
        return procs

    def _stop_bg_pings(self, procs: dict) -> None:
        for p in procs.values():
            p.terminate()
            p.wait()

    def _analyze_ping_logs(self, pairs: List[Tuple]) -> Tuple[int, float]:
        import re
        max_drops = 0
        max_outage = 0.0
        pattern = re.compile(r"\[(\d+\.\d+)\].*icmp_seq=(\d+)")
        
        for h1, h2 in pairs:
            log_file = f"/tmp/ping_{h1.name}_{h2.name}.log"
            try:
                with open(log_file, "r") as f:
                    lines = f.readlines()
            except Exception:
                continue
                
            last_ts = None
            last_seq = None
            
            for line in lines:
                m = pattern.search(line)
                if m:
                    ts = float(m.group(1))
                    seq = int(m.group(2))
                    
                    if last_ts is not None and last_seq is not None:
                        drops = seq - last_seq - 1
                        gap = ts - last_ts
                        if drops > max_drops:
                            max_drops = drops
                        if drops > 0 and gap > max_outage:
                            max_outage = gap
                    
                    last_ts = ts
                    last_seq = seq
        return max_drops, max_outage * 1000.0

    def run(self) -> dict:
        if not MININET_AVAILABLE:
            print("ERROR: Mininet not available. Run on Ubuntu with Mininet installed.")
            return {}

        setLogLevel("warning")
        topo = build_topo(self.topo_name)
        net  = Mininet(
            topo=topo,
            controller=RemoteController("c0", ip=CONTROLLER_IP, port=CONTROLLER_PORT),
            switch=lambda name, **kwargs: OVSSwitch(name, protocols='OpenFlow13', datapath='user', **kwargs),
            link=Link,
            autoSetMacs=True,
        )
        net.start()
        print(f"[chaos] Topology '{self.topo_name}' started. Waiting 10s for discovery...")
        time.sleep(10)

        # Baseline: pingAll before any failures
        print("[chaos] Waking up network (cold-start)...")
        net.pingAll(timeout=1)
        print("[chaos] Measuring baseline...")
        baseline_loss = net.pingAll(timeout=1)   # second pass to ensure all paths learned

        # Get all switch-to-switch links
        sw_links = [
            (l.intf1.node.name, l.intf2.node.name)
            for l in net.links
            if l.intf1.node.name.startswith("s") and l.intf2.node.name.startswith("s")
        ]
        
        # Build local graph to find affected host pairs
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "ryu_controller")))
        from topology_graph import TopologyGraph
        graph = TopologyGraph()
        for s1, s2 in sw_links:
            graph.add_link(int(s1[1:]), 0, int(s2[1:]), 0)
            
        host_to_switch = {}
        for h in net.hosts:
            for intf in h.intfList():
                link = intf.link
                if not link: continue
                other = link.intf1.node if link.intf2.node == h else link.intf2.node
                if other.name.startswith('s'):
                    host_to_switch[h] = int(other.name[1:])

        start_ts  = time.time()
        prev_log_len = 0

        while time.time() - start_ts < self.duration:
            wait = random.uniform(CHAOS_MIN_WAIT, CHAOS_MAX_WAIT)
            time.sleep(min(wait, self.duration - (time.time() - start_ts)))

            if time.time() - start_ts >= self.duration:
                break

            # Pick 1 random link to kill to isolate single-link recovery
            n_kill = 1
            targets = random.sample(sw_links, n_kill)

            # Pre-compute affected host pairs before killing links
            affected_pairs = []
            failed_edges = [(int(s1[1:]), int(s2[1:])) for s1, s2 in targets]
            
            def path_uses_link(path: List[int], a: int, b: int) -> bool:
                for i in range(len(path) - 1):
                    if (path[i] == a and path[i+1] == b) or (path[i] == b and path[i+1] == a):
                        return True
                return False

            for h1 in net.hosts:
                for h2 in net.hosts:
                    if h1 == h2: continue
                    paths = graph.ecmp_paths(host_to_switch[h1], host_to_switch[h2])
                    affected = False
                    for path in paths:
                        for u, v in failed_edges:
                            if path_uses_link(path, u, v):
                                affected = True
                                break
                        if affected: break
                    if affected:
                        affected_pairs.append((h1, h2))

            # Start background pings for affected pairs (limit to 2 to avoid CPU overload)
            sample_pairs = random.sample(affected_pairs, min(2, len(affected_pairs))) if affected_pairs else []
            procs = self._start_bg_pings(sample_pairs, interval=0.005)
            
            # Wait for steady state
            time.sleep(0.5)

            for s1, s2 in targets:
                t_fail = time.time()
                print(f"[chaos] Killing link {s1}-{s2}")
                net.configLinkStatus(s1, s2, "down")
                self.events.append({"event": "down", "link": f"{s1}-{s2}", "ts": t_fail})

            # Let pings capture the failure window and recovery
            time.sleep(2.0)
            
            self._stop_bg_pings(procs)
            
            if affected_pairs:
                drops, outage_ms = self._analyze_ping_logs(sample_pairs)
                self.data_plane_drops.append(drops)
                if drops > 0:
                    self.data_plane_outages.append(outage_ms)
                print(f"[chaos] Data-plane downtime: {drops} drops, max outage = {outage_ms:.1f}ms ({len(affected_pairs)} paths affected, 2 sampled @ 5ms interval)")
            else:
                print(f"[chaos] No paths affected by this failure.")

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
        dt = self.data_plane_outages
        dr = self.data_plane_drops
        results = {
            "topology": self.topo_name,
            "baseline_loss_pct": baseline_loss,
            "final_loss_pct": final_loss,
            "num_failures": len([e for e in self.events if e["event"] == "down"]),
            "recovery_times_ms": rt,
            "mean_recovery_ms":  float(np.mean(rt)) if rt else 0,
            "p50_recovery_ms":   float(np.percentile(rt, 50)) if rt else 0,
            "p95_recovery_ms":   float(np.percentile(rt, 95)) if rt else 0,
            "p99_recovery_ms":   float(np.percentile(rt, 99)) if rt else 0,
            "data_plane_mean_drops": float(np.mean(dr)) if dr else 0,
            "data_plane_mean_outage_ms": float(np.mean(dt)) if dt else 0,
        }
        self._print_table(results)
        self._save_results(results)
        return results

    def _print_table(self, r: dict) -> None:
        print("\n" + "=" * 60)
        print(f"  CHAOS TEST RESULTS — {r['topology'].upper()}")
        print("=" * 60)
        print(f"  Baseline packet loss :  {r['baseline_loss_pct']:.1f}%")
        print(f"  Final packet loss    :  {r['final_loss_pct']:.1f}%")
        print(f"  Failure events       :  {r['num_failures']}")
        print(f"  Recovery samples     :  {len(r['recovery_times_ms'])}")
        if r["recovery_times_ms"]:
            print(f"  Mean recovery (ctrl) :  {r['mean_recovery_ms']:.1f} ms")
            print(f"  p95 recovery  (ctrl) :  {r['p95_recovery_ms']:.1f} ms")
        print(f"  Data-plane drops     :  {r['data_plane_mean_drops']:.1f} pkts/failure (avg)")
        print(f"  Data-plane outage    :  {r['data_plane_mean_outage_ms']:.1f} ms (avg observed gap)")
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
