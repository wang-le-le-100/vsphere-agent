"""加载并验证采集配置；错误信息不包含配置值。"""

from dataclasses import dataclass, field
from pathlib import Path
import math
import re
from urllib.parse import urlsplit

import yaml


class ConfigurationError(ValueError):
    """配置不可用。"""


@dataclass(frozen=True)
class VCenterConfig:
    host: str
    username: str
    password: str = field(repr=False)
    port: int = 443
    request_timeout: float = 120
    verify_ssl: bool = True
    ca_file: str | None = None


@dataclass(frozen=True)
class ElasticsearchConfig:
    hosts: tuple[str, ...]
    username: str
    password: str = field(repr=False)
    request_timeout: float = 10
    max_retries: int = 2
    retry_delays: tuple[float, ...] = (1, 2)
    probe_interval: float = 30
    ca_file: str | None = None


@dataclass(frozen=True)
class CollectionConfig:
    events_interval: float = 30
    tasks_interval: float = 30
    alarms_interval: float = 300
    page_size: int = 500
    task_wait_timeout: int = 10


@dataclass(frozen=True)
class BufferConfig:
    max_items: int = 5000
    max_bytes: int = 10 * 1024 * 1024
    max_batch_items: int = 500
    max_batch_bytes: int = 5 * 1024 * 1024
    flush_interval: float = 5


@dataclass(frozen=True)
class StateConfig:
    path: str
    event_max_keys: int = 100000
    event_ttl: float = 86400
    alarm_max_keys: int = 100000
    max_bytes: int = 32 * 1024 * 1024


@dataclass(frozen=True)
class Config:
    vcenter: VCenterConfig
    elasticsearch: ElasticsearchConfig
    streams: dict[str, str]
    state: StateConfig
    collection: CollectionConfig
    buffer: BufferConfig


def _mapping(value, name):
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ConfigurationError(f"{name} 必须是映射")
    return value


def _known(data, name, keys):
    if set(data) - set(keys):
        raise ConfigurationError(f"{name} 包含不支持的配置项")


def _text(data, key, name):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ConfigurationError(f"{name}.{key} 必须是非空字符串")
    return value


def _number(data, key, default, name, *, integer=False, zero=False):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name}.{key} 必须是有效数字")
    if not math.isfinite(value) or (integer and not isinstance(value, int)) or value < 0 or (not zero and value == 0):
        raise ConfigurationError(f"{name}.{key} 超出允许范围")
    return value


def _url(value):
    try:
        parts = urlsplit(value)
        return (parts.scheme in ("http", "https") and bool(parts.hostname) and parts.username is None
                and parts.password is None and parts.path in ("", "/") and not parts.query
                and not parts.fragment and (parts.port is None or 1 <= parts.port <= 65535))
    except (ValueError, TypeError):
        return False


def _stream(value):
    return (isinstance(value, str) and len(value.encode("utf-8")) <= 255
            and bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value))
            and value not in (".", ".."))


