#!/usr/bin/env python3
"""Measure actual link failures with Mininet, continuous ping and controller API."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import random
import re
import signal
import statistics
import tempfile
import time
from urllib.request import urlopen

from topologies.runner import create_network  # build_topo is a public compatibility API


def api_get(base, endpoint):
    with urlopen(base.rstrip("/") + "/" + endpoint, timeout=3) as response:
        return json.load(response)


def analyze_ping(text, start_ts=None, stop_ts=None):
    """Use replies only; include leading and terminal outages, not just inner gaps.

    reply_gap_ms includes one normal ping interval. It is an observation at the
    chosen sampling resolution, not an exact hardware failover measurement.
    """
    replies = [(float(ts), int(seq)) for ts, seq in re.findall(
        r"\[([0-9.]+)\].*bytes from .*icmp_seq=(\d+)", text)]
    summary = re.search(r"(\d+) packets transmitted, (\d+)(?: packets)? received", text)
    sent, received = (int(summary[1]), int(summary[2])) if summary else (None, len(replies))
    gaps = [b[0] - a[0] for a, b in zip(replies, replies[1:])]
    if start_ts is not None and stop_ts is not None:
        gaps.append((replies[0][0] if replies else stop_ts) - start_ts)
        if replies:
            gaps.append(stop_ts - replies[-1][0])
    return {
        "sent": sent, "received": received,
        "loss_pct": (100 * (sent - received) / sent) if sent else None,
        "max_reply_gap_ms": max(gaps, default=0) * 1000 if gaps else None,
        "recovered": bool(replies),
    }


def percentile(values, percent):
    if not values:
        return None
    data = sorted(values)
    rank = (len(data) - 1) * percent / 100
    lo = int(rank)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (rank - lo)


class ChaosTest:
    def __init__(self, topo_name, duration, api_base, controller_ip="127.0.0.1",
                 ports=(6633,), datapath="kernel", seed=0, output="benchmarks",
                 interval=0.02, shaped=True):
        if duration <= 0 or interval <= 0:
            raise ValueError("Duration and ping interval must be positive")
        self.topo_name, self.duration, self.api_base = topo_name, duration, api_base
        self.controller_ip, self.ports, self.datapath = controller_ip, ports, datapath
        self.random = random.Random(seed)
        self.output = Path(output)
        self.interval, self.shaped = interval, shaped
        self.events = []

    def run(self):
        net = create_network(self.topo_name, self.controller_ip, self.ports,
                             self.datapath, self.shaped)
        try:
            net.start()
            expected_links = sum(link.intf1.node in net.switches and link.intf2.node in net.switches
                                 for link in net.links) * 2
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                topology = api_get(self.api_base, "topology")
                if sum(len(n) for n in topology["adj"].values()) == expected_links:
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("Topology discovery did not complete")
            net.pingAll(timeout="1")
            baseline = net.pingAll(timeout="1")
            if baseline:
                raise RuntimeError(f"Baseline connectivity failed: {baseline}% loss")
            host_by_mac = {host.MAC(): host for host in net.hosts}
            switches = {int(sw.dpid, 16): sw.name for sw in net.switches}
            deadline = time.monotonic() + self.duration
            while time.monotonic() < deadline:
                routes = [r for r in api_get(self.api_base, "paths")
                          if r["paths"] and len(r["paths"][0]) > 1]
                if not routes:
                    raise RuntimeError("No installed inter-switch paths to test")
                selected = self.random.choice(routes)
                path = self.random.choice(selected["paths"])
                a, b = self.random.choice(list(zip(path, path[1:])))
                source, destination = (host_by_mac[selected[field]]
                                       for field in ("src_mac", "dst_mac"))
                with tempfile.TemporaryFile(mode="w+") as capture:
                    started = time.time()
                    process = source.popen(["ping", "-n", "-D", "-i", str(self.interval),
                                            destination.IP()], stdout=capture, stderr=capture)
                    failed_at = None
                    try:
                        time.sleep(0.5)
                        failed_at = time.time()
                        net.configLinkStatus(switches[a], switches[b], "down")
                        time.sleep(2)
                        # Verify delivery while the failed link is still down.
                        check = source.cmd(f"ping -n -c 2 -W 1 {destination.IP()}")
                        recovered = " 0% packet loss" in check
                        net.configLinkStatus(switches[a], switches[b], "up")
                        time.sleep(1.5)
                    finally:
                        if failed_at is not None:
                            net.configLinkStatus(switches[a], switches[b], "up")
                        process.send_signal(signal.SIGINT)
                        try:
                            process.wait(timeout=3)
                        except Exception:
                            process.kill()
                            process.wait(timeout=3)
                    stopped = time.time()
                    capture.seek(0)
                    ping_result = analyze_ping(capture.read(), started, stopped)
                    ping_result["recovered"] = recovered
                logs = [entry for entry in api_get(self.api_base, "recovery-log")
                        if entry["ts"] >= failed_at and entry["reason"] == "failure"
                        and entry["status"] == "rerouted"
                        and entry["src_mac"] == source.MAC()
                        and entry["dst_mac"] == destination.MAC()]
                event = {"link": [a, b], "failure_ts": failed_at,
                         "src_mac": source.MAC(), "dst_mac": destination.MAC(),
                         "ping": ping_result,
                         "controller_enqueue_ms": [entry["recovery_ms"] for entry in logs]}
                self.events.append(event)
                print(json.dumps(event), flush=True)
                # Wait for restored LLDP links before selecting the next failure.
                restore_deadline = time.monotonic() + 10
                while str(b) not in api_get(self.api_base, "topology")["adj"].get(str(a), {}):
                    if time.monotonic() > restore_deadline:
                        raise RuntimeError("Failed link did not rediscover after restoration")
                    time.sleep(0.2)
            final = net.pingAll(timeout="1")
            samples = [n for event in self.events for n in event["controller_enqueue_ms"]]
            results = {
                "topology": self.topo_name, "baseline_loss_pct": baseline,
                "final_loss_pct": final, "num_failures": len(self.events),
                "ping_interval_ms": self.interval * 1000,
                "mean_controller_enqueue_ms": statistics.mean(samples) if samples else None,
                "p95_controller_enqueue_ms": percentile(samples, 95),
                "events": self.events,
                "passed": final == 0 and bool(self.events)
                          and all(e["ping"]["recovered"] for e in self.events),
            }
            self.output.mkdir(parents=True, exist_ok=True)
            (self.output / f"{self.topo_name}_results.json").write_text(json.dumps(results, indent=2) + "\n")
            return results
        finally:
            net.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topo", choices=["ring", "mesh", "fattree"], default="mesh")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--api", default="http://127.0.0.1:5000")
    parser.add_argument("--controller-ip", default="127.0.0.1")
    parser.add_argument("--ports", default="6633")
    parser.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", default="benchmarks")
    parser.add_argument("--interval", type=float, default=0.02)
    parser.add_argument("--unshaped", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Mininet requires root")
    test = ChaosTest(args.topo, args.duration, args.api, args.controller_ip,
                     [int(p) for p in args.ports.split(",")], args.datapath,
                     args.seed, args.output, args.interval, not args.unshaped)
    result = test.run()
    print(json.dumps({k: v for k, v in result.items() if k != "events"}, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
