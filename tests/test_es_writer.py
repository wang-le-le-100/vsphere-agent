from datetime import datetime, timezone
import json
from threading import Event

import pytest

from vsphere_agent.buffer import PendingBuffer
from vsphere_agent.config import ElasticsearchConfig
from vsphere_agent.es_writer import EsWriter, make_client, validate_streams
from vsphere_agent.models import Record


def test_elasticsearch_client_uses_configured_ca(monkeypatch):
    calls = []
    monkeypatch.setattr("vsphere_agent.es_writer.Elasticsearch", lambda *args, **kwargs: calls.append((args, kwargs)))
    config = ElasticsearchConfig(("https://es.invalid:9200",), "user", "secret", ca_file="/etc/es-ca.pem")
    make_client(config)
    assert calls[0][1]["ca_certs"] == "/etc/es-ca.pem"


def record(key):
    return Record("logs-test", key, "event", datetime(2026, 1, 1, tzinfo=timezone.utc), {})


class Error(Exception):
    def __init__(self, status):
        self.status_code = status


class FakeClient:
    def __init__(self, replies, healthy=True):
        self.replies, self.healthy, self.calls = list(replies), healthy, []
        self.indices = self
        self.streams = {"logs-test"}

    def bulk(self, *, body, require_data_stream):
        assert require_data_stream is True
        lines = body.decode().splitlines()
        self.calls.append([json.loads(line)["create"]["_id"] for line in lines[::2]])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"items": [{"create": {"status": status}} for status in reply]}

    def ping(self):
        return self.healthy

    def get_data_stream(self, *, name):
        if name not in self.streams:
            raise Error(404)
        return {"data_streams": [{"name": name}]}


def writer(client, buffer=None, delays=(1, 2), on_drop=None):
    waits = []
    result = EsWriter(client, buffer or PendingBuffer(10, 10000, 10, 10000), 2, delays, wait=lambda seconds: waits.append(seconds) or False, on_drop=on_drop)
    return result, waits


def test_partial_retry_retries_only_retryable_failures():
    client = FakeClient([[201, 429, 400, 409], [201]])
    sender, waits = writer(client)
    results = sender.send([record("a"), record("b"), record("c"), record("d")])
    assert [r.status for r in results] == ["success", "success", "failed", "conflict"]
    assert client.calls == [["a", "b", "c", "d"], ["b"]]
    assert waits == [1]


def test_timeout_clears_pending_and_shares_retry_budget_with_partial_failure():
    client = FakeClient([TimeoutError(), [429, 201], [409]])
    pending = PendingBuffer(10, 10000, 10, 10000)
    pending.offer(record("old"))
    dropped = []
    sender, waits = writer(client, pending, on_drop=lambda records, reason: dropped.append((records, reason)))
    results = sender.send([record("a"), record("b")])
    assert [r.status for r in results] == ["conflict", "success"]
    assert client.calls == [["a", "b"], ["a", "b"], ["a"]]
    assert waits == [1, 2]
    assert [r.document_id for r in dropped[0][0]] == ["old"]
    assert len(pending) == 0


def test_uncertain_timeout_not_misreported_as_failed():
    client = FakeClient([TimeoutError(), [429], [400]])
    sender, _ = writer(client)
    assert sender.send([record("a")])[0].status == "unknown"


def test_failure_discards_new_records_until_probe_and_never_replays():
    client = FakeClient([Error(503), TimeoutError(), TimeoutError(), [201]], healthy=False)
    sender, _ = writer(client)
    assert sender.send([record("first")])[0].status == "unknown"
    assert sender.faulted
    assert not sender.offer(record("discarded")).accepted
    assert not sender.probe()
    client.healthy = True
    assert sender.probe()
    assert sender.offer(record("fresh")).accepted
    assert [r.status for r in sender.send(sender.buffer.take_batch())] == ["success"]
    assert client.calls[-1] == ["fresh"]


def test_shutdown_interrupts_retries():
    client = FakeClient([[429]])
    sender = EsWriter(client, PendingBuffer(2, 10000, 2, 10000), 2, (1, 2), wait=lambda seconds: True)
    assert sender.send([record("a")])[0].status == "failed"
    assert len(client.calls) == 1


def test_missing_data_stream_is_checked_before_each_bulk_request():
    client = FakeClient([[201]])
    client.streams.clear()
    sender, _ = writer(client)
    assert sender.send([record("a")])[0].status == "failed"
    assert client.calls == []


def test_stream_existence_check_does_not_create_missing_streams():
    client = FakeClient([])
    validate_streams(client, ["logs-test", "logs-test"])
    with pytest.raises(RuntimeError):
        validate_streams(client, ["logs-missing"])
