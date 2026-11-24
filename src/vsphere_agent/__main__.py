"""采集服务命令行入口。"""

import argparse
import logging
import signal
import sys

from .config import ConfigurationError, load_config
from .service import Service


def main(argv=None):
    parser = argparse.ArgumentParser(description="vSphere 事件、任务和告警采集服务")
    parser.add_argument("--config", required=True, help="受限权限 YAML 配置文件路径")
    parser.add_argument("--check-config", action="store_true", help="仅校验配置，不连接服务")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if args.check_config:
        print("配置格式有效；尚未验证远端连接、权限或 Data Stream。")
        return 0
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    # ES 写入故障由服务统一限频记录，避免客户端节点日志绕过限频。
    logging.getLogger("elastic_transport").propagate = False
    try:
        service = Service(config)
        signal.signal(signal.SIGTERM, lambda signum, frame: service.stop())
        signal.signal(signal.SIGINT, lambda signum, frame: service.stop())
        service.run()
    except Exception as exc:
        print(f"采集服务退出：{type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
