#!/usr/bin/env bash
# Ubuntu 22.04 离线首次安装或保留配置和状态的升级入口。
set -Eeuo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
umask 077

app=/opt/vsphere-agent
config=/etc/vsphere-agent/config.yaml
db=/var/lib/vsphere-agent/state.sqlite
unit=/etc/systemd/system/vsphere-agent.service
python=/usr/bin/python3.10
lock=/run/vsphere-agent-deploy.lock
mode=preflight
changed=0
work=""
backup=""
was_running=0
stopped=0
old_src=0
old_vendor=0
new_src=0
new_vendor=0
start_attempted=0

fail() { printf '%s\n' "$1" >&2; return 1; }
property() { systemctl show vsphere-agent.service -p "$1" --value; }
check_stopped() {
    [[ $(property ActiveState) == inactive && $(property SubState) == dead && $(property MainPID) == 0 ]]
}
sqlite_sidecars_clear() {
    local suffix
    for suffix in -journal -wal -shm; do
        [ ! -e "$db$suffix" ] && [ ! -L "$db$suffix" ] || return 1
    done
}
on_error() {
    local rc=$? rollback_ok=1
    trap - ERR INT TERM
    set +e
    if [[ "$mode" != upgrade ]]; then
        if (( changed )); then
            printf '%s\n' '安装中途失败，可能已创建账户、文件或服务；请检查后手动处理。' >&2
        else
            printf '%s\n' '部署前检查失败，尚未进行安装。' >&2
        fi
        exit "$rc"
    fi
    printf '%s\n' '升级失败。' >&2
    if (( stopped )); then
        if (( start_attempted )); then
            if ! systemctl stop vsphere-agent.service || ! check_stopped; then
                printf '%s\n' '无法确认服务已停止，未自动回退；请保留现场并人工检查。' >&2
                [[ -z "$work" ]] || printf '工作目录：%s\n' "$work" >&2
                exit "$rc"
            fi
        elif ! check_stopped; then
            printf '%s\n' '服务状态不安全，未自动回退；请保留现场并人工检查。' >&2
            [[ -z "$work" ]] || printf '工作目录：%s\n' "$work" >&2
            exit "$rc"
        fi
        if (( new_src )); then
            if mv -- "$app/src" "$work/failed-src"; then new_src=0; else rollback_ok=0; fi
        fi
        if (( old_src )) && (( ! new_src )); then
            if cp -a -- "$backup/src" "$app/src"; then old_src=0; else rollback_ok=0; fi
        fi
        if (( new_vendor )); then
            if mv -- "$app/vendor" "$work/failed-vendor"; then new_vendor=0; else rollback_ok=0; fi
        fi
        if (( old_vendor )) && (( ! new_vendor )); then
            if cp -a -- "$backup/vendor" "$app/vendor"; then old_vendor=0; else rollback_ok=0; fi
        fi
        if (( old_src || old_vendor || new_src || new_vendor )); then rollback_ok=0; fi
        if (( rollback_ok )); then
            printf '%s\n' '原程序和依赖已恢复或未发生变化；现场配置和 SQLite 未覆盖。' >&2
            if (( was_running )); then
                if ! sqlite_sidecars_clear; then
                    printf '%s\n' 'SQLite 存在附属文件，服务保持停止；请先检查状态文件。' >&2
                elif systemctl start vsphere-agent.service && sleep 5 && systemctl is-active --quiet vsphere-agent.service; then
                    printf '%s\n' '已尝试恢复原服务运行状态；业务连接和写入仍需核验。' >&2
                else
                    printf '%s\n' '原服务恢复启动失败，请检查 systemd 日志。' >&2
                fi
            fi
        else
            printf '%s\n' '自动回退失败，服务保持停止；请保留现场和备份并人工处理。' >&2
        fi
    else
        printf '%s\n' '程序尚未切换；服务停止失败或状态未经确认时不会自动切换。' >&2
    fi
    [[ -z "$work" ]] || printf '工作目录：%s\n' "$work" >&2
    exit "$rc"
}
trap on_error ERR
trap 'fail "收到中断信号。"' INT TERM

[ "$#" -le 1 ] || fail '用法：首次安装传入外部配置绝对路径；升级不带参数。'
input_config=${1:-}
BASE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$BASE"
[ "$(id -u)" -eq 0 ] || fail '请使用 sudo bash install.sh 执行。'
[ -r /etc/os-release ] || fail '无法读取系统版本。'
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = 22.04 ] || fail '仅支持 Ubuntu 22.04。'
[ -x "$python" ] || fail '缺少 /usr/bin/python3.10；请先离线安装 Ubuntu Python 3.10。'
"$python" -S -c 'import sys, ssl, sqlite3, ctypes; assert sys.version_info[:2] == (3, 10)' || fail 'Python 3.10 或其 ssl/sqlite3/ctypes 标准库不可用。'
systemctl show --property=Version --value >/dev/null || fail 'systemd 不可用，请在使用 systemd 的系统上执行。'
[ ! -L "$lock" ] && { [ ! -e "$lock" ] || [ -f "$lock" ]; } || fail '部署锁路径不安全。'
exec 9>"$lock"
flock -n 9 || fail '已有部署或升级操作正在进行。'
[ -f SHA256SUMS ] && [ -f config.example.yaml ] && [ -d src ] && [ -d vendor ] && [ -f vsphere-agent.service ] || fail '离线包不完整。'
[ -z "$(find . ! -type d ! -type f -print -quit)" ] || fail '离线包包含不受支持的链接或特殊文件。'
cmp -s <(cut -c 67- SHA256SUMS | sort) <(find . -type f ! -path './SHA256SUMS' -printf '%P\n' | sort) || fail '离线包包含缺失或未列入清单的文件。'
sha256sum -c SHA256SUMS >/dev/null || fail '离线包校验失败。'

