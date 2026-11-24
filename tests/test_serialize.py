from datetime import datetime, timedelta, timezone
import json

import pytest

from vsphere_agent.models import Record
from vsphere_agent.serialize import document_id, local_time, ndjson_entry, redact, serializable


def test_stable_event_and_distinct_snapshot_ids():
    assert document_id("vc", 42) == document_id("vc", 42)
    assert document_id("vc", "batch-a", "entity", "alarm") != document_id("vc", "batch-b", "entity", "alarm")
    assert document_id("ab", "c") != document_id("a", "bc")


def test_local_time_preserves_absolute_instant_and_offset():
    utc = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert local_time(utc, timezone(timedelta(hours=8))) == "2026-01-01T08:00:00+08:00"
    assert local_time(utc, timezone.utc) == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        local_time(datetime(2026, 1, 1))


def test_record_produces_bulk_create_and_standard_fields():
    record = Record("logs-example", "doc-1", "event", datetime(2026, 1, 1, tzinfo=timezone.utc), {"event_id": 7})
    action, document = ndjson_entry(record).decode("utf-8").splitlines()
    assert json.loads(action) == {"create": {"_index": "logs-example", "_id": "doc-1"}}
    parsed = json.loads(document)
    assert parsed["data_type"] == "event"
    assert parsed["event_id"] == 7
    assert datetime.fromisoformat(parsed["@timestamp"]) == record.timestamp
    assert ndjson_entry(record).endswith(b"\n")


def test_error_redaction_and_nonserializable_values():
    assert "example-secret" not in redact("password=example-secret Authorization: Bearer token-value", ["example-secret"])
    assert "token-value" not in redact("Authorization: Bearer token-value", [])
    assert len(redact("x" * 5000, [], limit=100)) <= 100
    with pytest.raises(TypeError):
        serializable(object())
