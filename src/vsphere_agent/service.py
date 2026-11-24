"""独立采集工作单元、会话重建与有限发送的服务编排。"""

from datetime import datetime, timezone
import logging
import sqlite3
from threading import Event, RLock, Thread
import time
from uuid import uuid4

from .alarms import AlarmCollector
from .buffer import PendingBuffer
from .diagnostics import RateLimitedDiagnostics
from .es_writer import DataStreamCheckError, EsWriter, SendStopped, UNAVAILABLE, make_client, validate_streams
from .events import EventCollector, common_fields
from .models import Record
from .serialize import document_id
from .state import StateStore
from .tasks import TaskTracker
from .vcenter import VCenterAdapter


class StaleGeneration(Exception):
    """结果属于已失效的 vCenter 会话。"""


class Generation:
    def __init__(self, stop):
        self.stop = stop
        self.done = Event()
        self.lock = RLock()
        self.error = None
        self.task_ready = set()

    def active(self):
        return not self.done.is_set() and not self.stop.is_set()

    def fail(self, error):
        with self.lock:
            if self.active():
                self.error = error
                self.done.set()

    def finish(self):
        with self.lock:
            self.done.set()

    def wait(self, seconds):
        return self.done.wait(seconds) or self.stop.is_set()

    def task_work_succeeded(self, worker):
        with self.lock:
            if not self.active():
                return False
            self.task_ready.add(worker)
            return self.task_ready == {"discovery", "monitor"}


class GenerationState:
    def __init__(self, state, generation):
        self.state, self.generation = state, generation

    def __getattr__(self, name):
        method = getattr(self.state, name)
        def guarded(*args, **kwargs):
            with self.generation.lock:
                if not self.generation.active():
                    raise StaleGeneration()
                return method(*args, **kwargs)
        return guarded


