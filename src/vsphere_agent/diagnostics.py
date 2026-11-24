"""不依赖 ES 的有界类别故障限频记录。"""

from threading import RLock
import time

from .serialize import redact


class RateLimitedDiagnostics:
    CATEGORIES = frozenset({"vcenter", "event", "task", "alarm", "locale", "es", "buffer", "state", "service"})

    def __init__(self, emit, clock=time.monotonic, secrets=(), interval=300):
        self.emit, self.clock, self.secrets, self.interval = emit, clock, tuple(secrets), interval
        self._active = {}
        self._lock = RLock()

    def fault(self, category, message, count=1):
        category = category if category in self.CATEGORIES else "service"
        now = self.clock()
        safe = redact(message, self.secrets, 512)
        with self._lock:
            if category not in self._active:
                self._active[category] = [now, count, safe, now]
                self.emit(f"{category} 故障：{safe}；次数={count}")
            else:
                self._active[category][1] += count
                self._active[category][2] = safe
                self._active[category][3] = now
                self._summarize(category, now)

    def _summarize(self, category, now):
        state = self._active[category]
        if now - state[0] >= self.interval:
            self.emit(f"{category} 故障持续：{state[2]}；累计次数={state[1]}")
            state[0] = now

    def tick(self):
        with self._lock:
            now = self.clock()
            for category, state in list(self._active.items()):
                if category == "buffer" and now - state[3] >= self.interval:
                    self._active.pop(category)
                    self.emit(f"buffer 近期未再发生丢弃；累计次数={state[1]}")
                else:
                    self._summarize(category, now)

    def recover(self, category):
        with self._lock:
            state = self._active.pop(category, None)
            if state:
                self.emit(f"{category} 已恢复；累计次数={state[1]}")
