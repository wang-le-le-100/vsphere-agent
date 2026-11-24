"""受控 JSON 序列化、标识与敏感信息处理。"""

from datetime import datetime
import hashlib
import json
import math
import re


def local_time(value, zone=None):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须携带时区")
    return value.astimezone(zone).isoformat()


def serializable(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, datetime):
        return local_time(value)
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {k: serializable(v) for k, v in value.items()}
    raise TypeError("字段包含不支持序列化的值")


def document_id(*parts):
    encoded = json.dumps(serializable(list(parts)), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def redact(message, secrets=(), limit=1024):
    text = str(message)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[已隐藏]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+[^\s,;]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:password|passwd|token|cookie)\s*[:=]\s*)[^\s,;]+", r"\1[已隐藏]", text)
    return text[:limit]


def ndjson_entry(record):
    document = serializable(record.fields)
    if any(key in document for key in ("@timestamp", "ingest_time", "data_type")):
        raise ValueError("业务字段不能覆盖标准字段")
    document.update({"@timestamp": local_time(record.timestamp), "ingest_time": local_time(record.ingest_time), "data_type": record.data_type})
    metadata = {"create": {"_index": record.target, "_id": record.document_id}}
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return encode(metadata) + b"\n" + encode(document) + b"\n"
