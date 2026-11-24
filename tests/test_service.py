from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from vsphere_agent.alarms import AlarmCollector
from vsphere_agent.models import Record
from vsphere_agent.config import BufferConfig, CollectionConfig, Config, ElasticsearchConfig, StateConfig, VCenterConfig
from vsphere_agent.service import Generation, Service


class FakeES:
    def __init__(self):
        self.indices = self
        self.documents = []
        self.written = Event()
        self.closed = False
    def get_data_stream(self, *, name):
        return {"data_streams": [{"name": name}]}
    def bulk(self, *, body, require_data_stream):
        lines = body.decode().splitlines()
        docs = [json.loads(line) for line in lines[1::2]]
        self.documents.extend(docs)
        if any(d.get("data_type") == "event" for d in docs):
            self.written.set()
        return {"items": [{"create": {"status": 201}} for _ in docs]}
    def ping(self):
        return True
    def close(self):
        self.closed = True


class FakeAdapter:
    vcenter_id = "vc"
    vcenter_name = "example"
    vcenter_version = "7"
    def __init__(self, shared):
        self.shared = shared
    def connect(self):
        return self
    def close(self):
        self.shared["closed"] += 1
    def open_events(self, boundary):
        self.boundary = boundary
    def read_events(self, size):
        if not self.shared["events_read"]:
            self.shared["events_read"] = True
            yield [SimpleNamespace(key=1, createdTime=self.boundary + timedelta(seconds=1), fullFormattedMessage="event")]
    def open_tasks(self, boundary):
        pass
    def active_tasks(self):
        return []
    def read_tasks(self, size):
        return iter(())
    def open_task_monitor(self):
        pass
    def sync_task_watch(self, refs):
        pass
    def wait_task_updates(self, timeout):
        return []
    def enumerate_entities(self):
        return []
    def alarms_for(self, entity):
        return []


def configuration(tmp_path):
    return Config(VCenterConfig("vc.invalid", "user", "secret"), ElasticsearchConfig(("http://es.invalid:9200",), "user", "secret"),
                  {"event": "logs-test", "task": "logs-test", "alarm": "logs-test", "diagnostic": "logs-test"},
                  StateConfig(str(tmp_path / "state.sqlite")), CollectionConfig(0.05, 0.05, 0.05, 500, 1),
                  BufferConfig(100, 100000, 10, 10000, 0.05))


def test_service_starts_independent_collectors_sends_and_stops(tmp_path):
    es = FakeES()
    shared = {"closed": 0, "events_read": False}
    service = Service(configuration(tmp_path), client=es, adapter_factory=lambda _: FakeAdapter(shared), journal=lambda message: None)
    thread = Thread(target=service.run)
    thread.start()
    assert es.written.wait(3)
    service.stop()
    thread.join(3)
    assert not thread.is_alive()
    assert es.closed
    assert shared["closed"] == 4
    assert any(doc["data_type"] == "event" for doc in es.documents)
    assert any(doc.get("kind") == "snapshot_summary" for doc in es.documents)


def test_reconnection_skips_events_during_disconnection(tmp_path):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = [base]
    shared = {"closed": 0, "events_read": False, "created": 0, "failed": False}
    es = FakeES()
    class ReconnectingAdapter(FakeAdapter):
        def __init__(self, state):
            super().__init__(state)
            self.generation = state["created"] // 4
            state["created"] += 1
        def connect(self):
            if self.generation:
                current[0] = base + timedelta(seconds=10)
            return self
        def read_events(self, size):
            if self.generation == 0 and not self.shared["failed"]:
                self.shared["failed"] = True
                raise OSError("simulated disconnect")
            if self.generation == 1 and not self.shared["events_read"]:
                self.shared["events_read"] = True
                yield [SimpleNamespace(key=1, createdTime=base + timedelta(seconds=5)),
                       SimpleNamespace(key=2, createdTime=base + timedelta(seconds=11))]
    service = Service(configuration(tmp_path), client=es, adapter_factory=lambda _: ReconnectingAdapter(shared),
                      journal=lambda message: None, clock=lambda: current[0], reconnect_delay=0.01)
    thread = Thread(target=service.run)
    thread.start()
    assert es.written.wait(3)
    service.stop()
    thread.join(3)
    assert not thread.is_alive()
    assert [doc["event_id"] for doc in es.documents if doc.get("data_type") == "event"] == [2]
    assert shared["created"] >= 8


