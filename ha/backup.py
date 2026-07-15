"""
backup.py — Backup controller with leader election logic.

Monitors primary heartbeat via Redis. On TTL expiry:
  1. Promotes itself to primary role
  2. Loads last known topology state from Redis
  3. Sends OFPRoleRequest(MASTER) to all connected switches
  4. Records promotion timestamp (for measuring control-plane failover time)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ha.state_sync import HEARTBEAT_TTL, StateSync
from ryu_controller.topology_graph import TopologyGraph

log = logging.getLogger(__name__)

POLL_INTERVAL = 1.0   # check heartbeat every second


class LeaderElection:
    """
    State machine:
      WATCHING  → primary is alive, backup is standby
      PROMOTING → backup detected failure, loading state
      PRIMARY   → backup has taken over
    """
    WATCHING  = "WATCHING"
    PROMOTING = "PROMOTING"
    PRIMARY   = "PRIMARY"

    def __init__(self,
                 on_promote: Optional[Callable] = None,
                 instance_id: str = "backup"):
        self.state = self.WATCHING
        self.instance_id = instance_id
        self.on_promote = on_promote   # callback when promotion happens
        self.sync = StateSync()
        self._running = False
        self._thread: threading.Thread | None = None
        self.promote_ts: Optional[float] = None
        self.detect_ts:  Optional[float] = None

    @property
    def is_primary(self) -> bool:
        return self.state == self.PRIMARY

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="backup-election", daemon=True
        )
        self._thread.start()
        log.info("LeaderElection started, watching primary heartbeat")

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            time.sleep(POLL_INTERVAL)
            if self.state == self.WATCHING:
                if not self.sync.is_primary_alive():
                    self.detect_ts = time.time()
                    log.warning("Primary heartbeat lost — initiating promotion")
                    self.state = self.PROMOTING
                    self._promote()

    def _promote(self) -> None:
        self.state = self.PROMOTING
        # Load last known state
        state_data = self.sync.pull_state()
        graph: Optional[TopologyGraph] = None
        recovery_log = []
        if state_data:
            graph = TopologyGraph.from_dict(state_data.get("graph", {}))
            recovery_log = state_data.get("recovery_log", [])
            age = time.time() - state_data.get("ts", 0)
            log.info("Loaded synced state (age=%.1fs, %d hosts, %d switches)",
                     age, len(graph.hosts), len(graph.adj))
        else:
            log.warning("No synced state found — starting from scratch")
            graph = TopologyGraph()

        self.promote_ts = time.time()
        self.state = self.PRIMARY

        # Start writing own heartbeat so a subsequent backup sees us as primary
        self.sync.write_heartbeat(self.instance_id)

        elapsed_ms = (self.promote_ts - self.detect_ts) * 1000
        log.info("PROMOTED to PRIMARY in %.1f ms (control-plane failover)",
                 elapsed_ms)

        if self.on_promote:
            self.on_promote(graph, recovery_log, elapsed_ms)

    def failover_time_ms(self) -> Optional[float]:
        if self.detect_ts and self.promote_ts:
            return (self.promote_ts - self.detect_ts) * 1000
        return None
