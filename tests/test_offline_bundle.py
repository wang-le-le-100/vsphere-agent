"""离线包和部署入口只使用虚构连接信息进行测试。"""
import hashlib
import importlib.metadata as metadata
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from unittest.mock import patch

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "ops" / "build_offline.py"
BASH = Path(r"D:\git\Git\bin\bash.exe") if os.name == "nt" else Path("/bin/bash")


def build(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    result = subprocess.run([sys.executable, str(BUILDER), "--output", str(archive)], capture_output=True, text=True)
    return result, archive


def test_bundle_contains_only_example_config_without_sensitive_sources(tmp_path):
    archive = tmp_path / "public-bundle.tar.gz"
    result = subprocess.run([sys.executable, str(BUILDER), "--output", str(archive)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    with tarfile.open(archive, "r:gz") as tar:
        entries = tar.getmembers()
        names = {item.name for item in entries}
        assert "vsphere-agent-offline/config.yaml" not in names
        config = yaml.safe_load(tar.extractfile("vsphere-agent-offline/config.example.yaml"))
        assert config["vcenter"]["password"] == "replace-vcenter-password"
        assert config["elasticsearch"]["password"] == "replace-es-password"
        assert config["state"]["path"] == "/var/lib/vsphere-agent/state.sqlite"
        assert tar.getmember("vsphere-agent-offline").mode == 0o700
        assert "vsphere-agent-offline/install.sh" in names
        assert "vsphere-agent-offline/vsphere-agent.service" in names
        assert "vsphere-agent-offline/SHA256SUMS" in names
        assert "vsphere-agent-offline/vendor/certifi/cacert.pem" in names
        assert "vsphere-agent-offline/vendor/pyVmomi/__init__.py" in names
        assert not any(n.endswith((".pyd", ".dll", ".exe", ".pyc")) for n in names)
        assert not any("demo.py" in n or "settings.md" in n for n in names)
        tar.extractall(tmp_path)  # 仅解压本测试刚生成的归档
    payload = tmp_path / "vsphere-agent-offline"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, (payload / "src", payload / "vendor"))), PYTHONDONTWRITEBYTECODE="1")
    check = subprocess.run([sys.executable, "-S", "-m", "vsphere_agent", "--config", str(payload / "config.example.yaml"), "--check-config"], env=env, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    fallback = subprocess.run([sys.executable, "-S", "-c", "import yaml; assert not yaml.__with_libyaml__; import pyVmomi, elasticsearch, certifi; assert certifi.where()"], env=env, capture_output=True, text=True)
    assert fallback.returncode == 0, fallback.stderr


def test_bundle_contains_project_license(tmp_path):
    result, archive = build(tmp_path)
    assert result.returncode == 0, result.stderr
    with tarfile.open(archive, "r:gz") as tar:
        assert tar.extractfile("vsphere-agent-offline/LICENSE").read() == (ROOT / "LICENSE").read_bytes()


def test_bundle_preserves_distribution_license_and_notice_files(tmp_path):
    result, archive = build(tmp_path)
    assert result.returncode == 0, result.stderr
    spec = importlib.util.spec_from_file_location("offline_builder_licenses", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tarfile.open(archive, "r:gz") as tar:
        for name in builder.DISTRIBUTIONS:
            dist = metadata.distribution(name)
            for item in dist.files:
                parts = item.parts
                if (parts and parts[0].lower().endswith(".dist-info")
                        and (any(part.lower() == "licenses" for part in parts)
                             or parts[-1].lower().startswith(("license", "notice", "copying")))):
                    assert tar.extractfile("vsphere-agent-offline/vendor/" + item.as_posix()).read() == Path(dist.locate_file(item)).read_bytes()


def test_builder_refuses_existing_archive(tmp_path):
    destination = tmp_path / "bundle.tar.gz"
    destination.write_bytes(b"do not overwrite")
    result, _ = build(tmp_path)
    assert result.returncode != 0
    assert destination.read_bytes() == b"do not overwrite"


def test_builder_secures_empty_archive_before_writing(tmp_path):
    spec = importlib.util.spec_from_file_location("offline_builder_permissions", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    archive = tmp_path / "secured.tar.gz"
    observed = []

    def secure(path):
        observed.append(path.read_bytes())

    with patch.object(builder, "secure_output_permissions", secure, create=True):
        builder.create_bundle(archive)
    assert observed == [b""]
    assert archive.stat().st_size > 0


def test_builder_never_writes_credentials_if_permission_setup_fails(tmp_path):
    spec = importlib.util.spec_from_file_location("offline_builder_permissions_failure", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    archive = tmp_path / "secured.tar.gz"
    with patch.object(builder, "secure_output_permissions", side_effect=OSError("permission setup failed"), create=True):
        with pytest.raises(OSError):
            builder.create_bundle(archive)
    assert archive.read_bytes() == b""


def test_installer_syntax_and_unit_use_the_same_offline_runtime():
    script = ROOT / "ops" / "install-offline.sh"
    unit = ROOT / "ops" / "vsphere-agent.service"
    check = subprocess.run([str(BASH), "-n", str(script)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    content = script.read_text(encoding="utf-8")
    service = unit.read_text(encoding="utf-8")
    assert "sha256sum -c SHA256SUMS" in content
    assert "/usr/bin/python3.10 -S" in service
    assert "PYTHONPATH=/opt/vsphere-agent/src:/opt/vsphere-agent/vendor" in service
    assert "systemctl enable --now vsphere-agent.service" in content
    assert "apt-get " not in content and " pip " not in content


def bash_path(path):
    value = Path(path).resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


@pytest.fixture
def simulated_installer(tmp_path):
    """替换系统命令和目标路径，绝不修改真实主机或访问网络。"""
    base = tmp_path / "payload"
    base.mkdir()
    (base / "src").mkdir()
    (base / "vendor").mkdir()
    (base / "src" / "app.py").write_text("# fixture\n", encoding="utf-8")
    (base / "vendor" / "lib.py").write_text("# fixture\n", encoding="utf-8")
    (base / "config.example.yaml").write_text("fictional example\n", encoding="utf-8")
    (tmp_path / "private-config.yaml").write_text("fictional configuration\n", encoding="utf-8")
    (base / "vsphere-agent.service").write_text("fictional unit\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    fake = '''#!/bin/sh
name=${0##*/}
printf '%s' "$name" >> "$TEST_LOG"
printf ' <%s>' "$@" >> "$TEST_LOG"
printf '\\n' >> "$TEST_LOG"
case "$name" in
  id) printf '0\\n' ;;
  getent) exit 2 ;;
  systemctl)
    if [ "$1" = cat ]; then exit 1; fi
    if [ "$1" = enable ] && [ "${FAIL_ENABLE:-0}" = 1 ]; then exit 1; fi ;;
esac
exit 0
'''
    for name in ("id", "getent", "systemctl", "groupadd", "useradd", "install", "cp", "chown", "chmod", "python3.10", "flock"):
        path = fake_bin / name
        path.write_text(fake, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
    content = (ROOT / "ops" / "install-offline.sh").read_text(encoding="utf-8")
    content = content.replace("export PATH=/usr/sbin:/usr/bin:/sbin:/bin", f'export PATH="{bash_path(fake_bin)}:/usr/bin:/bin"')
    content = content.replace("/etc/os-release", bash_path(os_release))
    content = content.replace("/usr/bin/python3.10", bash_path(fake_bin / "python3.10"))
    for prefix in ("/opt/vsphere-agent", "/etc/vsphere-agent", "/var/lib/vsphere-agent", "/etc/systemd/system/vsphere-agent.service", "/usr/lib/systemd/system/vsphere-agent.service", "/lib/systemd/system/vsphere-agent.service", "/run/systemd/system/vsphere-agent.service", "/run/vsphere-agent-deploy.lock"):
        content = content.replace(prefix, bash_path(tmp_path / "system" / prefix.lstrip("/")))
    (tmp_path / "system" / "run").mkdir(parents=True, exist_ok=True)
    script = base / "install.sh"
    script.write_text(content, encoding="utf-8", newline="\n")
    manifest = []
    for file in sorted(p for p in base.rglob("*") if p.is_file()):
        manifest.append(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.relative_to(base).as_posix()}\n")
    (base / "SHA256SUMS").write_text("".join(manifest), encoding="ascii", newline="\n")
    def run(*args, **extra):
        env = dict(os.environ, TEST_LOG=bash_path(log), **extra)
        arguments = list(args) if args else [bash_path(tmp_path / "private-config.yaml")]
        return subprocess.run([str(BASH), bash_path(script), *arguments], env=env, capture_output=True, text=True, encoding="utf-8")
    return tmp_path, base, log, run


def test_installer_simulates_success_with_limited_permissions(simulated_installer):
    _, _, log, run = simulated_installer
    result = run()
    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "groupadd <--system> <vsphere>" in calls
    assert "private-config.yaml" in calls
    assert "load_config(sys.argv[1]).state.path == sys.argv[2]" in calls
    assert "<-m> <0640> <-o> <root> <-g> <vsphere>" in calls
    assert "<-m> <0700> <-o> <vsphere> <-g> <vsphere>" in calls
    assert "systemctl <enable> <--now> <vsphere-agent.service>" in calls


def test_installer_requires_external_config_before_modification(simulated_installer):
    tmp_path, _, log, run = simulated_installer
    result = run(bash_path(tmp_path / "missing.yaml"))
    assert result.returncode != 0
    assert "groupadd" not in log.read_text(encoding="utf-8")


def test_installer_rejects_example_as_private_configuration(simulated_installer):
    _, base, log, run = simulated_installer
    result = run(bash_path(base / "config.example.yaml"))
    assert result.returncode != 0
    assert "groupadd" not in log.read_text(encoding="utf-8")


def test_installer_conflict_stops_before_modification(simulated_installer):
    tmp_path, _, log, run = simulated_installer
    conflict = tmp_path / "system" / "opt" / "vsphere-agent"
    conflict.mkdir(parents=True)
    result = run("")
    assert result.returncode != 0
    assert "已有部署路径" in result.stderr
    assert "groupadd" not in log.read_text(encoding="utf-8")


def test_installer_rejects_unlisted_file_before_modification(simulated_installer):
    _, base, log, run = simulated_installer
    (base / "vendor" / "unexpected.py").write_text("# unexpected\n", encoding="utf-8")
    result = run()
    assert result.returncode != 0
    assert "groupadd" not in log.read_text(encoding="utf-8")


def test_installer_reports_failure_if_systemd_start_fails(simulated_installer):
    _, _, _, run = simulated_installer
    result = run(FAIL_ENABLE="1")
    assert result.returncode != 0
    assert "服务已启动" not in result.stdout
    assert "安装中途失败" in result.stderr


def test_installer_rejects_modified_example_before_modification(simulated_installer):
    _, base, log, run = simulated_installer
    (base / "config.example.yaml").write_text("changed\n", encoding="utf-8")
    result = run()
    assert result.returncode != 0
    assert "groupadd" not in log.read_text(encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("VSPHERE_TEST_BUNDLE"), reason="仅在明确指定产物时校验实际部署包")
def test_actual_bundle_matches_sources_and_manifest_without_printing_secrets():
    spec = importlib.util.spec_from_file_location("offline_builder", BUILDER)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with tarfile.open(os.environ["VSPHERE_TEST_BUNDLE"], "r:gz") as tar:
        members = tar.getmembers()
        if any(not (member.isfile() or member.isdir()) for member in members):
            pytest.fail("归档包含不支持的文件类型", pytrace=False)
        files = {m.name.removeprefix("vsphere-agent-offline/"): tar.extractfile(m).read()
                 for m in members if m.isfile()}
        manifest = files.pop("SHA256SUMS").decode("ascii")
        expected = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items()))
        assert manifest == expected
        assert files["install.sh"] == (ROOT / "ops" / "install-offline.sh").read_bytes().replace(b"\r\n", b"\n")
        assert files["vsphere-agent.service"] == (ROOT / "ops" / "vsphere-agent.service").read_bytes().replace(b"\r\n", b"\n")
        source_files = {"src/" + path.relative_to(ROOT / "src").as_posix(): path.read_bytes()
                        for path in (ROOT / "src" / "vsphere_agent").rglob("*.py")}
        assert {name: content for name, content in files.items() if name.startswith("src/")} == source_files
        assert {name: content for name, content in files.items() if name.startswith("vendor/")} == builder.collect_dependencies()
        assert tar.getmember("vsphere-agent-offline").mode == 0o700
        assert "config.yaml" not in files
        assert files["LICENSE"] == (ROOT / "LICENSE").read_bytes()
        assert files["config.example.yaml"] == (ROOT / "config.example.yaml").read_bytes()


@pytest.fixture
def simulated_upgrade(tmp_path):
    """在临时目录中模拟 systemd 和安装目录，不访问真实主机。"""
    root = tmp_path / "system"
    app = root / "opt" / "vsphere-agent"
    (app / "src").mkdir(parents=True)
    (app / "vendor").mkdir()
    (app / "src" / "old.py").write_text("old source", encoding="utf-8")
    (app / "vendor" / "old.py").write_text("old dependency", encoding="utf-8")
    config = root / "etc" / "vsphere-agent" / "config.yaml"
    database = root / "var" / "lib" / "vsphere-agent" / "state.sqlite"
    unit = root / "etc" / "systemd" / "system" / "vsphere-agent.service"
    for path, content in ((config, b"existing config"), (database, b"existing state"), (unit, b"existing unit")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    base = tmp_path / "payload"
    (base / "src").mkdir(parents=True)
    (base / "vendor").mkdir()
    (base / "src" / "new.py").write_text("new source", encoding="utf-8")
    (base / "vendor" / "new.py").write_text("new dependency", encoding="utf-8")
    (base / "config.example.yaml").write_text("example config", encoding="utf-8")
    (base / "vsphere-agent.service").write_text("package unit must not replace existing unit", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log, state, moves, starts, checks = (tmp_path / name for name in ("calls", "state", "moves", "starts", "checks"))
    state.write_text("active", encoding="utf-8")
    commands = {
        "id": "printf '0\\n'\n",
        "getent": "exit 0\n",
        "chown": "exit 0\n",
        "chmod": "exit 0\n",
        "sleep": "exit 0\n",
        "flock": '[ "${FAIL_LOCK:-0}" != 1 ]\n',
        "findmnt": "printf '/fixture\\n'\n",
        "df": '''printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\n'
if [ "${FAIL_SPACE:-0}" = 1 ]; then printf 'fixture 100 100 0 100%% /fixture\\n'; else printf 'fixture 20000000 1000 19999000 1%% /fixture\\n'; fi
''',
        "python3.10": "exit 0\n",
        "cp": '''if [ "${FAIL_RESTORE:-0}" = 1 ]; then
  case "$*" in *"/backup/src"*|*"/backup/vendor"*) exit 43 ;; esac
fi
exec /usr/bin/cp "$@"
''',
        "runuser": '''printf 'runuser\\n' >> "$TEST_LOG"
n=0; [ ! -f "$TEST_CHECKS" ] || n=$(cat "$TEST_CHECKS")
n=$((n + 1)); printf '%s' "$n" > "$TEST_CHECKS"
[ "${FAIL_CONFIG:-0}" != 1 ] && [ "${FAIL_CONFIG_AT:-0}" != "$n" ]
''',
        "mv": '''n=0
[ ! -f "$TEST_MOVES" ] || n=$(cat "$TEST_MOVES")
n=$((n + 1))
printf '%s' "$n" > "$TEST_MOVES"
case ",${FAIL_MV_CALLS:-}," in *",$n,"*) exit 42 ;; esac
exec /usr/bin/mv "$@"
''',
        "systemctl": '''printf 'systemctl %s\\n' "$*" >> "$TEST_LOG"
case "$1" in
  stop)
    if [ "${FAIL_STOP:-0}" = 1 ]; then exit 1; fi
    if [ "${FAIL_STOP_AFTER_START:-0}" = 1 ] && [ -f "$TEST_STARTS" ]; then exit 1; fi
    printf inactive > "$TEST_STATE" ;;
  start)
    n=0; [ ! -f "$TEST_STARTS" ] || n=$(cat "$TEST_STARTS")
    n=$((n + 1)); printf '%s' "$n" > "$TEST_STARTS"
    if [ "${FAIL_FIRST_START:-0}" = 1 ] && [ "$n" = 1 ]; then
      if [ "${MODIFY_DB_ON_START:-0}" = 1 ]; then printf 'new runtime state' > "$TEST_DB"; fi
      if [ "${SIDECAR_ON_START:-0}" = 1 ]; then printf wal > "$TEST_DB-wal"; fi
      printf failed > "$TEST_STATE"; exit 1
    fi
    printf active > "$TEST_STATE" ;;
  is-active) [ "$(cat "$TEST_STATE")" = active ] ;;
  show)
    property=
    while [ "$#" -gt 0 ]; do
      case "$1" in -p|--property) shift; property="$1" ;; --property=*) property="${1#*=}" ;; esac
      shift
    done
    case "$property" in
      Version) printf '249\\n' ;;
      LoadState) printf 'loaded\\n' ;;
      ActiveState) cat "$TEST_STATE"; printf '\\n' ;;
      SubState) if [ "$(cat "$TEST_STATE")" = active ]; then printf 'running\\n'; else printf 'dead\\n'; fi ;;
      MainPID) if [ "$(cat "$TEST_STATE")" = active ]; then printf '123\\n'; else printf '0\\n'; fi ;;
      FragmentPath) printf '%s\\n' "$TEST_UNIT" ;;
      DropInPaths) if [ "${BAD_UNIT:-0}" = 1 ]; then printf '/unexpected.conf\\n'; else printf '\\n'; fi ;;
      User|Group) printf 'vsphere\\n' ;;
      WorkingDirectory) printf '%s\\n' "$TEST_APP" ;;
      Environment) printf 'PYTHONPATH=%s/src:%s/vendor PYTHONDONTWRITEBYTECODE=1\\n' "$TEST_APP" "$TEST_APP" ;;
      ExecStart) printf '{ path=%s ; argv[]=%s -S -m vsphere_agent --config %s ; }\\n' "$TEST_PYTHON" "$TEST_PYTHON" "$TEST_CONFIG" ;;
      *) exit 1 ;;
    esac ;;
  cat) exit 0 ;;
  *) exit 1 ;;
