from datetime import datetime, timezone

from vsphere_agent.buffer import PendingBuffer
from vsphere_agent.models import Record
from vsphere_agent.serialize import ndjson_entry


def record(key, payload=""):
    return Record("logs-test", key, "event", datetime(2026, 1, 1, tzinfo=timezone.utc), {"payload": payload})


def test_overflow_keeps_newest_unsent_records():
    buffer = PendingBuffer(max_items=2, max_bytes=4096, max_batch_items=2, max_batch_bytes=4096)
    for document_id in ("1", "2", "3"):
        result = buffer.offer(record(document_id))
    assert result.accepted is True
    assert result.evicted_count == 1
    assert [item.document_id for item in buffer.take_batch()] == ["2", "3"]


def test_oversize_record_never_evicts_existing_items():
    buffer = PendingBuffer(3, 4096, 3, 300)
    buffer.offer(record("1"))
    result = buffer.offer(record("huge", "x" * 1000))
    assert not result.accepted and result.evicted_count == 0
    assert result.drop_reason == "batch_bytes"
    assert [r.document_id for r in buffer.take_batch()] == ["1"]


def test_byte_limits_and_inflight_exclusion():
    one = record("1")
    size = len(ndjson_entry(one))
    buffer = PendingBuffer(10, size * 2, 1, size * 2)
    buffer.offer(one)
    inflight = buffer.take_batch()
    buffer.offer(record("2"))
    buffer.offer(record("3"))
    result = buffer.offer(record("4"))
    assert result.evicted_count == 1
    assert [r.document_id for r in inflight] == ["1"]
    assert [r.document_id for r in buffer.take_batch()] == ["3"]
    assert [r.document_id for r in buffer.clear()] == ["4"]


def test_record_larger_than_queue_is_rejected_without_eviction():
    buffer = PendingBuffer(10, 200, 10, 1000)
    buffer.offer(record("1"))
    result = buffer.offer(record("2", "x" * 500))
    assert not result.accepted and result.drop_reason == "buffer_bytes"
    assert len(buffer) == 1
