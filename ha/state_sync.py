"""
state_sync.py — Redis-backed topology + flow state synchronisation.

Primary controller writes state every heartbeat interval.
Backup controller reads state and takes over if the heartbeat TTL expires.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

log = logging.getLogger(__name__)

REDIS_HOST      = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT      = int(os.getenv("REDIS_PORT", "6379"))
HEARTBEAT_KEY   = "sdn:heartbeat"
STATE_KEY        = "sdn:topology_state"
HEARTBEAT_TTL   = 5    # seconds — backup promotes if this expires
HEARTBEAT_INTERVAL = 2  # seconds between primary writes


class StateSync:
    """
    Thin wrapper around Redis for SDN controller state sharing.
    Falls back to a no-op if Redis is not available (single-controller mode).
    """

    def __init__(self):
        self._redis: Optional[object] = None
        if REDIS_AVAILABLE:
            try:
                self._redis = redis.Redis(
                    host=REDIS_HOST, port=REDIS_PORT,
                    decode_responses=True, socket_connect_timeout=2,
                )
                self._redis.ping()
                log.info("Redis connected at %s:%d", REDIS_HOST, REDIS_PORT)
            except Exception as e:
                log.warning("Redis unavailable (%s) — running in standalone mode", e)
                self._redis = None
        else:
            log.warning("redis-py not installed — running in standalone mode")

    @property
    def available(self) -> bool:
        return self._redis is not None

    # ------------------------------------------------------------------
    # Heartbeat (primary → Redis → backup monitors)
    # ------------------------------------------------------------------

    def write_heartbeat(self, instance_id: str) -> None:
        """Primary calls this in a loop. TTL auto-expires if primary dies."""
        if not self._redis:
            return
        try:
            self._redis.setex(HEARTBEAT_KEY, HEARTBEAT_TTL, instance_id)
        except Exception as e:
            log.warning("Heartbeat write failed: %s", e)

    def read_heartbeat(self) -> Optional[str]:
        """Returns current primary instance_id, or None if key expired."""
        if not self._redis:
            return "standalone"
        try:
            return self._redis.get(HEARTBEAT_KEY)
        except Exception:
            return None

    def is_primary_alive(self) -> bool:
        return self.read_heartbeat() is not None

    # ------------------------------------------------------------------
    # Topology state (primary writes, backup reads on takeover)
    # ------------------------------------------------------------------

    def push_state(self, graph_dict: dict, recovery_log: list) -> None:
        if not self._redis:
            return
        try:
            payload = json.dumps({
                "graph": graph_dict,
                "recovery_log": recovery_log,
                "ts": time.time(),
            })
            self._redis.set(STATE_KEY, payload)
        except Exception as e:
            log.warning("State push failed: %s", e)

    def pull_state(self) -> Optional[dict]:
        if not self._redis:
            return None
        try:
            raw = self._redis.get(STATE_KEY)
            return json.loads(raw) if raw else None
        except Exception as e:
            log.warning("State pull failed: %s", e)
            return None
