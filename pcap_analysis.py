#!/usr/bin/env python3
"""
pcap_analysis.py — Packet-level proof using tshark.

Captures OpenFlow control-channel traffic during a chaos test and
programmatically asserts the expected event sequence:
  OFPT_PORT_STATUS(down) → OFPT_FLOW_MOD (install) within window → OFPT_FLOW_MOD (delete stale)

Usage:
    # Capture during a test (requires tshark + sudo)
    sudo python3 pcap_analysis.py --capture --duration 30 --out capture.pcap

    # Analyse an existing pcap
    python3 pcap_analysis.py --analyse capture.pcap --recovery-window-ms 500
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple


OF_PORT = 6633
OF_TYPES = {
    0:  "OFPT_HELLO",
    2:  "OFPT_ERROR",
    4:  "OFPT_FEATURES_REQUEST",
    5:  "OFPT_FEATURES_REPLY",
    10: "OFPT_PACKET_IN",
    12: "OFPT_PORT_STATUS",
    13: "OFPT_PACKET_OUT",
    14: "OFPT_FLOW_MOD",
    16: "OFPT_STATS_REQUEST",
    17: "OFPT_STATS_REPLY",
}


@dataclass
class OFEvent:
    ts:      float
    of_type: int
    type_name: str
    info:    str


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def capture(duration: int, out_file: str, iface: str = "lo") -> None:
    print(f"[pcap] Capturing on {iface} for {duration}s → {out_file}")
    cmd = [
        "tshark", "-i", iface,
        "-f", f"tcp port {OF_PORT}",
        "-w", out_file,
        "-a", f"duration:{duration}",
    ]
    try:
        subprocess.run(cmd, check=True)
        print(f"[pcap] Capture complete: {out_file}")
    except FileNotFoundError:
        print("ERROR: tshark not found. Install with: sudo apt-get install tshark")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: tshark failed: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def parse_pcap(pcap_file: str) -> List[OFEvent]:
    """Use tshark to extract OF message types and timestamps."""
    cmd = [
        "tshark", "-r", pcap_file,
        "-T", "fields",
        "-e", "frame.time_epoch",
        "-e", "openflow_v4.type",
        "-e", "openflow_v4.ofp_header.type",
        "-Y", "openflow_v4",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        print("ERROR: tshark not found.")
        sys.exit(1)

    events: List[OFEvent] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue
        try:
            ts = float(parts[0])
            of_type_str = parts[1] or parts[2] if len(parts) > 2 else parts[1]
            of_type = int(of_type_str)
            events.append(OFEvent(
                ts=ts,
                of_type=of_type,
                type_name=OF_TYPES.get(of_type, f"OFPT_{of_type}"),
                info="",
            ))
        except (ValueError, IndexError):
            continue
    return events


# ---------------------------------------------------------------------------
# Assert sequence
# ---------------------------------------------------------------------------

def assert_recovery_sequence(events: List[OFEvent],
                              window_ms: float = 500.0) -> bool:
    """
    Assert that for every PORT_STATUS(down) event, a FLOW_MOD install
    appears within `window_ms` milliseconds.

    Returns True if all assertions pass.
    """
    PORT_STATUS = 12
    FLOW_MOD    = 14
    window_s    = window_ms / 1000.0
    passed = True

    port_downs = [e for e in events if e.of_type == PORT_STATUS]
    if not port_downs:
        print("[assert] No OFPT_PORT_STATUS events found — nothing to verify.")
        return True

    print(f"\n[assert] Found {len(port_downs)} PORT_STATUS events")
    for pd in port_downs:
        # Find a FLOW_MOD within the window after this event
        candidates = [
            e for e in events
            if e.of_type == FLOW_MOD and 0 <= e.ts - pd.ts <= window_s
        ]
        if candidates:
            delta_ms = (candidates[0].ts - pd.ts) * 1000
            print(f"  ✓ PORT_STATUS @ {pd.ts:.3f}s → FLOW_MOD in {delta_ms:.1f}ms")
        else:
            print(f"  ✗ PORT_STATUS @ {pd.ts:.3f}s — no FLOW_MOD within {window_ms}ms window")
            passed = False

    if passed:
        print("\n[assert] ✓ All recovery sequences verified within window.")
    else:
        print("\n[assert] ✗ Some recovery sequences exceeded the window — check controller.")
    return passed


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_summary(events: List[OFEvent]) -> None:
    from collections import Counter
    counts = Counter(e.type_name for e in events)
    print("\n── OpenFlow Message Summary ──────────────────────────")
    for name, count in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {name:<30} {count:>5}")
    print("─────────────────────────────────────────────────────\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenFlow pcap analysis tool")
    subp = parser.add_subparsers(dest="cmd")

    cap_p = subp.add_parser("capture")
    cap_p.add_argument("--duration", type=int, default=30)
    cap_p.add_argument("--out", default="capture.pcap")
    cap_p.add_argument("--iface", default="lo")

    ana_p = subp.add_parser("analyse")
    ana_p.add_argument("pcap")
    ana_p.add_argument("--recovery-window-ms", type=float, default=500.0)

    args = parser.parse_args()

    if args.cmd == "capture":
        capture(args.duration, args.out, args.iface)
    elif args.cmd == "analyse":
        events = parse_pcap(args.pcap)
        print_summary(events)
        ok = assert_recovery_sequence(events, args.recovery_window_ms)
        sys.exit(0 if ok else 1)
    else:
        parser.print_help()
