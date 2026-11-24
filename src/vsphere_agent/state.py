"""有限 SQLite 比较状态；不保存业务文档和发送队列。"""

from pathlib import Path
import sqlite3
from threading import RLock


class StateStore:
    def __init__(self, path, event_max_keys=100000, event_ttl=86400, alarm_max_keys=100000, max_bytes=32 * 1024 * 1024, on_evict=None):
        self.event_max_keys, self.event_ttl = event_max_keys, event_ttl
        self.alarm_max_keys = alarm_max_keys
        self.on_evict = on_evict or (lambda category, count: None)
        self._lock = RLock()
        self.db = sqlite3.connect(str(Path(path)), check_same_thread=False)
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            pages = max_bytes // page_size
            if pages < 1 or self.db.execute("PRAGMA page_count").fetchone()[0] > pages:
                raise sqlite3.DatabaseError("SQLite 主库超过配置上限")
            actual = self.db.execute(f"PRAGMA max_page_count={pages}").fetchone()[0]
            if actual > pages:
                raise sqlite3.DatabaseError("无法设置 SQLite 主库上限")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS event_seen (vcenter TEXT NOT NULL, event_key TEXT NOT NULL, seen REAL NOT NULL, PRIMARY KEY(vcenter,event_key));
                CREATE INDEX IF NOT EXISTS event_age ON event_seen(seen);
                CREATE TABLE IF NOT EXISTS alarm_entity (vcenter TEXT NOT NULL, entity TEXT NOT NULL, seen REAL NOT NULL, PRIMARY KEY(vcenter,entity));
                CREATE INDEX IF NOT EXISTS alarm_age ON alarm_entity(seen);
                CREATE TABLE IF NOT EXISTS alarm_key (vcenter TEXT NOT NULL, entity TEXT NOT NULL, alarm TEXT NOT NULL, PRIMARY KEY(vcenter,entity,alarm), FOREIGN KEY(vcenter,entity) REFERENCES alarm_entity(vcenter,entity) ON DELETE CASCADE);
            """)
        except Exception:
            self.db.close()
            raise

    def _retry_after_eviction(self, operation):
        with self._lock:
            while True:
                try:
                    return operation()
                except sqlite3.OperationalError as exc:
                    if "database or disk is full" not in str(exc).lower():
                        raise
                    with self.db:
                        events = self.db.execute("SELECT COUNT(*) FROM event_seen").fetchone()[0]
                        entities = self.db.execute("SELECT COUNT(*) FROM alarm_entity").fetchone()[0]
                        if not events and not entities:
                            raise
                        event_victims = max(1, events // 4) if events else 0
                        entity_victims = max(1, entities // 4) if entities else 0
                        self.db.execute("DELETE FROM event_seen WHERE rowid IN (SELECT rowid FROM event_seen ORDER BY seen,rowid LIMIT ?)", (event_victims,))
                        self.db.execute("DELETE FROM alarm_entity WHERE rowid IN (SELECT rowid FROM alarm_entity ORDER BY seen,rowid LIMIT ?)", (entity_victims,))
                    if event_victims:
                        self.on_evict("event", event_victims)
                    if entity_victims:
                        self.on_evict("alarm_entity", entity_victims)

    def remember_event(self, vcenter, key, now):
        return self._retry_after_eviction(lambda: self._remember_event(vcenter, key, now))

    def _remember_event(self, vcenter, key, now):
        with self._lock, self.db:
            self.db.execute("DELETE FROM event_seen WHERE seen < ?", (now - self.event_ttl,))
            exists = self.db.execute("SELECT 1 FROM event_seen WHERE vcenter=? AND event_key=?", (vcenter, str(key))).fetchone()
            if exists:
                return False
            self.db.execute("INSERT INTO event_seen VALUES(?,?,?)", (vcenter, str(key), now))
            count = self.db.execute("SELECT COUNT(*) FROM event_seen").fetchone()[0]
            excess = max(0, count - self.event_max_keys)
            if excess:
                self.db.execute("DELETE FROM event_seen WHERE rowid IN (SELECT rowid FROM event_seen ORDER BY seen,rowid LIMIT ?)", (excess,))
                self.on_evict("event", excess)
            return True

    def compare_alarms(self, vcenter, entity, keys, now):
        keys = {str(key) for key in keys}
        return self._retry_after_eviction(lambda: self._compare_alarms(vcenter, entity, keys, now))

    def _compare_alarms(self, vcenter, entity, keys, now):
        with self._lock, self.db:
            row = self.db.execute("SELECT 1 FROM alarm_entity WHERE vcenter=? AND entity=?", (vcenter, entity)).fetchone()
            previous = {r[0] for r in self.db.execute("SELECT alarm FROM alarm_key WHERE vcenter=? AND entity=?", (vcenter, entity))} if row else set()
            missing = previous - keys
            self.db.execute("DELETE FROM alarm_entity WHERE vcenter=? AND entity=?", (vcenter, entity))
            if len(keys) > self.alarm_max_keys:
                self.on_evict("alarm", len(keys))
                return missing
            while True:
                key_count = self.db.execute("SELECT COUNT(*) FROM alarm_key").fetchone()[0]
                entity_count = self.db.execute("SELECT COUNT(*) FROM alarm_entity").fetchone()[0]
                if key_count + len(keys) <= self.alarm_max_keys and entity_count + 1 <= self.alarm_max_keys:
                    break
                victim = self.db.execute("SELECT vcenter,entity FROM alarm_entity ORDER BY seen,rowid LIMIT 1").fetchone()
                if victim is None:
                    break
                self.db.execute("DELETE FROM alarm_entity WHERE vcenter=? AND entity=?", victim)
                self.on_evict("alarm_entity", 1)
            self.db.execute("INSERT INTO alarm_entity VALUES(?,?,?)", (vcenter, entity, now))
            self.db.executemany("INSERT INTO alarm_key VALUES(?,?,?)", ((vcenter, entity, key) for key in keys))
            return missing

    def invalidate_entity(self, vcenter, entity):
        with self._lock, self.db:
            self.db.execute("DELETE FROM alarm_entity WHERE vcenter=? AND entity=?", (vcenter, entity))

    def invalidate_scope(self, vcenter):
        with self._lock, self.db:
            self.db.execute("DELETE FROM alarm_entity WHERE vcenter=?", (vcenter,))

    def entities(self, vcenter):
        with self._lock:
            return {row[0] for row in self.db.execute("SELECT entity FROM alarm_entity WHERE vcenter=?", (vcenter,))}

    def close(self):
        with self._lock:
            self.db.close()
