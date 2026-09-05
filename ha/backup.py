"""Lease election advanced by the controller's cooperative event loop."""
from __future__ import annotations
import logging
import time
import uuid
from ha.state_sync import HEARTBEAT_TTL, StateSync

log = logging.getLogger(__name__)


class LeaderElection:
    WATCHING = "WATCHING"
    PRIMARY = "PRIMARY"

    def __init__(self, sync=None, instance_id=None, on_promote=None, on_demote=None):
        self.sync = sync if sync is not None else StateSync()
        self.instance_id = instance_id or str(uuid.uuid4())
        self.on_promote = on_promote
        self.on_demote = on_demote
        self.state = self.WATCHING
        self.generation = 0
        self.lease_deadline = 0.0
        self.last_error = None

    @property
    def is_primary(self):
        return self.state == self.PRIMARY and time.monotonic() < self.lease_deadline

    def tick(self):
        started = time.monotonic()
        try:
            if self.state == self.PRIMARY:
                if not self.sync.renew(self.instance_id):
                    self.demote()
                    return
                self.lease_deadline = started + HEARTBEAT_TTL - 1
            else:
                generation = self.sync.acquire(self.instance_id)
                if not generation:
                    return
                self.generation = generation
                self.lease_deadline = started + HEARTBEAT_TTL - 1
                snapshot = self.sync.pull_state()
                self.state = self.PRIMARY
                if self.on_promote:
                    self.on_promote(snapshot)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("HA unavailable; withdrawing controller writes: %s", exc)
            self.demote()

    def demote(self):
        was_primary = self.state == self.PRIMARY
        self.state = self.WATCHING
        self.lease_deadline = 0.0
        if was_primary and self.on_demote:
            self.on_demote()

    def stop(self):
        self.demote()
        try:
            self.sync.release(self.instance_id)
        except Exception:
            log.warning("Lease release failed; it will expire")
