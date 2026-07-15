"""
group_allocator.py — Deterministic, collision-free OpenFlow group-ID allocator.

Problem this replaces:
    group_id = hash(key) & 0xFFFFFFFF

    - Python randomizes hash() for str/tuple per-process (PYTHONHASHSEED) by
      default, so the SAME (src_mac, dst_mac) key gets a DIFFERENT group_id
      every controller restart — stale group entries on switches become
      orphaned and unreachable by ID.
    - Two different keys can collide on the same 32-bit value, silently
      overwriting one flow's ECMP group with another's buckets.

This allocator hands out small sequential integers, tracks key <-> group_id
in both directions, reclaims freed IDs, and optionally persists the mapping
to Redis so a promoted backup controller reuses the same IDs the primary
was using instead of renumbering everything on failover.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

GROUP_ID_MIN = 1            # 0 is reserved in some OF1.3 implementations
GROUP_ID_MAX = 0xFFFFFF00   # leave headroom below the OFPG_* reserved range
REDIS_MAP_KEY = "sdn:group_id_map"

FlowKey = Tuple[str, str]  # (src_mac, dst_mac)


class GroupIdAllocator:
    """Thread-safe, collision-free allocator for OF1.3 group IDs."""

    def __init__(self, redis_client=None):
        self._lock = threading.Lock()
        self._key_to_id: Dict[FlowKey, int] = {}
        self._id_to_key: Dict[int, FlowKey] = {}
        self._next_id = GROUP_ID_MIN
        self._free_ids: List[int] = []
        self._redis = redis_client
        if self._redis is not None:
            self._load_from_redis()

    def allocate(self, key: FlowKey) -> int:
        """Return the group_id for `key`, allocating a new one if needed."""
        with self._lock:
            if key in self._key_to_id:
                return self._key_to_id[key]

            if self._free_ids:
                gid = self._free_ids.pop()
            else:
                if self._next_id > GROUP_ID_MAX:
                    raise RuntimeError("Group ID space exhausted")
                gid = self._next_id
                self._next_id += 1

            self._key_to_id[key] = gid
            self._id_to_key[gid] = key
            self._persist()
            log.debug("GroupIdAllocator: assigned group %d to %s", gid, key)
            return gid

    def release(self, key: FlowKey) -> None:
        """Free the group_id for `key` so it can be reused later."""
        with self._lock:
            gid = self._key_to_id.pop(key, None)
            if gid is not None:
                self._id_to_key.pop(gid, None)
                self._free_ids.append(gid)
                self._persist()
                log.debug("GroupIdAllocator: released group %d (%s)", gid, key)

    def get(self, key: FlowKey) -> Optional[int]:
        with self._lock:
            return self._key_to_id.get(key)

    # -- persistence (survives controller restart / backup takeover) -----

    def _persist(self) -> None:
        if self._redis is None:
            return
        try:
            payload = json.dumps({
                "next_id": self._next_id,
                "free_ids": self._free_ids,
                "map": {f"{s}|{d}": gid for (s, d), gid in self._key_to_id.items()},
            })
            self._redis.set(REDIS_MAP_KEY, payload)
        except Exception as e:
            log.warning("GroupIdAllocator: persist failed: %s", e)

    def _load_from_redis(self) -> None:
        try:
            raw = self._redis.get(REDIS_MAP_KEY)
            if not raw:
                return
            data = json.loads(raw)
            self._next_id = data.get("next_id", GROUP_ID_MIN)
            self._free_ids = data.get("free_ids", [])
            for combined, gid in data.get("map", {}).items():
                src, dst = combined.split("|", 1)
                self._key_to_id[(src, dst)] = gid
                self._id_to_key[gid] = (src, dst)
            log.info("GroupIdAllocator: restored %d group mappings from Redis",
                      len(self._key_to_id))
        except Exception as e:
            log.warning("GroupIdAllocator: restore failed: %s", e)