def test_workers_keep_their_own_generation_and_adapter_after_reconnection(tmp_path):
    shared = {"closed": 0, "events_read": False}
    captured = []
    class CapturingService(Service):
        def _start_worker(self, generation, name, adapter, function):
            captured.append((generation, name, adapter, function))
            if len(captured) == 4:
                generation.fail(OSError("disconnected"))
            elif len(captured) == 8:
                self.stop()
    service = CapturingService(configuration(tmp_path), client=FakeES(), adapter_factory=lambda _: FakeAdapter(shared),
                               journal=lambda message: None, reconnect_delay=0)
    service._session_loop(datetime(2026, 1, 1, tzinfo=timezone.utc))
    calls = []
    service._events = lambda generation, adapter, boundary: calls.append((generation, adapter))
    captured[0][3]()
    assert calls == [(captured[0][0], captured[0][2])]
    service.state.close()


def test_locale_degradation_and_recovery_are_reported_without_stopping_collection(tmp_path):
    shared = {"closed": 0, "events_read": False, "created": 0}
    messages = []
    class LocaleAdapter(FakeAdapter):
        def __init__(self, state):
            super().__init__(state)
            generation, role = divmod(state["created"], 4)
            state["created"] += 1
            self.locale_issue = "locale_unavailable" if generation < 2 and role == 0 else None
    class CapturingService(Service):
        def _start_worker(self, generation, name, adapter, function):
            self.started = getattr(self, "started", 0) + 1
            if self.started in (4, 8):
                generation.fail(OSError("disconnected"))
            elif self.started == 12:
                self.stop()
    service = CapturingService(configuration(tmp_path), client=FakeES(),
                               adapter_factory=lambda _: LocaleAdapter(shared),
                               journal=messages.append, reconnect_delay=0)
    try:
        service._session_loop(datetime(2026, 1, 1, tzinfo=timezone.utc))
        records = service.buffer.take_batch()
        locale_records = [record for record in records if record.fields.get("kind", "").startswith("locale_")]
        assert [record.fields["kind"] for record in locale_records] == ["locale_unavailable", "locale_recovered"]
        assert all(record.data_type == "diagnostic" for record in locale_records)
        assert service.started == 12
        assert any("locale 故障" in message for message in messages)
        assert any("locale 已恢复" in message for message in messages)
    finally:
        service.state.close()


def test_es_connection_failure_at_start_enters_drop_mode_and_continues(tmp_path):
    class OfflineES(FakeES):
        def get_data_stream(self, *, name):
            raise ConnectionError("offline")
    observed = []
    class CheckingService(Service):
        def _session_loop(self, boundary):
            observed.append(self.writer.faulted)
            self.stop()
    es = OfflineES()
    service = CheckingService(configuration(tmp_path), client=es, journal=lambda message: None)
    service.run()
    assert observed == [True]
    assert es.closed


def test_missing_stream_at_start_fails_before_collecting(tmp_path):
    class MissingError(Exception):
        status_code = 404
    class MissingES(FakeES):
        def get_data_stream(self, *, name):
            raise MissingError()
    class CheckingService(Service):
        def _session_loop(self, boundary):
            pytest.fail("缺少预建 Data Stream 时不得启动采集")
    es = MissingES()
    service = CheckingService(configuration(tmp_path), client=es, journal=lambda message: None)
    with pytest.raises(RuntimeError):
        service.run()
    assert es.closed


def test_stop_during_connection_does_not_start_more_sessions_or_collectors(tmp_path):
    shared = {"closed": 0, "events_read": False, "connections": 0}
    class StoppingAdapter(FakeAdapter):
        def connect(self):
            shared["connections"] += 1
            service.stop()
            return self
    service = Service(configuration(tmp_path), client=FakeES(), adapter_factory=lambda _: StoppingAdapter(shared),
                      journal=lambda message: None)
    service.run()
    assert shared["connections"] == 1
    assert shared["closed"] == 1


