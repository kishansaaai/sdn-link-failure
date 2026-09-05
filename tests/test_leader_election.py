"""Exercise real Lua leases, ownership fencing, failure and recovery."""
from unittest.mock import Mock
import fakeredis
import pytest
from redis.exceptions import ConnectionError
from ha.backup import LeaderElection
from ha.state_sync import StateSync, HEARTBEAT_KEY


@pytest.fixture
def client():
    return fakeredis.FakeRedis(decode_responses=True)


def test_only_one_owner_and_generation_increases(client):
    a, b = StateSync(client), StateSync(client)
    assert a.acquire("a") == 1
    assert b.acquire("b") == 0
    assert not b.renew("b")
    b.release("b")
    assert a.read_heartbeat() == "a"
    a.release("a")
    assert b.acquire("b") == 2


def test_state_is_fenced(client):
    sync = StateSync(client)
    sync.acquire("a")
    assert sync.push_state("a", {"graph": {}, "intents": [["a", "b"]]})
    assert not sync.push_state("b", {"graph": {"corrupt": True}})
    assert sync.pull_state()["intents"] == [["a", "b"]]
    assert client.ttl(HEARTBEAT_KEY) > 0


def test_promote_renew_demote_and_takeover(client):
    promoted, demoted = Mock(), Mock()
    a = LeaderElection(StateSync(client), "a", promoted, demoted)
    b = LeaderElection(StateSync(client), "b")
    a.tick()
    b.tick()
    assert a.is_primary and not b.is_primary
    promoted.assert_called_once_with(None)
    a.tick()
    promoted.assert_called_once()
    client.delete(HEARTBEAT_KEY)  # simulate TTL expiration
    b.tick()
    a.tick()
    assert b.is_primary and not a.is_primary
    demoted.assert_called_once()
    assert b.generation > a.generation


def test_redis_failure_is_not_permission_to_promote():
    sync = Mock()
    sync.acquire.side_effect = ConnectionError("offline")
    e = LeaderElection(sync)
    e.tick()
    assert not e.is_primary
    assert "offline" in e.last_error


def test_failed_renewal_withdraws_existing_master():
    sync, demote = Mock(), Mock()
    sync.acquire.return_value = 1
    sync.pull_state.return_value = None
    e = LeaderElection(sync, on_demote=demote)
    e.tick()
    sync.renew.side_effect = ConnectionError("offline")
    e.tick()
    assert not e.is_primary
    demote.assert_called_once()


def test_bad_snapshot_never_promotes(client):
    client.set("sdn:topology_state", "{bad")
    e = LeaderElection(StateSync(client))
    e.tick()
    assert not e.is_primary


def test_expired_local_lease_stops_writes_without_tick(client):
    e = LeaderElection(StateSync(client))
    e.tick()
    e.lease_deadline = 0
    assert not e.is_primary