esac
''',
    }
    for name, body in commands.items():
        file = fake_bin / name
        file.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
        file.chmod(0o755)
    os_release = tmp_path / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="22.04"\n', encoding="utf-8")
    content = (ROOT / "ops" / "install-offline.sh").read_text(encoding="utf-8")
    content = content.replace("export PATH=/usr/sbin:/usr/bin:/sbin:/bin", f'export PATH="{bash_path(fake_bin)}:/usr/bin:/bin"')
    content = content.replace("/etc/os-release", bash_path(os_release))
    content = content.replace("/usr/bin/python3.10", bash_path(fake_bin / "python3.10"))
    for prefix in ("/opt/vsphere-agent", "/etc/vsphere-agent", "/var/lib/vsphere-agent", "/etc/systemd/system/vsphere-agent.service", "/usr/lib/systemd/system/vsphere-agent.service", "/lib/systemd/system/vsphere-agent.service", "/run/systemd/system/vsphere-agent.service", "/run/vsphere-agent-deploy.lock"):
        content = content.replace(prefix, bash_path(root / prefix.lstrip("/")))
    (root / "run").mkdir(parents=True, exist_ok=True)
    script = base / "install.sh"
    script.write_text(content, encoding="utf-8", newline="\n")
    manifest = "".join(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.relative_to(base).as_posix()}\n"
                       for file in sorted(p for p in base.rglob("*") if p.is_file()))
    (base / "SHA256SUMS").write_text(manifest, encoding="ascii", newline="\n")
    env = dict(os.environ, TEST_APP=bash_path(app), TEST_CONFIG=bash_path(config), TEST_UNIT=bash_path(unit),
               TEST_DB=bash_path(database), TEST_PYTHON=bash_path(fake_bin / "python3.10"), TEST_LOG=bash_path(log),
               TEST_STATE=bash_path(state), TEST_MOVES=bash_path(moves), TEST_STARTS=bash_path(starts), TEST_CHECKS=bash_path(checks))

    def run(**extra):
        return subprocess.run([str(BASH), bash_path(script)], env=dict(env, **extra), capture_output=True, text=True, encoding="utf-8")
    return {"app": app, "config": config, "database": database, "unit": unit, "base": base,
            "state": state, "log": log, "run": run, "root": root}


def upgrade_backup(fixture):
    return next(fixture["app"].glob(".upgrade-*/backup"))


def assert_preserved(fixture):
    assert fixture["config"].read_bytes() == b"existing config"
    assert fixture["database"].read_bytes() == b"existing state"
    assert fixture["unit"].read_bytes() == b"existing unit"


def test_upgrade_running_replaces_source_and_dependencies_preserves_everything_else(simulated_upgrade):
    f = simulated_upgrade
    result = f["run"]()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (f["app"] / "src" / "new.py").read_text() == "new source"
    assert (f["app"] / "vendor" / "new.py").read_text() == "new dependency"
    backup = upgrade_backup(f)
    assert (backup / "src" / "old.py").read_text() == "old source"
    assert (backup / "vendor" / "old.py").read_text() == "old dependency"
    assert (backup / "config.yaml").read_bytes() == b"existing config"
    assert (backup / "state.sqlite").read_bytes() == b"existing state"
    assert (backup / "vsphere-agent.service").read_bytes() == b"existing unit"
    assert_preserved(f)
    assert f["state"].read_text() == "active"
    calls = f["log"].read_text()
    assert "systemctl stop vsphere-agent.service" in calls
    assert "systemctl start vsphere-agent.service" in calls
    assert "systemctl enable" not in calls


def test_upgrade_stopped_does_not_start_service(simulated_upgrade):
    f = simulated_upgrade
    f["state"].write_text("inactive")
    result = f["run"]()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (f["app"] / "src" / "new.py").exists()
    assert f["state"].read_text() == "inactive"
    assert "systemctl start" not in f["log"].read_text()
    assert_preserved(f)


@pytest.mark.parametrize("fault", ["tampered", "unlisted", "sidecar", "incomplete", "lock", "bad_config", "unknown_state", "stop_failure", "space", "unit_override"])
def test_upgrade_refuses_unsafe_situations_before_switch(simulated_upgrade, fault):
    f = simulated_upgrade
    extra = {}
    if fault == "tampered":
        (f["base"] / "src" / "new.py").write_text("modified", encoding="utf-8")
    elif fault == "unlisted":
        (f["base"] / "vendor" / "extra.py").write_text("unexpected", encoding="utf-8")
    elif fault == "sidecar":
        f["database"].with_name("state.sqlite-wal").write_bytes(b"wal")
    elif fault == "incomplete":
        f["unit"].unlink()
    elif fault == "lock":
        extra["FAIL_LOCK"] = "1"
    elif fault == "bad_config":
        extra["FAIL_CONFIG"] = "1"
    elif fault == "unknown_state":
        f["state"].write_text("activating")
    elif fault == "stop_failure":
        extra["FAIL_STOP"] = "1"
    elif fault == "space":
        extra["FAIL_SPACE"] = "1"
    elif fault == "unit_override":
        extra["BAD_UNIT"] = "1"
    result = f["run"](**extra)
    assert result.returncode != 0, result.stdout + result.stderr
    expected = {"tampered": "离线包校验失败", "unlisted": "未列入清单", "sidecar": "SQLite 存在附属文件",
                "incomplete": "部署不完整", "lock": "已有部署或升级操作", "bad_config": "现场配置校验失败",
                "unknown_state": "服务处于不明确", "stop_failure": "服务停止失败",
                "space": "可用空间不足", "unit_override": "覆盖配置不符合预期"}
    assert expected[fault] in result.stderr
    assert (f["app"] / "src" / "old.py").read_text() == "old source"
    assert (f["app"] / "vendor" / "old.py").read_text() == "old dependency"
    assert f["config"].read_bytes() == b"existing config"
    assert f["database"].read_bytes() == b"existing state"
    if fault in {"tampered", "unlisted", "incomplete", "lock", "bad_config", "unknown_state", "space", "unit_override"}:
        assert not f["log"].exists() or "systemctl stop" not in f["log"].read_text()


@pytest.mark.parametrize("failure", ["2", "3", "4"])
def test_switch_failure_restores_original_program_and_running_state(simulated_upgrade, failure):
    f = simulated_upgrade
    result = f["run"](FAIL_MV_CALLS=failure)
    assert result.returncode != 0, result.stdout + result.stderr
    assert (f["app"] / "src" / "old.py").read_text() == "old source"
    assert (f["app"] / "vendor" / "old.py").read_text() == "old dependency"
    assert f["state"].read_text() == "active"
    calls = f["log"].read_text()
    assert "systemctl stop vsphere-agent.service" in calls
    assert "systemctl start vsphere-agent.service" in calls
    assert_preserved(f)


def test_start_failure_rolls_back_and_restores_running_state(simulated_upgrade):
    f = simulated_upgrade
    result = f["run"](FAIL_FIRST_START="1")
    assert result.returncode != 0, result.stdout + result.stderr
    assert (f["app"] / "src" / "old.py").read_text() == "old source"
    assert (f["app"] / "vendor" / "old.py").read_text() == "old dependency"
    assert f["state"].read_text() == "active"
    assert_preserved(f)
    assert f["log"].read_text().count("systemctl start vsphere-agent.service") == 2


def test_post_switch_check_failure_restores_and_retains_backup(simulated_upgrade):
    f = simulated_upgrade
    result = f["run"](FAIL_CONFIG_AT="3")
    assert result.returncode != 0
    assert "切换后配置校验失败" in result.stderr
    assert (f["app"] / "src" / "old.py").read_text() == "old source"
    assert (f["app"] / "vendor" / "old.py").read_text() == "old dependency"
    assert (upgrade_backup(f) / "src" / "old.py").read_text() == "old source"
    assert (upgrade_backup(f) / "vendor" / "old.py").read_text() == "old dependency"
    assert f["state"].read_text() == "active"
    assert_preserved(f)


def test_start_failure_does_not_restore_old_sqlite_snapshot(simulated_upgrade):
    f = simulated_upgrade
    result = f["run"](FAIL_FIRST_START="1", MODIFY_DB_ON_START="1")
    assert result.returncode != 0
    assert f["database"].read_bytes() == b"new runtime state"
    assert (upgrade_backup(f) / "state.sqlite").read_bytes() == b"existing state"
    assert (f["app"] / "src" / "old.py").exists()
    assert (f["app"] / "vendor" / "old.py").exists()
    assert f["state"].read_text() == "active"


def test_start_failure_with_sqlite_sidecar_restores_code_but_does_not_restart(simulated_upgrade):
    f = simulated_upgrade
    result = f["run"](FAIL_FIRST_START="1", SIDECAR_ON_START="1")
    assert result.returncode != 0
    assert (f["app"] / "src" / "old.py").exists()
    assert (f["app"] / "vendor" / "old.py").exists()
    assert f["database"].with_name("state.sqlite-wal").read_bytes() == b"wal"
    assert f["state"].read_text() == "inactive"
    assert f["log"].read_text().count("systemctl start vsphere-agent.service") == 1
    assert "SQLite 存在附属文件" in result.stderr


def test_rollback_failure_reports_and_does_not_restart(simulated_upgrade):
    f = simulated_upgrade
    result = f["run"](FAIL_MV_CALLS="2", FAIL_RESTORE="1")
    assert result.returncode != 0
    assert "回退失败" in result.stderr
    assert f["state"].read_text() == "inactive"
    assert "systemctl start" not in f["log"].read_text()
    assert_preserved(f)


def test_failed_start_with_unstoppable_service_does_not_switch_back(simulated_upgrade):
    f = simulated_upgrade
    result = f["run"](FAIL_FIRST_START="1", FAIL_STOP_AFTER_START="1")
    assert result.returncode != 0
    assert (f["app"] / "src" / "new.py").exists()
    assert (f["app"] / "vendor" / "new.py").exists()
    assert (upgrade_backup(f) / "src" / "old.py").exists()
    assert f["log"].read_text().count("systemctl start vsphere-agent.service") == 1
    assert_preserved(f)
