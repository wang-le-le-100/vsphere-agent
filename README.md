# vSphere Agent

通过 vSphere Management API 采集 vCenter 事件、任务和告警，转换为结构化日志并发送到 Elasticsearch Data Stream。此项目是独立社区项目，与 VMware / Broadcom 无隶属关系。

## 当前能力与限制

- 事件：采集当前会话有效边界之后的事件；任务：发现任务并跟踪可见的状态变化；告警：周期性采集可见对象的告警快照。
- 输出：Elasticsearch 8.x Data Stream，使用 Bulk `create`；必须先创建 Data Stream。Kafka 和 Logstash 输出尚未实现。
- 语言：尽力请求简体中文，服务端不支持时保留接口返回的语言。
- 可靠性：待发送记录仅保存在有限内存中，缓冲区满、进程退出或 ES 不可用时可能丢弃；没有持久化 Outbox 或断点补发。首次启动和会话重连以新的时间边界开始，停机或断线期间的事件、任务不保证补采。告警快照不等同于完整告警变化历史。
- SQLite 只保存有限的事件去重和告警比较状态，不保存业务文档或待发送队列。

## 准备

- Python **3.10**；依赖版本范围见 `pyproject.toml`，锁定依赖见 `uv.lock`。
- 一个可通过 HTTPS 访问的 vCenter 和专用最小权限账号。账号须能查看目标对象、事件、任务和告警；可见范围由 vCenter 授权决定，请根据实际版本测试权限。
- Elasticsearch 8.x；为配置中四个路由预建 Data Stream，写入账号需有相应 Data Stream 的 `create_doc` 和 `view_index_metadata` 等必要权限。请按集群安全策略验证权限及探测接口访问。
- 兼容性和吞吐量尚需在目标 vCenter / ES 版本及规模下验证，不能仅凭单元测试推断生产可用性。

## Elasticsearch Data Stream 示例

以下示例在 Elasticsearch **Dev Tools** 中执行，创建一个测试用模板和四个 Data Stream。示例没有配置生命周期或生产 mapping；生产环境请先规划保留期、mapping、权限，并检查是否与已有模板冲突。

```http
PUT _index_template/vsphere-agent-example
{
  "index_patterns": ["logs-example.*-test"],
  "priority": 500,
  "data_stream": {},
  "template": {
    "mappings": {"properties": {"@timestamp": {"type": "date"}}}
  }
}

PUT _data_stream/logs-example.events-test
PUT _data_stream/logs-example.tasks-test
PUT _data_stream/logs-example.alarms-test
PUT _data_stream/logs-example.agent-test
```

这些示例会修改 ES 集群；运行前请确认授权。Agent 不负责自动创建模板和 Data Stream。

## 本地快速开始

以下命令在项目源码仓库根目录执行；单独安装 wheel 或解压 Python 源码分发包不包含离线脚本及示例配置。以下为 Linux 示例，命令中的配置占位值必须由使用者在本地安全填写；不要把真实配置提交到 Git。Windows 开发环境可使用对应的虚拟环境命令，systemd 离线安装仅针对 Ubuntu 22.04。

```bash
python3.10 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
install -m 600 config.example.yaml config.yaml
```

编辑 `config.yaml`：填写 vCenter 地址及账号、ES 地址及账号、已创建的四个 Data Stream；将 `state.path` 改为当前用户可写的 SQLite 文件路径。保持配置文件仅授权用户可读。

```bash
python -m vsphere_agent --config config.yaml --check-config
python -m vsphere_agent --config config.yaml
```

`--check-config` 仅验证配置格式，**不会**验证远端连通性、权限、CA 文件或 Data Stream 是否存在。启动后通过 ES 检查对应数据流是否有新数据，例如在 Dev Tools 查询：

```http
GET logs-example.events-test/_search
{
  "size": 5,
  "sort": [{"@timestamp": "desc"}]
}
```

事件、任务日志只反映启动后实际产生且成功发送的数据；告警按配置周期生成快照。无记录时先触发一个可观察的测试事件，再检查日志和权限。

## 安全配置

- `vcenter.verify_ssl` 默认 `true`，校验证书及主机名。使用私有 CA 时可指定 `vcenter.ca_file`。仅在理解中间人攻击风险且没有更好选择时，才显式设为 `false`；此时不要设置 `ca_file`。
- Elasticsearch 推荐 `https://`；私有 CA 可指定 `elasticsearch.ca_file`。`http://` 仅供受控环境使用，会明文传输凭据和数据。
- 配置密码按文件权限保护；建议将实际配置放在仓库外，勿将配置、日志、SQLite、私钥或离线包中的敏感内容提交仓库。日志可能包含用户名、对象名称和业务信息，应限制访问。内置脱敏和 `.gitignore` 均不能保证识别或清除所有敏感信息，提交时应逐项审查文件和 Git 历史。
- 当前开源分支默认启用 vCenter 证书校验；由旧版本升级时，若旧环境依赖未验证的自签证书，需先为现场配置添加 CA，或明确设置 `verify_ssl: false`。升级脚本不会修改现有配置。

## Ubuntu 22.04 离线安装与升级

构建依赖需要与 `uv.lock` / `ops/build_offline.py` 中的版本一致；构建器只读取本地已安装依赖，不访问网络。生成的包只带示例配置，不读取本地业务配置。

```bash
python ops/build_offline.py --output vsphere-agent-offline.tar.gz
tar -xzf vsphere-agent-offline.tar.gz
```

将包传到目标机器；首次安装前在**包目录外**创建受限权限的实际配置文件，填写真实配置并保证其 SQLite 路径为 `/var/lib/vsphere-agent/state.sqlite`。以下以该外部文件路径为示例：

```bash
sudo bash vsphere-agent-offline/install.sh /path/to/private-config.yaml
```

脚本会安装配置、程序和 systemd 单元并启动服务。已有符合脚本预检条件的部署可使用以下命令升级；升级保留原配置、SQLite 和服务单元，并尽量在失败时回退程序：

```bash
sudo bash vsphere-agent-offline/install.sh
systemctl status vsphere-agent --no-pager
journalctl -u vsphere-agent -n 100 --no-pager
```

脚本仅适用于 Ubuntu 22.04 和预期的目录布局。升级涉及停机、备份和程序切换，应先阅读 `ops/install-offline.sh` 并确认现场状态；停机期间事件和任务不补采。`active` 不代表 vCenter 或 ES 已连通，必须另行验证实际写入。

## 数据字段

各记录包含 `@timestamp`、`ingest_time`、`data_type` 和 vCenter 标识；事件通常包含 `event_id`、`message`，任务包含 `task_key`、`state`，告警包含 `snapshot_id` 等字段。字段因 vCenter 返回内容和权限而异；现有格式不宣称完整兼容 ECS。

## 开发与测试

```bash
python -m pytest -q
```

测试主要使用模拟对象，不替代真实 vCenter、Elasticsearch 和 Ubuntu systemd 的集成验收。离线包测试依赖对应版本的本地依赖；指定 `VSPHERE_TEST_BUNDLE` 时还会核对实际构建产物。

## 许可证与发布

本项目采用 [Apache License 2.0](LICENSE)。正式发布前，权利人仍须确认代码权属及第三方依赖授权，审查完整 Git 历史和发布产物，移除真实凭据及内部环境信息。