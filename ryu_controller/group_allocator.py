"""Collision-free local group IDs; switch tables are reconciled on takeover."""
import heapq
import threading

GROUP_ID_MIN = 1
GROUP_ID_MAX = 0xFFFFFEFF


class GroupIdAllocator:
    def __init__(self):
        self._lock = threading.Lock()
        self._key_to_id = {}
        self._next_id = GROUP_ID_MIN
        self._free_ids = []

    def allocate(self, key):
        with self._lock:
            if key in self._key_to_id:
                return self._key_to_id[key]
            if self._free_ids:
                gid = heapq.heappop(self._free_ids)
            else:
                if self._next_id > GROUP_ID_MAX:
                    raise RuntimeError("Group ID space exhausted")
                gid = self._next_id
                self._next_id += 1
            self._key_to_id[key] = gid
            return gid

    def release(self, key):
        with self._lock:
            gid = self._key_to_id.pop(key, None)
            if gid is not None:
                heapq.heappush(self._free_ids, gid)

    def get(self, key):
        with self._lock:
            return self._key_to_id.get(key)