class Service:
    def __init__(self, config, client=None, adapter_factory=VCenterAdapter, journal=None, clock=None, reconnect_delay=10):
        self.config = config
        self.client = client if client is not None else make_client(config.elasticsearch)
        self.adapter_factory = adapter_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reconnect_delay = reconnect_delay
        self.stop_event = Event()
        self.wake_sender = Event()
        self._generation = None
        self._alarm = None
        self._threads = []
        self._lock = RLock()
        self._fatal = None
        self._locale_degraded = False
        self.session_id = str(uuid4())
        self._diagnostic_sequence = 0
        secrets = (config.vcenter.password, config.elasticsearch.password)
        self.diagnostics = RateLimitedDiagnostics(journal or logging.getLogger("vsphere_agent").warning, secrets=secrets)
        b = config.buffer
        self.buffer = PendingBuffer(b.max_items, b.max_bytes, b.max_batch_items, b.max_batch_bytes)
        s = config.state
        self.state = StateStore(s.path, s.event_max_keys, s.event_ttl, s.alarm_max_keys, s.max_bytes,
                                on_evict=lambda category, count: self.diagnostics.fault("state", "有限状态已淘汰，相关比较可能未知", count))
        es = config.elasticsearch
        self.writer = EsWriter(self.client, self.buffer, es.max_retries, es.retry_delays, wait=self.stop_event.wait,
                               on_drop=self._on_drop, on_fault=lambda reason: self.diagnostics.fault("es", reason),
                               on_recover=lambda: self.diagnostics.recover("es"), should_stop=self.stop_event.is_set)

    def _on_drop(self, records, reason):
        self.diagnostics.fault("buffer", f"未发送记录已丢弃：{reason}", len(records))
        for record in records:
            self._ack(record, "failed")

    def _ack(self, record, status):
        with self._lock:
            alarm = self._alarm
        if alarm is not None:
            alarm.ack(record, status)

    def _offer(self, record, generation=None):
        if generation is not None:
            with generation.lock:
                if not generation.active():
                    self._ack(record, "failed")
                    return
                result = self.writer.offer(record)
        elif not self.stop_event.is_set():
            result = self.writer.offer(record)
        else:
            self._ack(record, "failed")
            return
        if result.accepted and (len(self.buffer) >= self.buffer.max_batch_items or self.buffer.byte_count >= self.buffer.max_batch_bytes):
            self.wake_sender.set()

    def _diagnostic(self, kind, description, adapter=None, **extra):
        with self._lock:
            self._diagnostic_sequence += 1
            sequence = self._diagnostic_sequence
        fields = common_fields(adapter) if adapter is not None else {}
        fields.update(kind=kind, description=description, **extra)
        self._offer(Record(self.config.streams["diagnostic"], document_id(self.session_id, sequence), "diagnostic", self.clock(), fields))

    def _send_loop(self):
        next_probe = time.monotonic()
        while not self.stop_event.is_set():
            self.wake_sender.wait(self.config.buffer.flush_interval)
            self.wake_sender.clear()
            self.diagnostics.tick()
            if self.stop_event.is_set():
                break
            if self.writer.faulted:
                now = time.monotonic()
                if now >= next_probe:
                    self.writer.probe()
                    next_probe = now + self.config.elasticsearch.probe_interval
                continue
            while not self.stop_event.is_set():
                batch = self.buffer.take_batch()
                if not batch:
                    break
                results = self.writer.send(batch)
                for result in results:
                    self._ack(result.record, result.status)
                failures = [result for result in results if result.status != "success"]
                if failures:
                    self.diagnostics.fault("es", "Bulk 含失败、冲突或结果不确定的条目", len(failures))
                    if not self.writer.faulted and any(result.record.data_type != "diagnostic" for result in failures):
                        self._diagnostic("bulk_failure", "Bulk 有未确认条目；冲突不代表此前写入成功", failed_count=len(failures),
                                         unknown_count=sum(r.status == "unknown" for r in failures),
                                         conflict_count=sum(r.status == "conflict" for r in failures))
                elif not self.writer.faulted:
                    self.diagnostics.recover("es")

    def _worker(self, generation, name, adapter, function):
        try:
            function()
        except StaleGeneration:
            pass
        except Exception as exc:
            if generation.active():
                if isinstance(exc, sqlite3.DatabaseError):
                    self._fatal = "SQLite 状态不可用"
                    self.diagnostics.fault("state", self._fatal)
                else:
                    self.diagnostics.fault(name, f"采集工作单元异常：{type(exc).__name__}")
                    self._diagnostic("collector_failure", f"{name} 采集失败：{type(exc).__name__}", adapter)
                generation.fail(exc)
        finally:
            adapter.close()

    def _start_worker(self, generation, name, adapter, function):
        thread = Thread(target=self._worker, args=(generation, name, adapter, function), daemon=True, name=f"vsphere-{name}")
        self._threads.append(thread)
        thread.start()

    def _events(self, generation, adapter, boundary):
        collector = EventCollector(adapter, GenerationState(self.state, generation), self.config.streams["event"], boundary,
                                   lambda record: self._offer(record, generation), self.config.collection.page_size,
                                   (self.config.vcenter.password, self.config.elasticsearch.password), generation.active)
        while generation.active():
            collector.collect()
            if generation.active():
                self.diagnostics.recover("event")
            if generation.wait(self.config.collection.events_interval):
                break

    def _discover_tasks(self, generation, adapter, tracker):
        while generation.active():
            for page in adapter.read_tasks(self.config.collection.page_size):
                for info in page:
                    if not generation.active():
                        return
                    tracker.discover(info)
            if generation.task_work_succeeded("discovery"):
                self.diagnostics.recover("task")
            if generation.wait(self.config.collection.tasks_interval):
                break

    def _monitor_tasks(self, generation, adapter, tracker):
        while generation.active():
            refs = tracker.active_refs()
            adapter.sync_task_watch(refs)
            if not refs:
                if generation.task_work_succeeded("monitor"):
                    self.diagnostics.recover("task")
                if generation.wait(min(self.config.collection.task_wait_timeout, 1)):
                    break
                continue
            for moid, status in adapter.wait_task_updates(self.config.collection.task_wait_timeout):
                if not generation.active():
                    return
                info = adapter.task_info(moid)
                if not generation.active():
                    return
                tracker.update(moid, status, info)
            if generation.task_work_succeeded("monitor"):
                self.diagnostics.recover("task")

    def _alarms(self, generation, collector):
        snapshot = None
        def collect():
            try:
                batch = collector.collect()
                if batch is not None and not batch.failed_entities and generation.active():
                    self.diagnostics.recover("alarm")
            except StaleGeneration:
                pass
            except Exception as exc:
                if generation.active():
                    self.diagnostics.fault("alarm", f"告警快照失败：{type(exc).__name__}")
                    if isinstance(exc, sqlite3.DatabaseError):
                        self._fatal = "SQLite 状态不可用"
                    else:
                        self._diagnostic("alarm_snapshot_failed", f"告警快照失败：{type(exc).__name__}")
                    generation.fail(exc)
        try:
            while generation.active():
                if snapshot is not None and snapshot.is_alive():
                    collector.collect()
                else:
                    snapshot = Thread(target=collect, daemon=True, name="vsphere-alarm-snapshot")
                    snapshot.start()
                if generation.wait(self.config.collection.alarms_interval):
                    break
        finally:
            if snapshot is not None:
                snapshot.join()

    def _session_loop(self, initial_boundary):
        recovering = False
        while not self.stop_event.is_set() and self._fatal is None:
            adapters = []
            started = False
            generation = Generation(self.stop_event)
            try:
                for _ in range(4):
                    if not generation.active():
                        return
                    adapter = self.adapter_factory(self.config.vcenter)
                    adapters.append(adapter)
                    adapter.connect()
                if not generation.active():
                    return
                if len({adapter.vcenter_id for adapter in adapters}) != 1:
                    raise RuntimeError("会话连接到不同的 vCenter")
                boundary = self.clock() if recovering else initial_boundary
                events, tasks, monitor, alarms = adapters
                if any(getattr(adapter, "locale_issue", None) for adapter in adapters):
                    message = "部分会话无法确认使用简体中文消息，采集内容以接口返回为准"
                    self.diagnostics.fault("locale", message)
                    if not self._locale_degraded:
                        self._diagnostic("locale_unavailable", message, events)
                    self._locale_degraded = True
                elif self._locale_degraded:
                    self.diagnostics.recover("locale")
                    self._diagnostic("locale_recovered", "采集会话已确认使用简体中文消息", events)
                    self._locale_degraded = False
                if recovering:
                    self.state.invalidate_scope(events.vcenter_id)
                events.open_events(boundary)
                if not generation.active():
                    return
                tasks.open_tasks(boundary)
                if not generation.active():
                    return
                tracker = TaskTracker(tasks, self.config.streams["task"], boundary, str(uuid4()),
                                      lambda record, gen=generation: self._offer(record, gen), self.clock,
                                      (self.config.vcenter.password, self.config.elasticsearch.password))
                tracker.baseline(tasks.active_tasks())
                if not generation.active():
                    return
                monitor.open_task_monitor()
                if not generation.active():
                    return
                collector = AlarmCollector(alarms, GenerationState(self.state, generation), self.config.streams["alarm"],
                                           self.config.streams["diagnostic"], lambda record, gen=generation: self._offer(record, gen),
                                           self.clock, (self.config.vcenter.password, self.config.elasticsearch.password), generation.active)
                with self._lock:
                    self._generation, self._alarm = generation, collector
                self.diagnostics.recover("vcenter")
                self._start_worker(generation, "event", events, lambda gen=generation, obj=events, start=boundary: self._events(gen, obj, start))
                self._start_worker(generation, "task", tasks, lambda gen=generation, obj=tasks, tracking=tracker: self._discover_tasks(gen, obj, tracking))
                self._start_worker(generation, "task", monitor, lambda gen=generation, obj=monitor, tracking=tracker: self._monitor_tasks(gen, obj, tracking))
                self._start_worker(generation, "alarm", alarms, lambda gen=generation, current=collector: self._alarms(gen, current))
                started = True
                while generation.active():
                    generation.wait(0.25)
                collector.invalidate()
                if generation.error is not None:
                    recovering = True
                    self.diagnostics.fault("vcenter", "会话失效，重连后建立新采集边界")
            except Exception as exc:
                recovering = True
                if isinstance(exc, sqlite3.DatabaseError):
                    self._fatal = "SQLite 状态不可用"
                    self.diagnostics.fault("state", self._fatal)
                else:
                    self.diagnostics.fault("vcenter", f"连接或建立采集会话失败：{type(exc).__name__}")
                generation.finish()
            finally:
                generation.finish()
                if not started:
                    for adapter in adapters:
                        adapter.close()
                self._threads = [thread for thread in self._threads if thread.is_alive()]
            if not self.stop_event.is_set() and self._fatal is None and recovering:
                self.stop_event.wait(self.reconnect_delay)

    def stop(self):
        self.stop_event.set()
        with self._lock:
            generation = self._generation
        if generation is not None:
            generation.finish()
        self.wake_sender.set()

    def run(self):
        initial_boundary = self.clock()
        sender = None
        try:
            try:
                validate_streams(self.client, self.config.streams.values(), self.stop_event.is_set)
            except SendStopped:
                return
            except DataStreamCheckError as exc:
                if exc.status_code is not None and exc.status_code not in UNAVAILABLE:
                    raise
                self.writer.mark_unavailable("启动时无法核验 Data Stream，写入路径暂不可用")
            if self.stop_event.is_set():
                return
            sender = Thread(target=self._send_loop, daemon=True, name="vsphere-es")
            sender.start()
            self._session_loop(initial_boundary)
            if self._fatal:
                raise RuntimeError(self._fatal)
        finally:
            self.stop()
            deadline = time.monotonic() + max(self.config.vcenter.request_timeout, self.config.elasticsearch.request_timeout) + 1
            threads = self._threads + ([sender] if sender is not None else [])
            for thread in threads:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            if any(thread.is_alive() for thread in threads):
                self.diagnostics.fault("service", "停止等待超时，部分在途工作可能未完成资源释放")
            dropped = self.buffer.clear()
            if dropped:
                self._on_drop(dropped, "shutdown")
            self.state.close()
            self.client.close()
