#!/usr/bin/env python3
"""Inspect OF1.3 control-channel evidence; this does not prove packet recovery.

Capture: python3 pcap_analysis.py capture --duration 30 --out capture.pcap
Analyse: python3 pcap_analysis.py analyse capture.pcap --recovery-window-ms 500
The strict check needs a capture where each observed down port affects a route.
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import dataclass
import subprocess
import xml.etree.ElementTree as ET

OF_TYPES = {0: "HELLO", 1: "ERROR", 2: "ECHO_REQUEST", 3: "ECHO_REPLY",
            5: "FEATURES_REQUEST", 6: "FEATURES_REPLY", 10: "PACKET_IN",
            12: "PORT_STATUS", 13: "PACKET_OUT", 14: "FLOW_MOD", 15: "GROUP_MOD",
            18: "MULTIPART_REQUEST", 19: "MULTIPART_REPLY",
            20: "BARRIER_REQUEST", 21: "BARRIER_REPLY", 24: "ROLE_REQUEST", 25: "ROLE_REPLY"}


@dataclass
class OFEvent:
    ts: float
    of_type: int
    type_name: str = ""
    info: str = ""
    stream: int = -1
    port_down: bool = False
    command: int | None = None


def capture(duration, out_file, iface="lo", port=6633):
    subprocess.run(["tshark", "-i", iface, "-f", f"tcp port {port}",
                    "-w", out_file, "-a", f"duration:{duration}"], check=True)


def parse_pdml(text):
    """PDML preserves multiple OpenFlow messages coalesced into a TCP frame."""
    result = []
    for frame in ET.fromstring(text).findall("packet"):
        fields = {f.get("name"): f.get("show") for f in frame.iter("field")}
        ts = float(fields.get("frame.time_epoch", "0"))
        stream = int(fields.get("tcp.stream", "-1"))
        for proto in frame.findall(".//proto[@name='openflow_v4']"):
            values = {f.get("name"): f.get("show") for f in proto.iter("field")}
            if "openflow_v4.type" not in values:
                continue
            kind = int(values["openflow_v4.type"], 0)
            down = (values.get("openflow_v4.port.state.link_down") == "1"
                    or values.get("openflow_v4.port.config.port_down") == "1"
                    or values.get("openflow_v4.port_status.reason") == "1")
            command = values.get("openflow_v4.flowmod.command")
            result.append(OFEvent(ts, kind, OF_TYPES.get(kind, str(kind)),
                                  stream=stream, port_down=down,
                                  command=int(command, 0) if command else None))
    return sorted(result, key=lambda event: event.ts)


def parse_pcap(pcap_file, port=6633):
    output = subprocess.run(["tshark", "-r", str(pcap_file), "-d", f"tcp.port=={port},openflow",
                             "-Y", "openflow_v4", "-T", "pdml"],
                            capture_output=True, text=True, check=True)
    return parse_pdml(output.stdout)


def assert_recovery_sequence(events, window_ms=500):
    if window_ms <= 0:
        raise ValueError("Recovery window must be positive")
    downs = [e for e in events if e.of_type == 12 and e.port_down]
    if not downs:
        print("No port-down evidence; recovery cannot be verified.")
        return False
    passed = True
    for down in downs:
        candidates = [e for e in events if e.of_type == 14 and e.command in (0, 1, 2)
                      and e.stream == down.stream
                      and 0 <= (e.ts - down.ts) * 1000 <= window_ms]
        if not candidates:
            print(f"No add/modify on stream {down.stream} within {window_ms} ms of {down.ts}")
            passed = False
    return passed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cap = commands.add_parser("capture")
    cap.add_argument("--duration", type=int, default=30)
    cap.add_argument("--out", default="capture.pcap")
    cap.add_argument("--iface", default="lo")
    cap.add_argument("--port", type=int, default=6633)
    analyze = commands.add_parser("analyse")
    analyze.add_argument("pcap")
    analyze.add_argument("--port", type=int, default=6633)
    analyze.add_argument("--recovery-window-ms", type=float, default=500)
    args = parser.parse_args()
    if args.command == "capture":
        capture(args.duration, args.out, args.iface, args.port)
    else:
        events = parse_pcap(args.pcap, args.port)
        print(dict(Counter(event.type_name for event in events)))
        raise SystemExit(0 if assert_recovery_sequence(events, args.recovery_window_ms) else 1)


if __name__ == "__main__":
    main()