def test_es_failure_drops_snapshot_then_recovery_sends_only_new_snapshot(tmp_path):
    healthy, lost_summary, recovered, summary_written = Event(), Event(), Event(), Event()
    class FlakyES(FakeES):
        def __init__(self):
            super().__init__()
            self.attempts = []
        def bulk(self, *, body, require_data_stream):
            lines = body.decode().splitlines()
            self.attempts.extend(json.loads(line)["create"]["_id"] for line in lines[::2])
            if not healthy.is_set():
                raise TimeoutError("simulated timeout")
            result = super().bulk(body=body, require_data_stream=require_data_stream)
            if any(doc.get("kind") == "snapshot_summary" for doc in self.documents):
                summary_written.set()
            return result
        def ping(self):
            return healthy.is_set()
    class AlarmAdapter(FakeAdapter):
        def enumerate_entities(self):
            return [SimpleNamespace(_moId="entity")]
        def alarms_for(self, entity):
            return [SimpleNamespace(key="alarm", overallStatus="red")]
    config = configuration(tmp_path)
    config = replace(config, elasticsearch=replace(config.elasticsearch, max_retries=0, probe_interval=0.05))
    es = FlakyES()
    service = Service(config, client=es, journal=lambda message: None)
    adapter = AlarmAdapter({"closed": 0, "events_read": False})
    collector = AlarmCollector(adapter, service.state, "logs-test", "logs-test", service._offer)
    service._alarm = collector
    original_drop, original_recover = service.writer.on_drop, service.writer.on_recover
    def on_drop(records, reason):
        original_drop(records, reason)
        if any(r.fields.get("kind") == "snapshot_summary" for r in records):
            lost_summary.set()
    def on_recover():
        original_recover()
        recovered.set()
    service.writer.on_drop, service.writer.on_recover = on_drop, on_recover
    sender = Thread(target=service._send_loop)
    sender.start()
    try:
        first = collector.collect()
        assert lost_summary.wait(3)
        assert first.unknown_records == 1 and first.confirmed_records == 0
        assert service.writer.faulted
        service._offer(Record("logs-test", "discarded-offline", "event", datetime.now(timezone.utc), {}))
        assert len(service.buffer) == 0
        healthy.set()
        service.wake_sender.set()
        assert recovered.wait(3)
        second = collector.collect()
        assert summary_written.wait(3)
        assert second.snapshot_id != first.snapshot_id
        alarms = [doc for doc in es.documents if doc.get("data_type") == "alarm"]
        assert [doc["snapshot_id"] for doc in alarms] == [second.snapshot_id]
        summaries = [doc for doc in es.documents if doc.get("kind") == "snapshot_summary"]
        assert len(summaries) == 1 and summaries[0]["snapshot_status"] == "complete"
        assert "discarded-offline" not in es.attempts
        assert len(es.attempts) == 3
    finally:
        service.stop()
        sender.join(3)
        service.state.close()
        es.close()
    assert not sender.is_alive()


def test_stop_during_stream_preflight_prevents_further_requests(tmp_path):
    class StoppingES(FakeES):
        def __init__(self):
            super().__init__()
            self.checks = []
            self.bulk_calls = 0
        def get_data_stream(self, *, name):
            self.checks.append(name)
            service.stop()
            return {"data_streams": [{"name": name}]}
        def bulk(self, *, body, require_data_stream):
            self.bulk_calls += 1
            return super().bulk(body=body, require_data_stream=require_data_stream)
    es = StoppingES()
    service = Service(configuration(tmp_path), client=es, journal=lambda message: None)
    now = datetime.now(timezone.utc)
    results = service.writer.send([Record("logs-a", "a", "event", now, {}),
                                   Record("logs-b", "b", "event", now, {})])
    assert es.checks == ["logs-a"]
    assert es.bulk_calls == 0
    assert [item.status for item in results] == ["failed", "failed"]
    service.state.close()
    es.close()


def test_shutdown_waits_for_inflight_collector_to_release_its_session(tmp_path):
    entered, release, finished = Event(), Event(), Event()
    shared = {"closed": 0, "events_read": False}
    class SlowAdapter(FakeAdapter):
        def read_events(self, size):
            entered.set()
            release.wait(5)
            return iter(())
    config = configuration(tmp_path)
    config = replace(config, vcenter=replace(config.vcenter, request_timeout=3),
                     elasticsearch=replace(config.elasticsearch, request_timeout=3))
    service = Service(config, client=FakeES(), adapter_factory=lambda _: SlowAdapter(shared), journal=lambda message: None)
    def run():
        try:
            service.run()
        finally:
            finished.set()
    thread = Thread(target=run)
    thread.start()
    try:
        assert entered.wait(3)
        service.stop()
        assert not finished.wait(1.4)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert shared["closed"] == 4