def load_config(path):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            root = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ConfigurationError("无法读取或解析配置文件") from None
    root = _mapping(root, "配置")
    _known(root, "配置", ("vcenter", "elasticsearch", "streams", "state", "collection", "buffer"))
    vc = _mapping(root.get("vcenter"), "vcenter")
    es = _mapping(root.get("elasticsearch"), "elasticsearch")
    routes = _mapping(root.get("streams"), "streams")
    state = _mapping(root.get("state"), "state")
    collection = _mapping(root.get("collection", {}), "collection")
    buffer = _mapping(root.get("buffer", {}), "buffer")
    _known(vc, "vcenter", ("host", "port", "username", "password", "request_timeout", "verify_ssl", "ca_file"))
    _known(es, "elasticsearch", ("hosts", "username", "password", "request_timeout", "max_retries", "retry_delays", "probe_interval", "ca_file"))
    _known(routes, "streams", ("event", "task", "alarm", "diagnostic"))
    _known(state, "state", ("path", "event_max_keys", "event_ttl", "alarm_max_keys", "max_bytes"))
    _known(collection, "collection", ("events_interval", "tasks_interval", "alarms_interval", "page_size", "task_wait_timeout"))
    _known(buffer, "buffer", ("max_items", "max_bytes", "max_batch_items", "max_batch_bytes", "flush_interval"))
    host = _text(vc, "host", "vcenter")
    if any(char in host for char in "/@?# ") or "://" in host:
        raise ConfigurationError("vcenter.host 必须是主机名或 IP 地址")
    port = _number(vc, "port", 443, "vcenter", integer=True)
    if port > 65535:
        raise ConfigurationError("vcenter.port 超出允许范围")
    verify_ssl = vc.get("verify_ssl", True)
    if not isinstance(verify_ssl, bool):
        raise ConfigurationError("vcenter.verify_ssl 必须是布尔值")
    vc_ca = _text(vc, "ca_file", "vcenter") if "ca_file" in vc else None
    if not verify_ssl and vc_ca:
        raise ConfigurationError("关闭 vCenter 证书校验时不能指定 ca_file")
    vcenter = VCenterConfig(host, _text(vc, "username", "vcenter"), _text(vc, "password", "vcenter"), port,
                            _number(vc, "request_timeout", 120, "vcenter"), verify_ssl, vc_ca)
    hosts = es.get("hosts")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(url, str) and _url(url) for url in hosts):
        raise ConfigurationError("elasticsearch.hosts 必须是有效的 HTTP 或 HTTPS 节点地址列表")
    retries = _number(es, "max_retries", 2, "elasticsearch", integer=True, zero=True)
    delays = es.get("retry_delays", [1, 2])
    if not isinstance(delays, list) or len(delays) < retries or any(isinstance(d, bool) or not isinstance(d, (int, float)) or not math.isfinite(d) or d <= 0 for d in delays):
        raise ConfigurationError("elasticsearch.retry_delays 无效或不足以覆盖重试次数")
    es_ca = _text(es, "ca_file", "elasticsearch") if "ca_file" in es else None
    elasticsearch = ElasticsearchConfig(tuple(hosts), _text(es, "username", "elasticsearch"), _text(es, "password", "elasticsearch"),
                                        _number(es, "request_timeout", 10, "elasticsearch"), retries, tuple(delays),
                                        _number(es, "probe_interval", 30, "elasticsearch"), es_ca)
    if set(routes) != {"event", "task", "alarm", "diagnostic"} or not all(_stream(v) for v in routes.values()):
        raise ConfigurationError("streams 必须为四种数据类型提供有效的 Data Stream 名称")
    state_config = StateConfig(_text(state, "path", "state"), _number(state, "event_max_keys", 100000, "state", integer=True), _number(state, "event_ttl", 86400, "state"), _number(state, "alarm_max_keys", 100000, "state", integer=True), _number(state, "max_bytes", 32 * 1024 * 1024, "state", integer=True))
    collection_config = CollectionConfig(_number(collection, "events_interval", 30, "collection"), _number(collection, "tasks_interval", 30, "collection"), _number(collection, "alarms_interval", 300, "collection"), _number(collection, "page_size", 500, "collection", integer=True), _number(collection, "task_wait_timeout", 10, "collection", integer=True))
    buffer_config = BufferConfig(_number(buffer, "max_items", 5000, "buffer", integer=True), _number(buffer, "max_bytes", 10 * 1024 * 1024, "buffer", integer=True), _number(buffer, "max_batch_items", 500, "buffer", integer=True), _number(buffer, "max_batch_bytes", 5 * 1024 * 1024, "buffer", integer=True), _number(buffer, "flush_interval", 5, "buffer"))
    return Config(vcenter, elasticsearch, dict(routes), state_config, collection_config, buffer_config)
