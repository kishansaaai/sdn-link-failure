"""Root-only real-switch integration: connectivity, ECMP, failures, restoration, HA.

Run with the system Python (for distro Mininet), passing the controller venv.
Uses dedicated controller/Redis ports and stops only processes it starts.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from topologies.runner import create_network

ROOT = Path(__file__).resolve().parents[1]


def get_json(port, endpoint):
    with urlopen(f"http://127.0.0.1:{port}/{endpoint}", timeout=2) as response:
        return json.load(response)


def wait_for(predicate, timeout=20, message="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {message}: {last}")


def ping(source, destination, count=3, timeout=10):
    """Wait for packet recovery, recording every probe including transient loss.

    A topology API update precedes switch execution. A first packet may still
    be lost during replacement; require eventual consecutive success within a
    bound instead of confusing graph readiness with lossless dataplane updates.
    Baseline/final all-pairs checks remain strict with no retry.
    """
    deadline = time.monotonic() + timeout
    attempts = []
    while time.monotonic() < deadline:
        output = source.cmd(f"ping -n -c {count} -W 1 -i 0.1 {destination.IP()}")
        summary = re.search(r"(\d+) packets transmitted, (\d+) received", output)
        if summary:
            sent, received = map(int, summary.groups())
            attempts.append({"sent": sent, "received": received})
            if sent == received == count:
                return attempts
        time.sleep(0.1)
    raise AssertionError(f"Packet recovery failed within {timeout}s: {attempts}; {output}")


def run_topology(name, python, datapath, output_dir):
    processes, handles = [], []
    net = None
    result = {"topology": name}
    env = {**os.environ, "CONTROLLER_ROLE": "primary", "REDIS_PORT": "16379",
           "OF_PORT": "16633", "API_PORT": "15000", "PYTHONPATH": str(ROOT)}
    def launch(args, label, environment=None):
        handle = (output_dir / f"{name}-{label}.log").open("w")
        handles.append(handle)
        process = subprocess.Popen(args, cwd=ROOT, env=environment, stdout=handle,
                                   stderr=subprocess.STDOUT)
        processes.append(process)
        return process

    try:
        launch(["redis-server", "--bind", "127.0.0.1", "--port", "16379",
                "--save", "", "--appendonly", "no"], "redis")
        primary = launch([python, "start_ryu.py"], "primary", env)
        wait_for(lambda: get_json(15000, "health")["role"] == "primary", message="primary")
        backup = launch([python, "start_ryu.py"], "backup",
                        {**env, "CONTROLLER_ROLE": "backup", "OF_PORT": "16634", "API_PORT": "15001"})
        wait_for(lambda: get_json(15001, "health")["role"] == "backup", message="backup")
        net = create_network(name, ports=(16633, 16634), datapath=datapath, shaped=False)
        net.start()
        expected = sum(1 for link in net.links
                       if link.intf1.node in net.switches and link.intf2.node in net.switches) * 2
        wait_for(lambda: sum(len(v) for v in get_json(15000, "topology")["adj"].values()) == expected,
                 message=f"all {expected // 2} links discovered")
        wait_for(lambda: get_json(15000, "health")["ready_switches"] == len(net.switches),
                 message="master roles")
        net.pingAll(timeout="1")
        baseline = net.pingAll(timeout="1")
        assert baseline == 0, f"Baseline loss {baseline}%"
        result["baseline_loss_pct"] = baseline
        # Select an actual installed multi-switch route, then break its first edge.
        paths = get_json(15000, "paths")
        ecmp_count = sum(len(p["paths"]) > 1 for p in paths)
        assert ecmp_count > 0, "No ECMP routes installed"
        result["ecmp_routes"] = ecmp_count
        selected = next(p for p in paths if p["paths"] and len(p["paths"][0]) > 1)
        hosts = {h.MAC(): h for h in net.hosts}
        source, destination = hosts[selected["src_mac"]], hosts[selected["dst_mac"]]
        by_dpid = {int(s.dpid, 16): s.name for s in net.switches}
        a, b = selected["paths"][0][:2]
        start = time.monotonic()
        net.configLinkStatus(by_dpid[a], by_dpid[b], "down")
        def recovered():
            routes = get_json(15000, "paths")
            record = next(p for p in routes if p["src_mac"] == source.MAC()
                          and p["dst_mac"] == destination.MAC())
            return record["paths"] and all(
                all({u, v} != {a, b} for u, v in zip(path, path[1:]))
                for path in record["paths"])
        wait_for(recovered, message="alternate route")
        result["failure_recovery_probes"] = ping(source, destination)
        result["failure_to_verified_ping_ms"] = (time.monotonic() - start) * 1000
        net.configLinkStatus(by_dpid[a], by_dpid[b], "up")
        wait_for(lambda: str(b) in get_json(15000, "topology")["adj"][str(a)], message="restoration")
        result["restoration_probes"] = ping(source, destination)
        result["restoration"] = "passed"
        # Isolate the destination switch; stale rules must disappear, then return.
        destination_dpid = get_json(15000, "topology")["hosts"][destination.MAC()]["dpid"]
        adjacent = list(get_json(15000, "topology")["adj"][str(destination_dpid)])
        for neighbor in adjacent:
            net.configLinkStatus(by_dpid[destination_dpid], by_dpid[int(neighbor)], "down")
        wait_for(lambda: any(p["src_mac"] == source.MAC() and p["dst_mac"] == destination.MAC()
                             and p["status"] == "unreachable" for p in get_json(15000, "paths")),
                 message="partition reporting")
        for neighbor in adjacent:
            net.configLinkStatus(by_dpid[destination_dpid], by_dpid[int(neighbor)], "up")
        wait_for(lambda: len(get_json(15000, "topology")["adj"][str(destination_dpid)]) == len(adjacent),
                 message="partition healing")
        result["partition_healing_probes"] = ping(source, destination)
        result["partition_and_healing"] = "passed"
        # Allow one snapshot, kill the lease owner, and verify backup writes.
        time.sleep(1.5)
        start = time.monotonic()
        primary.kill()
        primary.wait(timeout=5)
        wait_for(lambda: get_json(15001, "health")["role"] == "primary", timeout=12, message="HA takeover")
        wait_for(lambda: get_json(15001, "health")["ready_switches"] == len(net.switches), message="backup master roles")
        result["takeover_probes"] = ping(source, destination)
        result["controller_takeover_ms"] = (time.monotonic() - start) * 1000
        # A new link failure after takeover proves active control, not stale flows.
        route = next(p for p in get_json(15001, "paths") if p["src_mac"] == source.MAC()
                     and p["dst_mac"] == destination.MAC())["paths"][0]
        a, b = route[:2]
        net.configLinkStatus(by_dpid[a], by_dpid[b], "down")
        wait_for(lambda: str(b) not in get_json(15001, "topology")["adj"][str(a)], message="backup handles failure")
        result["failure_after_takeover_probes"] = ping(source, destination)
        net.configLinkStatus(by_dpid[a], by_dpid[b], "up")
        wait_for(lambda: str(b) in get_json(15001, "topology")["adj"][str(a)], message="backup handles restoration")
        result["failure_after_takeover"] = "passed"
        # Restart former primary; the lease prevents it from stealing leadership.
        launch([python, "start_ryu.py"], "restarted-primary", env)
        wait_for(lambda: get_json(15000, "health")["role"] == "backup", message="former primary stays standby")
        assert get_json(15001, "health")["role"] == "primary"
        result["former_primary_restart"] = "passed"
        assert backup.poll() is None
        final_loss = net.pingAll(timeout="1")
        assert final_loss == 0
        result["final_loss_pct"] = final_loss
        result["recovery_events"] = len(get_json(15001, "recovery-log"))
        for handle in handles:
            handle.flush()
        for log in output_dir.glob(f"{name}-*.log"):
            text = log.read_text()
            assert "OpenFlow error" not in text, f"Protocol error: {log}"
            assert "Traceback" not in text, f"Crash: {log}"
        result["openflow_errors"] = 0
        result["status"] = "passed"
        return result
    finally:
        if net is not None:
            net.stop()
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for handle in handles:
            handle.close()


def run_reuse(python, datapath, output_dir):
    """Reuse identical switch IDs/port numbers across different topologies."""
    log_path = output_dir / "reuse-controller.log"
    env = {**os.environ, "CONTROLLER_ROLE": "standalone", "OF_PORT": "16635",
           "API_PORT": "15002", "PYTHONPATH": str(ROOT)}
    with log_path.open("w") as handle:
        process = subprocess.Popen([python, "start_ryu.py"], cwd=ROOT, env=env,
                                   stdout=handle, stderr=subprocess.STDOUT)
        results = []
        try:
            wait_for(lambda: get_json(15002, "health")["role"] == "primary")
            for name in ("mesh", "ring", "fattree"):
                net = create_network(name, ports=(16635,), datapath=datapath, shaped=False)
                try:
                    net.start()
                    expected = sum(link.intf1.node in net.switches and link.intf2.node in net.switches
                                   for link in net.links) * 2
                    wait_for(lambda: sum(len(n) for n in get_json(15002, "topology")["adj"].values()) == expected,
                             message=f"reused controller discovers {name}")
                    loss = net.pingAll(timeout="1")
                    assert loss == 0, f"Reusing controller for {name}: {loss}% loss"
                    results.append({"topology": name, "loss_pct": loss})
                finally:
                    net.stop()
                wait_for(lambda: get_json(15002, "health")["switches"] == 0,
                         message="old switch sessions close")
            return {"topology": "reuse", "status": "passed", "runs": results}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller-python", default=str(ROOT / ".venv/bin/python"))
    parser.add_argument("--topo", choices=["ring", "mesh", "fattree", "reuse", "all"], default="all")
    parser.add_argument("--datapath", choices=["kernel", "user"], default="kernel")
    parser.add_argument("--output-dir", default="benchmarks/live")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Integration tests require root")
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    names = ["ring", "mesh", "fattree"] if args.topo == "all" else [args.topo]
    results = []
    for name in names:
        print(f"Testing {name}", flush=True)
        if name == "reuse":
            results.append(run_reuse(args.controller_python, args.datapath, output))
        else:
            results.append(run_topology(name, args.controller_python, args.datapath, output))
        print(json.dumps(results[-1], indent=2), flush=True)
        (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    if args.topo == "all":
        results.append(run_reuse(args.controller_python, args.datapath, output))
        (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