if [ -e "$app" ] || [ -L "$app" ] || [ -e /etc/vsphere-agent ] || [ -L /etc/vsphere-agent ] || [ -e /var/lib/vsphere-agent ] || [ -L /var/lib/vsphere-agent ] || [ -e "$unit" ] || [ -L "$unit" ] || getent passwd vsphere >/dev/null || getent group vsphere >/dev/null || systemctl cat vsphere-agent.service >/dev/null 2>&1; then
    mode=upgrade
    [ -z "$input_config" ] || fail '升级保留现场配置，请勿传入配置文件。'
    for directory in "$app" "$app/src" "$app/vendor" /etc/vsphere-agent /var/lib/vsphere-agent; do
        [ -d "$directory" ] && [ ! -L "$directory" ] || fail "检测到已有部署路径，但部署不完整或路径不安全：$directory"
    done
    for file in "$config" "$db" "$unit"; do
        [ -f "$file" ] && [ ! -L "$file" ] || fail "检测到已有部署路径，但部署不完整或路径不安全：$file"
    done
    [ -z "$(find "$app/src" "$app/vendor" ! -type d ! -type f -print -quit)" ] || fail '现有程序或依赖包含链接或特殊文件。'
    getent passwd vsphere >/dev/null && getent group vsphere >/dev/null || fail '现有服务账户或组不存在。'
    [ "$(property LoadState)" = loaded ] && [ "$(property FragmentPath)" = "$unit" ] && [ -z "$(property DropInPaths)" ] || fail '服务单元位置或覆盖配置不符合预期。'
    [ "$(property User)" = vsphere ] && [ "$(property Group)" = vsphere ] && [ "$(property WorkingDirectory)" = "$app" ] || fail '服务账户或工作目录不符合预期。'
    environment=$(property Environment)
    [[ " $environment " == *" PYTHONPATH=$app/src:$app/vendor "* ]] || fail '服务 Python 路径不符合预期。'
    exec_start=$(property ExecStart)
    [[ "$exec_start" == *"argv[]=$python -S -m vsphere_agent --config $config"* ]] || fail '服务启动命令不符合预期。'
    state=$(property ActiveState)
    substate=$(property SubState)
    pid=$(property MainPID)
    if [[ "$state" == active && "$substate" == running && "$pid" =~ ^[1-9][0-9]*$ ]]; then
        was_running=1
    elif [[ "$state" == inactive && "$substate" == dead && "$pid" == 0 ]]; then
        was_running=0
    else
        fail '服务处于不明确或不支持的状态，未执行升级。'
    fi
    mount=$(findmnt -n -o TARGET -T "$app")
    [ -n "$mount" ] && [ "$(findmnt -n -o TARGET -T "$app/src")" = "$mount" ] && [ "$(findmnt -n -o TARGET -T "$app/vendor")" = "$mount" ] || fail '程序和依赖目录不在同一挂载点。'
    required=$(du -sk "$BASE/src" "$BASE/vendor" "$db" "$config" "$unit" | awk '{sum += $1} END {print sum + 10240}')
    available=$(df -Pk "$app" | awk 'NR == 2 {print $4}')
    [[ "$required" =~ ^[0-9]+$ && "$available" =~ ^[0-9]+$ ]] && (( available >= required )) || fail '程序所在文件系统可用空间不足或无法检查。'
    work=$(mktemp -d "$app/.upgrade-XXXXXXXX")
    chmod 0755 "$work"
    backup="$work/backup"
    mkdir -m 0700 -- "$backup"
    cp -a -- "$BASE/src" "$work/new-src"
    cp -a -- "$BASE/vendor" "$work/new-vendor"
    chown -R root:root "$work/new-src" "$work/new-vendor"
    chmod -R u=rwX,go=rX "$work/new-src" "$work/new-vendor"
    (cd / && runuser -u vsphere -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$work/new-src:$work/new-vendor" "$python" -S -m vsphere_agent --config "$config" --check-config) || fail '新程序或现场配置校验失败。'
    (cd / && runuser -u vsphere -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$work/new-src:$work/new-vendor" "$python" -S -c 'import sys; from vsphere_agent.config import load_config; assert load_config(sys.argv[1]).state.path == sys.argv[2]' "$config" "$db") || fail '现场 SQLite 路径不符合预期。'
    if (( was_running )); then
        printf '%s\n' '正在停止服务；等待时间可能达到 150 秒，停机期间事件和任务不会补采。'
        systemctl stop vsphere-agent.service || fail '服务停止失败，未切换程序。'
    fi
    check_stopped || fail '无法确认服务完全停止，未切换程序。'
    stopped=1
    sqlite_sidecars_clear || fail 'SQLite 存在附属文件，请先核查。'
    cp -a -- "$config" "$backup/config.yaml"
    cp -a -- "$db" "$backup/state.sqlite"
    cp -a -- "$unit" "$backup/vsphere-agent.service"
    cmp -s -- "$config" "$backup/config.yaml" && cmp -s -- "$db" "$backup/state.sqlite" && cmp -s -- "$unit" "$backup/vsphere-agent.service" || fail '现场文件备份校验失败。'
    check_stopped || fail '服务状态发生变化，未切换程序。'
    cmp -s -- "$config" "$backup/config.yaml" && cmp -s -- "$db" "$backup/state.sqlite" && cmp -s -- "$unit" "$backup/vsphere-agent.service" || fail '现场文件在升级期间发生变化。'
    mv -- "$app/src" "$backup/src"
    old_src=1
    mv -- "$work/new-src" "$app/src"
    new_src=1
    mv -- "$app/vendor" "$backup/vendor"
    old_vendor=1
    mv -- "$work/new-vendor" "$app/vendor"
    new_vendor=1
    (cd / && runuser -u vsphere -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$app/src:$app/vendor" "$python" -S -m vsphere_agent --config "$config" --check-config) || fail '切换后配置校验失败。'
    if (( was_running )); then
        start_attempted=1
        systemctl start vsphere-agent.service || fail '新服务启动失败。'
        sleep 5
        systemctl is-active --quiet vsphere-agent.service || fail '新服务未保持 active。'
        printf '%s\n' '服务已启动；远端连接、数据写入和中文内容仍需现场验证。'
    else
        printf '%s\n' '升级前服务处于停止状态，升级后继续保持停止。'
    fi
    printf '升级完成，备份目录：%s\n' "$backup"
    printf '%s\n' '查看状态：systemctl status vsphere-agent --no-pager'
    printf '%s\n' '查看日志：journalctl -u vsphere-agent -n 100 --no-pager'
else
    mode=install
    for path in "$app" /etc/vsphere-agent /var/lib/vsphere-agent "$unit" /usr/lib/systemd/system/vsphere-agent.service /lib/systemd/system/vsphere-agent.service /run/systemd/system/vsphere-agent.service; do
        [ ! -e "$path" ] && [ ! -L "$path" ] || fail "检测到已有部署路径：$path，已停止。"
    done
    if getent passwd vsphere >/dev/null || getent group vsphere >/dev/null || systemctl cat vsphere-agent.service >/dev/null 2>&1; then
        fail '检测到已有 vsphere 账户、组或服务，已停止。'
    fi
    [[ "$input_config" == /* ]] && [ -f "$input_config" ] && [ ! -L "$input_config" ] && [ -r "$input_config" ] || fail '首次安装需要单独提供可读取的配置文件绝对路径。'
    case "$input_config" in "$BASE"/*) fail '配置文件必须放在离线包目录外。' ;; esac
    PYTHONPATH="$BASE/src:$BASE/vendor" PYTHONDONTWRITEBYTECODE=1 "$python" -S -m vsphere_agent --config "$input_config" --check-config
    PYTHONPATH="$BASE/src:$BASE/vendor" PYTHONDONTWRITEBYTECODE=1 "$python" -S -c 'import sys; from vsphere_agent.config import load_config; assert load_config(sys.argv[1]).state.path == sys.argv[2]' "$input_config" "$db" || fail '配置 SQLite 路径不符合预期。'
    changed=1
    groupadd --system vsphere
    useradd --system --gid vsphere --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin vsphere
    install -d -m 0755 -o root -g root "$app"
    cp -R -- "$BASE/src" "$BASE/vendor" "$app/"
    chown -R root:root "$app"
    chmod -R u=rwX,go=rX "$app"
    install -d -m 0750 -o root -g vsphere /etc/vsphere-agent
    install -m 0640 -o root -g vsphere "$input_config" "$config"
    install -d -m 0700 -o vsphere -g vsphere /var/lib/vsphere-agent
    install -m 0644 -o root -g root "$BASE/vsphere-agent.service" "$unit"
    systemctl daemon-reload
    systemctl enable --now vsphere-agent.service
    systemctl is-active --quiet vsphere-agent.service || fail '服务未处于 active 状态，请检查 systemd 日志。'
    printf '%s\n' '服务已启动；远端连接、权限、数据写入及 mapping 仍需现场验证。'
    printf '%s\n' '查看状态：systemctl status vsphere-agent --no-pager'
    printf '%s\n' '查看日志：journalctl -u vsphere-agent -n 100 --no-pager'
fi