"""告警追加快照及可比较范围内的客观缺失记录。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from pyVmomi import vim, vmodl

from .events import common_fields
from .models import Record
from .serialize import document_id, redact


ALARM_STATE_LABELS = {"red": "严重", "yellow": "警告", "green": "正常", "gray": "未知"}
MAX_DEFINITION_CACHE = 4096


def optional_property(obj, name):
    try:
        return getattr(obj, name, None), False
    except vim.fault.NotAuthenticated:
        raise
    except vmodl.MethodFault:
        return None, True


@dataclass
class Snapshot:
    snapshot_id: str
    scope_count: int = 0
    processed_entities: int = 0
    failed_entities: int = 0
    expected_records: int = 0
    expected_alarms: int = 0
    confirmed_records: int = 0
    failed_records: int = 0
    unknown_records: int = 0
    pending: set = field(default_factory=set)
    collection_done: bool = False
    invalidated: bool = False


class AlarmCollector:
    def __init__(self, adapter, state, alarm_target, diagnostic_target, emit, clock=None, secrets=(), active=lambda: True):
        self.adapter, self.state = adapter, state
        self.alarm_target, self.diagnostic_target, self.emit = alarm_target, diagnostic_target, emit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.secrets, self.active = secrets, active
        self._lock = RLock()
        self._current = None

    def _diagnostic(self, kind, description, **fields):
        timestamp = self.clock()
        data = common_fields(self.adapter)
        data.update(kind=kind, description=redact(description, self.secrets, 1024), **fields)
        return Record(self.diagnostic_target, document_id(str(uuid4())), "diagnostic", timestamp, data)

    def _submit(self, batch, record):
        with self._lock:
            if batch.invalidated:
                return
            batch.pending.add(record.document_id)
            batch.expected_records += 1
            if record.data_type == "alarm":
                batch.expected_alarms += 1
        self.emit(record)

    def _summary_locked(self, batch):
        if not batch.collection_done or batch.pending or batch.invalidated or self._current is not batch:
            return None
        status = "unknown" if batch.unknown_records else "partial" if batch.failed_records or batch.failed_entities else "complete"
        self._current = None
        return self._diagnostic("snapshot_summary", "告警快照采集及写入结果", snapshot_id=batch.snapshot_id,
                                scope_count=batch.scope_count, processed_entities=batch.processed_entities,
                                failed_entities=batch.failed_entities, expected_records=batch.expected_records,
                                expected_alarms=batch.expected_alarms, confirmed_records=batch.confirmed_records,
                                failed_records=batch.failed_records, unknown_records=batch.unknown_records,
                                snapshot_status=status)

    def ack(self, record, status):
        summary = None
        with self._lock:
            batch = self._current
            if batch is None or record.fields.get("snapshot_id") != batch.snapshot_id or record.document_id not in batch.pending:
                return
            batch.pending.remove(record.document_id)
            if status == "success":
                batch.confirmed_records += 1
            elif status in ("unknown", "conflict"):
                batch.unknown_records += 1
            else:
                batch.failed_records += 1
            summary = self._summary_locked(batch)
        if summary is not None:
            self.emit(summary)

    def invalidate(self):
        with self._lock:
            if self._current:
                self._current.invalidated = True
                self._current = None

    def collect(self):
        with self._lock:
            if self._current is not None:
                skipped = True
            else:
                skipped = False
                batch = Snapshot(str(uuid4()))
                self._current = batch
        if skipped:
            self.emit(self._diagnostic("snapshot_skipped", "上一轮告警快照尚未结束，本次周期跳过"))
            return None
        try:
            entities = self.adapter.enumerate_entities()
            current_ids = {obj._moId for obj in entities}
            batch.scope_count = len(current_ids)
            previous_ids = self.state.entities(self.adapter.vcenter_id)
            if previous_ids and previous_ids != current_ids:
                self.state.invalidate_scope(self.adapter.vcenter_id)
                self._submit(batch, self._diagnostic("alarm_scope_changed", "对象可见范围发生变化，比较状态已置为未知", snapshot_id=batch.snapshot_id))
            missing_candidates = []
            definition_cache = {}
            comparable = True
            for entity in entities:
                if not self.active() or batch.invalidated:
                    self.invalidate()
                    return batch
                entity_id = entity._moId
                try:
                    alarms = self.adapter.alarms_for(entity)
                except (OSError, ConnectionError, TimeoutError, vim.fault.NotAuthenticated):
                    self.state.invalidate_scope(self.adapter.vcenter_id)
                    raise
                except Exception as exc:
                    batch.failed_entities += 1
                    self.state.invalidate_scope(self.adapter.vcenter_id)
                    comparable = False
                    missing_candidates.clear()
                    self._submit(batch, self._diagnostic("alarm_query_failed", f"对象告警查询失败，范围比较状态未知：{type(exc).__name__}", snapshot_id=batch.snapshot_id, entity_moref=entity_id))
                    continue
                if not self.active() or batch.invalidated:
                    self.invalidate()
                    return batch
                keys = {str(item.key) for item in alarms}
                if len(keys) != len(alarms):
                    batch.failed_entities += 1
                    self.state.invalidate_scope(self.adapter.vcenter_id)
                    comparable = False
                    missing_candidates.clear()
                    self._submit(batch, self._diagnostic("alarm_query_failed", "对象告警查询返回重复标识，比较状态未知", snapshot_id=batch.snapshot_id, entity_moref=entity_id))
                    continue
                batch.processed_entities += 1
                missing = self.state.compare_alarms(self.adapter.vcenter_id, entity_id, keys, self.clock().timestamp())
                entity_name, entity_name_failed = optional_property(entity, "name") if alarms else (None, False)
                for item in alarms:
                    timestamp = self.clock()
                    fields = common_fields(self.adapter)
                    state = str(item.overallStatus)
                    fields.update(snapshot_id=batch.snapshot_id, entity_moref=entity_id, entity_type=entity.__class__.__name__,
                                  alarm_key=str(item.key), state=state, state_label=ALARM_STATE_LABELS.get(state, "未识别"),
                                  raw_type=item.__class__.__name__)
                    if entity_name:
                        fields["entity_name"] = redact(entity_name, self.secrets, 4096)
                    for source, target in (("time", "alarm_time"), ("acknowledged", "acknowledged"),
                                           ("acknowledgedByUser", "acknowledged_by_user"),
                                           ("acknowledgedTime", "acknowledged_time"), ("eventKey", "alarm_event_key")):
                        value = getattr(item, source, None)
                        if value is not None:
                            fields[target] = redact(value, self.secrets) if isinstance(value, str) else value
                    alarm_obj = getattr(item, "alarm", None)
                    definition_failed = False
                    if alarm_obj is not None:
                        alarm_id = alarm_obj._moId
                        fields["alarm_moref"] = alarm_id
                        if alarm_id not in definition_cache:
                            info, failed = optional_property(alarm_obj, "info")
                            if len(definition_cache) < MAX_DEFINITION_CACHE:
                                definition_cache[alarm_id] = info, failed
                        else:
                            info, failed = definition_cache[alarm_id]
                        definition_failed = failed
                        if info is not None:
                            for source, target in (("name", "alarm_name"), ("description", "alarm_description")):
                                value = getattr(info, source, None)
                                if value:
                                    fields[target] = redact(value, self.secrets, 4096)
                    if entity_name_failed or definition_failed:
                        fields["metadata_status"] = "unavailable"
                    fields["message"] = redact(
                        f"对象：{fields.get('entity_name', entity_id)}；告警：{fields.get('alarm_name', fields.get('alarm_moref', str(item.key)))}；状态：{fields['state_label']}",
                        self.secrets, 4096)
                    if not self.active() or batch.invalidated:
                        self.invalidate()
                        return batch
                    record = Record(self.alarm_target, document_id(self.adapter.vcenter_id, batch.snapshot_id, entity_id, str(item.key)), "alarm", timestamp, fields)
                    self._submit(batch, record)
                if comparable:
                    missing_candidates.extend((entity_id, key) for key in sorted(missing))
            if not self.active() or batch.invalidated:
                self.invalidate()
                return batch
            for entity_id, key in missing_candidates:
                self._submit(batch, self._diagnostic("alarm_not_returned", "此前出现的告警本次未返回，原因未知", snapshot_id=batch.snapshot_id, entity_moref=entity_id, alarm_key=key))
        except Exception:
            with self._lock:
                batch.failed_entities += 1
            try:
                self.state.invalidate_scope(self.adapter.vcenter_id)
            finally:
                with self._lock:
                    batch.collection_done = True
                    summary = self._summary_locked(batch)
                if summary is not None:
                    self.emit(summary)
            raise
        with self._lock:
            batch.collection_done = True
            summary = self._summary_locked(batch)
        if summary is not None:
            self.emit(summary)
        return batch
