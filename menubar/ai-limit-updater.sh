#!/bin/sh
# ai-limit-updater.sh — 一键更新 helper 脚本
#
# 由 ai-limit-app.py 在校验通过新版 DMG 后 detached 启动，主 App 随即退出，
# 剩下的"等旧进程退出 -> 原子替换 -> 拉起新版"全部由这个脚本完成。这个脚本
# 打包进 Contents/Resources（随 App 一起签名封印），但每次触发前 Python 会把
# 它拷到本次更新专用的临时目录再执行——避免"脚本执行到一半又要替换掉脚本
# 自己所在的 bundle"这个 self-modifying-script 陷阱。
#
# 用法（全部位置参数，由 Python 侧拼好，不接受省略）：
#   ai-limit-updater.sh <new_app> <target_app> <old_pid> <log_path> <marker_path> <cleanup_dir>
#
# 失败时尽力回滚到旧版并写 marker 文件，不让用户的 App 从此打不开。

set -u

NEW_APP="$1"
TARGET="$2"
OLD_PID="$3"
LOG="$4"
MARKER="$5"
CLEANUP_DIR="$6"

exec >>"$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') updater 启动 ==="
echo "NEW_APP=$NEW_APP TARGET=$TARGET OLD_PID=$OLD_PID"

BACKUP="${TARGET}.old"

write_marker() {
    # reason/detail 都是脚本内部预定义的固定枚举字符串，值由脚本自己掌控，
    # 手工拼 JSON 足够安全，不需要引入 JSON 库。
    reason="$1"
    detail="$2"
    printf '{"reason": "%s", "detail": "%s", "time": "%s"}\n' \
        "$reason" "$detail" "$(date '+%Y-%m-%d %H:%M:%S')" > "$MARKER"
    echo "已写失败 marker: $MARKER ($reason: $detail)"
}

cleanup_tmp() {
    if [ -n "$CLEANUP_DIR" ] && [ -d "$CLEANUP_DIR" ]; then
        rm -rf "$CLEANUP_DIR"
        echo "已清理临时目录 $CLEANUP_DIR"
    fi
}

# ── 1. 等旧进程退出（最多 20 秒，每 0.5 秒轮询一次）──────────────────────────
echo "等待旧进程 (PID $OLD_PID) 退出…"
i=0
while [ "$i" -lt 40 ]; do
    if ! kill -0 "$OLD_PID" 2>/dev/null; then
        echo "旧进程已退出"
        break
    fi
    # PID 存在不代表还是我们的进程：可能已退出、系统把 PID 复用给了无关进程。
    comm=$(ps -p "$OLD_PID" -o comm= 2>/dev/null)
    case "$comm" in
        *ai-limit*) ;;
        *)
            echo "PID $OLD_PID 命令名已不是 ai-limit（${comm}），视为已退出"
            break
            ;;
    esac
    i=$((i + 1))
    sleep 0.5
done

if [ "$i" -ge 40 ]; then
    comm=$(ps -p "$OLD_PID" -o comm= 2>/dev/null)
    case "$comm" in
        *ai-limit*)
            echo "等待超时，强制 kill -9 $OLD_PID"
            kill -9 "$OLD_PID" 2>/dev/null
            sleep 1
            ;;
    esac
fi

# ── 2. 清理残留脏状态（上次更新中途崩溃/被杀留下的痕迹）──────────────────────
if [ -d "$BACKUP" ] && [ ! -d "$TARGET" ]; then
    echo "发现残留 .old 且目标不存在：上次卡在替换前，先恢复"
    mv "$BACKUP" "$TARGET"
elif [ -d "$BACKUP" ] && [ -d "$TARGET" ]; then
    echo "发现残留 .old 且目标也存在：上次卡在清理前，.old 是垃圾，直接删"
    rm -rf "$BACKUP"
fi

# ── 3. 磁盘空间二次确认（Python 侧下载前已查过一次，这里是替换那一刻的兜底）──
new_size_kb=$(du -sk "$NEW_APP" 2>/dev/null | awk '{print $1}')
free_kb=$(df -k "$(dirname "$TARGET")" 2>/dev/null | awk 'NR==2 {print $4}')
if [ -n "$new_size_kb" ] && [ -n "$free_kb" ] && [ "$free_kb" -lt "$new_size_kb" ]; then
    echo "磁盘空间不足：新版 ${new_size_kb}KB，剩余 ${free_kb}KB"
    write_marker "insufficient_disk_space" "磁盘空间不足，无法完成替换"
    cleanup_tmp
    exit 1
fi

# ── 4. 原子替换：先改名备份，成功才删备份；失败则整体回滚 ────────────────────
replace_failed=0

if [ -d "$TARGET" ]; then
    if ! mv "$TARGET" "$BACKUP"; then
        echo "mv 目标到 .old 失败"
        write_marker "backup_failed" "无法备份当前版本，已取消本次更新"
        cleanup_tmp
        exit 1
    fi
fi

if ! cp -R "$NEW_APP" "$TARGET"; then
    replace_failed=1
    echo "cp -R 失败"
elif [ ! -d "$TARGET/Contents/MacOS" ] || [ ! -x "$TARGET/Contents/MacOS/ai-limit" ]; then
    replace_failed=1
    echo "复制后结构抽检未通过（Contents/MacOS/ai-limit 缺失或不可执行）"
fi

if [ "$replace_failed" -eq 1 ]; then
    echo "替换失败，开始回滚"
    rm -rf "$TARGET"
    if [ -d "$BACKUP" ]; then
        mv "$BACKUP" "$TARGET"
        echo "已回滚到旧版"
        open -a "$TARGET" 2>/dev/null
        write_marker "replace_failed" "自动更新失败，已回退到当前版本"
    else
        echo "没有 .old 可回滚（首装场景失败），App 目前不可用"
        write_marker "replace_failed_no_backup" "自动更新失败且无法回退，请手动重装"
    fi
    cleanup_tmp
    exit 1
fi

# ── 5. 成功：清理 + 拉起新版 ─────────────────────────────────────────────────
rm -rf "$BACKUP"
rm -f "$MARKER"
echo "替换成功，拉起新版"
open -a "$TARGET"
cleanup_tmp
echo "=== $(date '+%Y-%m-%d %H:%M:%S') updater 完成 ==="
exit 0
