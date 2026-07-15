"""
test_leader_election.py — Unit tests for the HA leader election state machine.

Runs without Redis by using a mock StateSync.
"""
import time
import pytest
from unittest.mock import MagicMock, patch

from ha.backup import LeaderElection, POLL_INTERVAL


class MockStateSync:
    def __init__(self, primary_alive: bool = True):
        self._alive = primary_alive
        self.heartbeats: list = []

    def is_primary_alive(self) -> bool:
        return self._alive

    def pull_state(self):
        return {"graph": {"adj": {}, "hosts": {}}, "recovery_log": [], "ts": time.time()}

    def write_heartbeat(self, instance_id: str) -> None:
        self.heartbeats.append(instance_id)


def make_election(primary_alive: bool = True, on_promote=None):
    election = LeaderElection(on_promote=on_promote, instance_id="test-backup")
    election.sync = MockStateSync(primary_alive=primary_alive)
    return election


# ---------------------------------------------------------------------------
# State machine tests
# ---------------------------------------------------------------------------

def test_initial_state_is_watching():
    e = make_election(primary_alive=True)
    assert e.state == LeaderElection.WATCHING


def test_is_not_primary_while_watching():
    e = make_election(primary_alive=True)
    assert not e.is_primary


def test_promotion_when_heartbeat_lost():
    """Backup should promote when primary heartbeat is gone."""
    promoted = []

    def on_promote(graph, log, elapsed_ms):
        promoted.append(elapsed_ms)

    e = make_election(primary_alive=False, on_promote=on_promote)
    e.detect_ts = time.time()   # simulate detection
    e._promote()

    assert e.state == LeaderElection.PRIMARY
    assert e.is_primary
    assert len(promoted) == 1


def test_failover_time_is_measured():
    e = make_election(primary_alive=False)
    t0 = time.time()
    e.detect_ts = t0
    e._promote()
    ft = e.failover_time_ms()
    assert ft is not None
    assert ft >= 0
    assert ft < 5000   # should complete in < 5s in tests


def test_no_failover_time_before_promotion():
    e = make_election(primary_alive=True)
    assert e.failover_time_ms() is None


def test_state_loaded_from_sync_on_promotion():
    """Graph should be restored from synced state on promotion."""
    loaded_graphs = []

    def on_promote(graph, log, elapsed_ms):
        loaded_graphs.append(graph)

    e = make_election(primary_alive=False, on_promote=on_promote)
    e.detect_ts = time.time()
    e._promote()

    assert len(loaded_graphs) == 1
    # Graph restored (even if empty in mock)
    assert loaded_graphs[0] is not None


def test_backup_writes_heartbeat_after_promotion():
    e = make_election(primary_alive=False)
    e.detect_ts = time.time()
    e._promote()
    assert "test-backup" in e.sync.heartbeats


def test_double_promotion_does_not_regress():
    """Calling _promote twice should not break state."""
    e = make_election(primary_alive=False)
    e.detect_ts = time.time()
    e._promote()
    assert e.state == LeaderElection.PRIMARY
    e._promote()   # second call — should stay PRIMARY
    assert e.state == LeaderElection.PRIMARY
