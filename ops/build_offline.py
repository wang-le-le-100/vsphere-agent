"""从本地依赖生成不含现场配置的离线部署包，不访问网络。"""
import argparse
import base64
import csv
import hashlib
import importlib.metadata as metadata
import io
from pathlib import Path
import os
import re
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "vsphere-agent-offline"
# 与 uv.lock 中的运行依赖版本对应；不携带 Windows 原生扩展。
DISTRIBUTIONS = {
    "pyvmomi": "8.0.3.0.1", "elasticsearch": "8.19.3",
    "elastic-transport": "8.19.1", "PyYAML": "6.0.4",
    "certifi": "2026.11.20", "urllib3": "2.8.0", "six": "1.17.0",
    "python-dateutil": "3.0.0", "typing-extensions": "4.16.0", "tzdata": "2026.6",
}
RUNTIME_ROOTS = {
    "pyVim", "pyVmomi", "elasticsearch", "elastic_transport", "yaml", "certifi",
    "urllib3", "six.py", "dateutil", "typing_extensions.py", "tzdata",
}


def collect_dependencies():
    files = {}
    for name, version in DISTRIBUTIONS.items():
        dist = metadata.distribution(name)
        if dist.version != version or not dist.files:
            raise ValueError("本地依赖版本与离线构建清单不一致")
        for item in dist.files:
            parts = item.parts
            if not parts or "__pycache__" in parts:
                continue
            top = parts[0]
            is_metadata = top.lower().endswith(".dist-info") and (
                parts[-1] == "METADATA" or any(part.lower() == "licenses" for part in parts)
                or parts[-1].lower().startswith(("license", "notice", "copying"))
            )
            is_runtime = top in RUNTIME_ROOTS and (item.suffix in {".py", ".pem", ".txt", ".json"} or top == "tzdata")
            if not is_metadata and not is_runtime:
                continue
            if item.hash is None or item.hash.mode != "sha256" or ".." in parts:
                raise ValueError("依赖文件缺少可验证的哈希")
            content = Path(dist.locate_file(item)).read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode("ascii")
            if digest != item.hash.value:
                raise ValueError("本地依赖文件校验失败")
            destination = "vendor/" + item.as_posix()
            if destination in files:
                raise ValueError("依赖文件路径冲突")
            files[destination] = content
    for path in ("vendor/yaml/__init__.py", "vendor/pyVmomi/__init__.py", "vendor/certifi/cacert.pem", "vendor/elasticsearch/__init__.py"):
        if path not in files:
            raise ValueError("离线依赖不完整")
    return files


def secure_output_permissions(output):
    """限制归档的访问权限。"""
    if os.name != "nt":
        os.chmod(output, 0o600)
        return
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    identity = subprocess.run(
        [str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    rows = list(csv.reader(io.StringIO(identity.stdout)))
    if len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r"S-1-(?:\d+-)*\d+", rows[0][1]):
        raise ValueError("无法识别当前 Windows 用户 SID")
    subprocess.run(
        [str(system32 / "icacls.exe"), str(output), "/inheritance:r", "/grant:r",
         f"*{rows[0][1]}:(F)", "*S-1-5-18:(F)", "*S-1-5-32-544:(F)"],
        check=True, capture_output=True,
    )


def create_bundle(output):
    files = collect_dependencies()
    files["LICENSE"] = (ROOT / "LICENSE").read_bytes()
    files["config.example.yaml"] = (ROOT / "config.example.yaml").read_bytes()
    files["install.sh"] = (ROOT / "ops" / "install-offline.sh").read_bytes().replace(b"\r\n", b"\n")
    files["vsphere-agent.service"] = (ROOT / "ops" / "vsphere-agent.service").read_bytes().replace(b"\r\n", b"\n")
    for source in (ROOT / "src" / "vsphere_agent").rglob("*.py"):
        files["src/" + source.relative_to(ROOT / "src").as_posix()] = source.read_bytes()
    manifest = "".join(f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in sorted(files.items()))
    files["SHA256SUMS"] = manifest.encode("ascii")

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        root = tarfile.TarInfo(PREFIX)
        root.type, root.mode, root.mtime = tarfile.DIRTYPE, 0o700, 0
        tar.addfile(root)
        for name, content in sorted(files.items()):
            entry = tarfile.TarInfo(f"{PREFIX}/{name}")
            entry.mode = 0o755 if name == "install.sh" else 0o644
            entry.size, entry.mtime = len(content), 0
            tar.addfile(entry, io.BytesIO(content))
    with output.open("xb") as target:
        secure_output_permissions(output)
        target.write(data.getvalue())


def main():
    parser = argparse.ArgumentParser(description="生成不含真实配置的离线部署包")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        create_bundle(args.output)
    except (OSError, ValueError, metadata.PackageNotFoundError, subprocess.SubprocessError) as exc:
        print(f"离线包构建失败：{type(exc).__name__}", file=sys.stderr)
        return 1
    print("离线包已生成；首次安装需要单独提供配置文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())