def test_collector_faults_report_recovery_after_successful_work(tmp_path):
    messages = []
    service = Service(configuration(tmp_path), client=FakeES(), journal=messages.append)
    generation = Generation(service.stop_event)
    generation.wait = lambda seconds: True
    adapter = FakeAdapter({"closed": 0, "events_read": True})
    service.diagnostics.fault("event", "test failure")
    service._events(generation, adapter, datetime.now(timezone.utc))
    assert any("event 已恢复" in message for message in messages)
    service.state.close()


def test_task_recovery_requires_discovery_and_monitor_success(tmp_path):
    messages = []
    service = Service(configuration(tmp_path), client=FakeES(), journal=messages.append)
    generation = Generation(service.stop_event)
    generation.wait = lambda seconds: True
    adapter = FakeAdapter({"closed": 0, "events_read": True})
    tracker = SimpleNamespace(active_refs=lambda: {})
    service.diagnostics.fault("task", "test failure")
    service._discover_tasks(generation, adapter, tracker)
    assert not any("task 已恢复" in message for message in messages)
    service._monitor_tasks(generation, adapter, tracker)
    assert any("task 已恢复" in message for message in messages)
    service.state.close()


def test_alarm_recovery_requires_successful_snapshot(tmp_path):
    messages = []
    service = Service(configuration(tmp_path), client=FakeES(), journal=messages.append)
    generation = Generation(service.stop_event)
    generation.wait = lambda seconds: True
    collector = SimpleNamespace(collect=lambda: SimpleNamespace(failed_entities=0))
    service.diagnostics.fault("alarm", "test failure")
    service._alarms(generation, collector)
    assert any("alarm 已恢复" in message for message in messages)
    service.state.close()


def test_task_monitor_reads_latest_details_for_observed_state(tmp_path):
    from vsphere_agent.tasks import TaskTracker

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    info = SimpleNamespace(key="task-1", queueTime=base + timedelta(seconds=1), state="queued", progress=0,
                           task=SimpleNamespace(_moId="task-1"))
    latest = SimpleNamespace(key="task-1", queueTime=info.queueTime, state="running", progress=48,
                             task=info.task, description=SimpleNamespace(message="正在迁移"))
    records = []
    service = Service(configuration(tmp_path), client=FakeES(), journal=lambda message: None)
    generation = Generation(service.stop_event)
    tracker = TaskTracker(FakeAdapter({}), "logs-test", base, "session", records.append)
    tracker.discover(info)
    class Monitor:
        def sync_task_watch(self, refs):
            assert refs == {"task-1": "task-1"}
        def wait_task_updates(self, timeout):
            return [("task-1", "running")]
        def task_info(self, moid):
            assert moid == "task-1"
            return latest
    original_update = tracker.update
    def update(*args):
        original_update(*args)
        generation.finish()
    tracker.update = update
    service._monitor_tasks(generation, Monitor(), tracker)
    assert len(records) == 2
    assert records[-1].fields["progress"] == 48
    assert records[-1].fields["task_detail"] == "正在迁移"
    service.state.close()


def test_task_info_returned_after_generation_expires_is_ignored(tmp_path):
    from vsphere_agent.tasks import TaskTracker

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    info = SimpleNamespace(key="task-1", queueTime=base + timedelta(seconds=1), state="queued",
                           task=SimpleNamespace(_moId="task-1"))
    records = []
    service = Service(configuration(tmp_path), client=FakeES(), journal=lambda message: None)
    generation = Generation(service.stop_event)
    tracker = TaskTracker(FakeAdapter({}), "logs-test", base, "session", records.append)
    tracker.discover(info)
    class Monitor:
        def sync_task_watch(self, refs):
            pass
        def wait_task_updates(self, timeout):
            return [("task-1", "running")]
        def task_info(self, moid):
            generation.finish()
            return SimpleNamespace(key="task-1", state="running", progress=48)
    service._monitor_tasks(generation, Monitor(), tracker)
    assert [r.fields["state"] for r in records] == ["queued"]
    service.state.close()
