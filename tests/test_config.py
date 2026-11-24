import copy

import pytest
import yaml

from vsphere_agent.config import ConfigurationError, load_config


@pytest.fixture
def settings():
    return {
        "vcenter": {"host": "vcenter.invalid", "username": "example-user", "password": "vcenter-test-secret"},
        "elasticsearch": {"hosts": ["http://es.invalid:9200"], "username": "example-user", "password": "es-test-secret"},
        "streams": {"event": "logs-example.events-test", "task": "logs-example.tasks-test", "alarm": "logs-example.alarms-test", "diagnostic": "logs-example.agent-test"},
        "state": {"path": "agent-state.sqlite"},
    }


def read_config(tmp_path, settings):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    return load_config(path)


def test_default_limits_and_intervals(tmp_path, settings):
    config = read_config(tmp_path, settings)
    assert (config.collection.events_interval, config.collection.tasks_interval, config.collection.alarms_interval) == (30, 30, 300)
    assert (config.elasticsearch.request_timeout, config.elasticsearch.max_retries, config.elasticsearch.retry_delays) == (10, 2, (1, 2))
    assert (config.buffer.max_items, config.buffer.max_bytes) == (5000, 10 * 1024 * 1024)
    assert (config.buffer.max_batch_items, config.buffer.max_batch_bytes, config.buffer.flush_interval) == (500, 5 * 1024 * 1024, 5)
    assert (config.state.event_max_keys, config.state.event_ttl, config.state.alarm_max_keys, config.state.max_bytes) == (100000, 86400, 100000, 32 * 1024 * 1024)
    assert config.vcenter.port == 443
    assert config.vcenter.verify_ssl is True
    assert config.vcenter.ca_file is None
    assert config.elasticsearch.ca_file is None
    assert "vcenter-test-secret" not in repr(config)
    assert "es-test-secret" not in repr(config)


def test_tls_settings_accept_https_and_explicit_ca_paths(tmp_path, settings):
    settings["vcenter"].update(verify_ssl=True, ca_file="/etc/ssl/vcenter.pem")
    settings["elasticsearch"].update(hosts=["https://es.invalid:9200"], ca_file="/etc/ssl/es.pem")
    config = read_config(tmp_path, settings)
    assert config.vcenter.verify_ssl is True
    assert config.vcenter.ca_file == "/etc/ssl/vcenter.pem"
    assert config.elasticsearch.hosts == ("https://es.invalid:9200",)
    assert config.elasticsearch.ca_file == "/etc/ssl/es.pem"


def test_unverified_vcenter_with_ca_file_is_rejected(tmp_path, settings):
    settings["vcenter"].update(verify_ssl=False, ca_file="/etc/ssl/vcenter.pem")
    with pytest.raises(ConfigurationError):
        read_config(tmp_path, settings)


def test_same_existing_stream_can_be_used_for_all_types(tmp_path, settings):
    settings["streams"] = dict.fromkeys(settings["streams"], "logs-example.shared-test")
    assert len(set(read_config(tmp_path, settings).streams.values())) == 1


@pytest.mark.parametrize("section,key,value", [
    ("vcenter", "host", ""),
    ("vcenter", "username", ""),
    ("vcenter", "password", ""),
    ("vcenter", "port", 0),
    ("elasticsearch", "hosts", []),
    ("elasticsearch", "hosts", ["ftp://es.invalid:9200"]),
    ("vcenter", "verify_ssl", "false"),
    ("vcenter", "ca_file", ""),
    ("elasticsearch", "ca_file", 10),
    ("elasticsearch", "hosts", ["http://user:secret@es.invalid:9200"]),
    ("elasticsearch", "hosts", ["http://es.invalid/path"]),
    ("elasticsearch", "username", ""),
    ("elasticsearch", "password", ""),
    ("elasticsearch", "request_timeout", 0),
    ("elasticsearch", "max_retries", -1),
    ("elasticsearch", "retry_delays", [1]),
    ("streams", "event", "INVALID STREAM"),
    ("streams", "task", ""),
    ("state", "path", ""),
    ("state", "event_max_keys", 0),
    ("collection", "events_interval", -1),
    ("collection", "tasks_interval", 0),
    ("collection", "alarms_interval", False),
    ("buffer", "max_items", 0),
    ("buffer", "max_batch_bytes", -2),
])
def test_invalid_configuration_is_rejected_without_leaking_passwords(tmp_path, settings, section, key, value):
    settings = copy.deepcopy(settings)
    settings.setdefault(section, {})[key] = value
    with pytest.raises(ConfigurationError) as exc:
        read_config(tmp_path, settings)
    assert "vcenter-test-secret" not in str(exc.value)
    assert "es-test-secret" not in str(exc.value)
    assert "user:secret" not in str(exc.value)


def test_missing_required_section_and_malformed_yaml_are_safe(tmp_path, settings):
    del settings["streams"]
    with pytest.raises(ConfigurationError):
        read_config(tmp_path, settings)
    path = tmp_path / "bad.yaml"
    path.write_text("password: vcenter-test-secret\nbroken: [", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_config(path)
    assert "vcenter-test-secret" not in str(exc.value)
