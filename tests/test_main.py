import logging

import yaml

from elasticsearch import Elasticsearch

from vsphere_agent import __main__ as entry


def settings():
    return {"vcenter": {"host": "vc.invalid", "username": "user", "password": "test-secret"},
            "elasticsearch": {"hosts": ["http://es.invalid:9200"], "username": "user", "password": "test-secret"},
            "streams": dict.fromkeys(["event", "task", "alarm", "diagnostic"], "logs-test"),
            "state": {"path": "test.sqlite"}}


def test_invalid_config_exits_without_secret(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("password: test-secret\nbroken: [", encoding="utf-8")
    assert entry.main(["--config", str(path)]) == 2
    assert "test-secret" not in capsys.readouterr().err


def test_config_check_does_not_connect(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings()), encoding="utf-8")
    monkeypatch.setattr(entry, "Service", lambda config: (_ for _ in ()).throw(AssertionError("unexpected connection")))
    assert entry.main(["--config", str(path), "--check-config"]) == 0


def test_main_runs_and_installs_signal_handlers(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings()), encoding="utf-8")
    calls = []
    class FakeService:
        def __init__(self, config):
            calls.append("init")
        def run(self):
            calls.append("run")
        def stop(self):
            calls.append("stop")
    monkeypatch.setattr(entry, "Service", FakeService)
    monkeypatch.setattr(entry.signal, "signal", lambda sig, handler: calls.append(sig))
    assert entry.main(["--config", str(path)]) == 0
    assert calls.count("run") == 1
    assert entry.signal.SIGTERM in calls


def test_transport_node_warnings_do_not_bypass_service_diagnostics(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings()), encoding="utf-8")
    transport_logger = logging.getLogger("elastic_transport")
    monkeypatch.setattr(transport_logger, "propagate", transport_logger.propagate)

    class FakeService:
        def __init__(self, config):
            self.client = Elasticsearch("http://es.invalid:9200")
        def run(self):
            pool = self.client.transport.node_pool
            node = pool.all()[0]
            for _ in range(5):
                pool.mark_dead(node)
                pool.mark_live(node)
            logging.getLogger("vsphere_agent").warning("service diagnostic")
            self.client.close()
        def stop(self):
            pass

    monkeypatch.setattr(entry, "Service", FakeService)
    monkeypatch.setattr(entry.signal, "signal", lambda *args: None)
    with caplog.at_level(logging.WARNING):
        assert entry.main(["--config", str(path)]) == 0
    assert [r.message for r in caplog.records if r.name == "vsphere_agent"] == ["service diagnostic"]
    assert not [r for r in caplog.records if r.name.startswith("elastic_transport")]


def test_sigterm_handler_stops_running_service(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings()), encoding="utf-8")
    handlers, calls = {}, []
    class FakeService:
        def __init__(self, config):
            pass
        def run(self):
            handlers[entry.signal.SIGTERM](entry.signal.SIGTERM, None)
        def stop(self):
            calls.append("stop")
    monkeypatch.setattr(entry, "Service", FakeService)
    monkeypatch.setattr(entry.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    assert entry.main(["--config", str(path)]) == 0
    assert calls == ["stop"]
