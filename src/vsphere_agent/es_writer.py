"""Data Stream Bulk create、有限重试及保守故障门控。"""

from dataclasses import dataclass
from threading import Event, RLock

from elasticsearch import Elasticsearch

from .buffer import OfferResult
from .serialize import ndjson_entry


RETRYABLE = {429, 502, 503, 504}
UNAVAILABLE = {502, 503, 504}


@dataclass(frozen=True)
class SendResult:
    record: object
    status: str
    http_status: int | None = None


def make_client(config):
    return Elasticsearch(list(config.hosts), basic_auth=(config.username, config.password), request_timeout=config.request_timeout,
                         max_retries=0, retry_on_timeout=False, retry_on_status=(), sniff_on_start=False,
                         sniff_before_requests=False, sniff_on_node_failure=False, ca_certs=config.ca_file)


class DataStreamCheckError(RuntimeError):
    def __init__(self, status_code=None):
        super().__init__("预建 Data Stream 不存在、不可访问或无法核验")
        self.status_code = status_code


class SendStopped(Exception):
    """停止时不再启动新的预检或写入请求。"""


def validate_streams(client, names, should_stop=lambda: False):
    for name in sorted(set(names)):
        if should_stop():
            raise SendStopped()
        try:
            response = client.indices.get_data_stream(name=name)
            if not any(item.get("name") == name for item in response.get("data_streams", [])):
                raise DataStreamCheckError(404)
        except DataStreamCheckError:
            raise
        except Exception as exc:
            raise DataStreamCheckError(getattr(exc, "status_code", None)) from None


class EsWriter:
    def __init__(self, client, buffer, max_retries=2, retry_delays=(1, 2), wait=None, on_drop=None, on_fault=None, on_recover=None, should_stop=None):
        self.client, self.buffer = client, buffer
        self.max_retries, self.retry_delays = max_retries, retry_delays
        self.wait = wait or Event().wait
        self.should_stop = should_stop or (lambda: False)
        self.on_drop = on_drop or (lambda records, reason: None)
        self.on_fault = on_fault or (lambda reason: None)
        self.on_recover = on_recover or (lambda: None)
        self._faulted = False
        self._lock = RLock()

    @property
    def faulted(self):
        with self._lock:
            return self._faulted

    def offer(self, record):
        with self._lock:
            if self._faulted:
                result = OfferResult(False, drop_reason="write_unavailable")
                self.on_drop([record], result.drop_reason)
                return result
            result = self.buffer.offer(record)
            if result.evicted:
                self.on_drop(result.evicted, "buffer_overflow")
            if not result.accepted:
                self.on_drop([record], result.drop_reason)
            return result

    def mark_unavailable(self, reason):
        with self._lock:
            self._faulted = True
            discarded = self.buffer.clear()
            if discarded:
                self.on_drop(discarded, "write_unavailable")
            self.on_fault(reason)

    def _recovered(self):
        with self._lock:
            if self._faulted:
                self._faulted = False
                self.on_recover()

    def probe(self):
        try:
            available = bool(self.client.ping())
        except Exception:
            available = False
        if available:
            self._recovered()
        return available

    def send(self, batch):
        if not batch:
            return []
        pending = list(range(len(batch)))
        outcomes = [None] * len(batch)
        uncertain = set()
        for attempt in range(self.max_retries + 1):
            if self.should_stop() or (attempt and self.wait(self.retry_delays[attempt - 1])):
                break
            bulk_started = False
            try:
                validate_streams(self.client, (batch[i].target for i in pending), self.should_stop)
                body = b"".join(ndjson_entry(batch[i]) for i in pending)
                if self.should_stop():
                    break
                bulk_started = True
                response = self.client.bulk(body=body, require_data_stream=True)
                items = response.get("items")
                if not isinstance(items, list) or len(items) != len(pending) or any(not isinstance(x, dict) or not isinstance(x.get("create"), dict) or not isinstance(x["create"].get("status"), int) for x in items):
                    raise ValueError("无法解析 Bulk 响应")
                retry = []
                all_success = True
                for index, item in zip(pending, items):
                    status = item["create"]["status"]
                    if 200 <= status < 300:
                        outcomes[index] = SendResult(batch[index], "success", status)
                    elif status == 409:
                        all_success = False
                        outcomes[index] = SendResult(batch[index], "conflict", status)
                    elif status in RETRYABLE:
                        all_success = False
                        retry.append(index)
                    else:
                        all_success = False
                        outcomes[index] = SendResult(batch[index], "unknown" if index in uncertain else "failed", status)
                if all_success:
                    self._recovered()
                pending = retry
            except SendStopped:
                break
            except Exception as exc:
                if self.should_stop():
                    if bulk_started:
                        uncertain.update(pending)
                    break
                status = getattr(exc, "status_code", None)
                if status in UNAVAILABLE or status is None:
                    if bulk_started:
                        uncertain.update(pending)
                    self.mark_unavailable("写入路径暂不可用或结果不确定")
                elif status != 429:
                    for index in pending:
                        outcomes[index] = SendResult(batch[index], "unknown" if index in uncertain else "failed", status)
                    pending = []
            if not pending:
                break
        for index in pending:
            outcomes[index] = SendResult(batch[index], "unknown" if index in uncertain else "failed")
        return outcomes
