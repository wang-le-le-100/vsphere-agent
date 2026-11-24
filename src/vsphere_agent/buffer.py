"""按完整 Bulk NDJSON 字节数计量的线程安全 FIFO。"""

from collections import deque
from dataclasses import dataclass
from threading import Lock

from .serialize import ndjson_entry


@dataclass(frozen=True)
class OfferResult:
    accepted: bool
    evicted_count: int = 0
    drop_reason: str | None = None
    evicted: tuple = ()


class PendingBuffer:
    def __init__(self, max_items, max_bytes, max_batch_items, max_batch_bytes):
        if min(max_items, max_bytes, max_batch_items, max_batch_bytes) <= 0:
            raise ValueError("缓冲及批次上限必须为正数")
        self.max_items, self.max_bytes = max_items, max_bytes
        self.max_batch_items, self.max_batch_bytes = max_batch_items, max_batch_bytes
        self._queue = deque()
        self._bytes = 0
        self._lock = Lock()

    def __len__(self):
        with self._lock:
            return len(self._queue)

    @property
    def byte_count(self):
        with self._lock:
            return self._bytes

    def offer(self, record):
        size = len(ndjson_entry(record))
        if size > self.max_batch_bytes:
            return OfferResult(False, drop_reason="batch_bytes")
        if size > self.max_bytes:
            return OfferResult(False, drop_reason="buffer_bytes")
        evicted = []
        with self._lock:
            while self._queue and (len(self._queue) + 1 > self.max_items or self._bytes + size > self.max_bytes):
                old, old_size = self._queue.popleft()
                self._bytes -= old_size
                evicted.append(old)
            self._queue.append((record, size))
            self._bytes += size
        return OfferResult(True, len(evicted), evicted=tuple(evicted))

    def take_batch(self):
        result = []
        size = 0
        with self._lock:
            while self._queue and len(result) < self.max_batch_items:
                record, record_size = self._queue[0]
                if size + record_size > self.max_batch_bytes:
                    break
                self._queue.popleft()
                self._bytes -= record_size
                size += record_size
                result.append(record)
        return result

    def clear(self):
        with self._lock:
            records = [record for record, _ in self._queue]
            self._queue.clear()
            self._bytes = 0
            return records
