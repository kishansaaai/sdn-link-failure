"""
primary.py — Primary controller launcher with heartbeat + state sync.

Run alongside the Ryu controller process. In production this would be
a RyuApp mixin; here it's a standalone thread wrapper so it can be
imported and tested without a full Ryu environment.
"""
from __future__ import annotations

import logging
import threading
import time

from ha.state_sync import HEARTBEAT_INTERVAL, StateSync

log = logging.getLogger(__name__)

INSTANCE_ID = "primary"


class PrimaryController:
    """
    Wraps an SDNController and periodically writes heartbeat + state to Redis.
    Usage:
        primary = PrimaryController(controller)
        primary.start()
    """

    def __init__(self, controller=None):
        self.controller = controller
        self.sync = StateSync()
        self._running = False
        self._thread: threading.Thread | None = None
        self.role = "primary"

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="primary-heartbeat", daemon=True
        )
        self._thread.start()
        log.info("PrimaryController heartbeat started (instance=%s)", INSTANCE_ID)

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            self.sync.write_heartbeat(INSTANCE_ID)
            if self.controller:
                graph_dict = self.controller.graph.to_dict()
                recovery_log = self.controller.recovery_log
                self.sync.push_state(graph_dict, recovery_log)
            time.sleep(HEARTBEAT_INTERVAL)
