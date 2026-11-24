import sqlite3

import pytest

from vsphere_agent.state import StateStore


def make_store(tmp_path, **kwargs):
    return StateStore(tmp_path / "state.sqlite", event_max_keys=kwargs.get("event_max_keys", 3), event_ttl=10, alarm_max_keys=kwargs.get("alarm_max_keys", 3), max_bytes=1024 * 1024)


def test_event_dedup_expiry_capacity_and_restart(tmp_path):
    store = make_store(tmp_path, event_max_keys=2)
    assert store.remember_event("vc", "1", 100)
    assert not store.remember_event("vc", "1", 101)
    assert store.remember_event("vc", "2", 102)
    assert store.remember_event("vc", "3", 103)
    assert store.remember_event("vc", "1", 104)
    store.close()
    store = make_store(tmp_path, event_max_keys=2)
    assert not store.remember_event("vc", "1", 105)
    assert store.remember_event("vc", "1", 120)
    store.close()


def test_alarm_missing_once_then_return_then_missing_again(tmp_path):
    store = make_store(tmp_path)
    assert store.compare_alarms("vc", "entity", {"a"}, 1) == set()
    assert store.compare_alarms("vc", "entity", set(), 2) == {"a"}
    assert store.compare_alarms("vc", "entity", set(), 3) == set()
    assert store.compare_alarms("vc", "entity", {"a"}, 4) == set()
    assert store.compare_alarms("vc", "entity", set(), 5) == {"a"}
    store.close()


def test_alarm_unknown_after_invalidated_query_or_eviction(tmp_path):
    store = make_store(tmp_path, alarm_max_keys=1)
    store.compare_alarms("vc", "entity-1", {"a"}, 1)
    store.invalidate_entity("vc", "entity-1")
    assert store.compare_alarms("vc", "entity-1", set(), 2) == set()
    store.compare_alarms("vc", "entity-1", {"a"}, 3)
    store.compare_alarms("vc", "entity-2", {"b"}, 4)
    assert store.compare_alarms("vc", "entity-1", set(), 5) == set()
    store.close()


def test_corrupt_database_is_not_silently_treated_as_empty(tmp_path):
    (tmp_path / "state.sqlite").write_bytes(b"not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        make_store(tmp_path)


def test_database_size_limit_evicts_old_state_and_keeps_writing(tmp_path):
    path = tmp_path / "limited.sqlite"
    evicted = []
    store = StateStore(path, event_max_keys=10000, event_ttl=100000, alarm_max_keys=10000, max_bytes=65536,
                       on_evict=lambda kind, count: evicted.append((kind, count)))
    for number in range(500):
        assert store.remember_event("vc", f"{number}-" + "x" * 500, number)
    store.close()
    assert path.stat().st_size <= 65536
    assert evicted


def test_database_limit_evicts_alarm_baseline_without_fabricating_missing(tmp_path):
    path = tmp_path / "limited-alarms.sqlite"
    store = StateStore(path, event_max_keys=10000, event_ttl=100000, alarm_max_keys=10000, max_bytes=65536)
    for number in range(150):
        store.compare_alarms("vc", f"entity-{number}", {f"alarm-{number}-" + "x" * 500}, number)
    assert store.compare_alarms("vc", "entity-0", set(), 200) == set()
    store.close()
    assert path.stat().st_size <= 65536
