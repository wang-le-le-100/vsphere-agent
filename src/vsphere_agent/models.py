"""采集记录的数据模型。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class Record:
    target: str
    document_id: str
    data_type: str
    timestamp: datetime
    fields: dict
    ingest_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
