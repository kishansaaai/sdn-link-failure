"""Owner-checked Redis leases and atomic, fenced controller snapshots."""
from __future__ import annotations
import json
import os
import time
import redis

HEARTBEAT_KEY = "sdn:heartbeat"
STATE_KEY = "sdn:topology_state"
GENERATION_KEY = "sdn:generation"
HEARTBEAT_TTL = 5
HEARTBEAT_INTERVAL = 1

ACQUIRE = """
if redis.call('exists', KEYS[1]) == 0 then
  local generation = redis.call('incr', KEYS[2])
  redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return generation
end
return 0
"""
RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('expire', KEYS[1], ARGV[2])
  return 1
end
return 0
"""
PUBLISH = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('set', KEYS[2], ARGV[2])
  return 1
end
return 0
"""
RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class StateSync:
    def __init__(self, client=None):
        self._redis = client if client is not None else redis.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5,
        )

    def acquire(self, owner: str) -> int:
        return int(self._redis.eval(ACQUIRE, 2, HEARTBEAT_KEY, GENERATION_KEY,
                                    owner, HEARTBEAT_TTL))

    def renew(self, owner: str) -> bool:
        return bool(self._redis.eval(RENEW, 1, HEARTBEAT_KEY, owner, HEARTBEAT_TTL))

    def release(self, owner: str) -> None:
        self._redis.eval(RELEASE, 1, HEARTBEAT_KEY, owner)

    def read_heartbeat(self):
        # Connection failures deliberately propagate; they are not lease expiry.
        return self._redis.get(HEARTBEAT_KEY)

    def push_state(self, owner: str, state: dict) -> bool:
        payload = json.dumps({**state, "ts": time.time(), "version": 1}, allow_nan=False)
        return bool(self._redis.eval(PUBLISH, 2, HEARTBEAT_KEY, STATE_KEY, owner, payload))

    def pull_state(self) -> dict | None:
        raw = self._redis.get(STATE_KEY)
        if raw is None:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Unsupported controller snapshot")
        return data
