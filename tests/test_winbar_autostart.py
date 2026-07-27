#!/usr/bin/env python3
"""Windows 托盘版「开机自启」的离线单测——不碰真注册表，Mac 上照样跑通。

用一个假 winreg 模块顶掉 sys.modules["winreg"]（真机上是 C 扩展，Mac 根本没有），
再把 sys.platform 临时改成 win32 骗过平台判断，这样这条 Windows-only 的代码路径
在开发机上就能被完整走一遍。覆盖：

1. 非 Windows 平台优雅降级（查询 False / 设置无操作 / 都不抛）
2. 启用 / 禁用 / 查询三条主路径 + 幂等与空值
3. 路径漂移自愈：旧路径改写 / 已一致不重复写 / 没开启时绝不偷偷开
4. frozen（打包 exe）与源码运行两种命令行拼接，含空格路径必须带引号、
   源码分支必须用 pythonw.exe（推导不出才退回）
5. 注册表被锁（读/写/删都抛）时静默失败而不是崩

跑法：python3 tests/test_winbar_autostart.py
"""
import importlib.util
import pathlib
import sys
import tempfile
import types

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# 托盘脚本文件名带连字符，不能直接 import，走 importlib（同 test_winbar_ipsec.py）
_spec = importlib.util.spec_from_file_location(
    "winbar_tray", _ROOT / "winbar" / "ai-limit-tray.py")
tray = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tray)

FAILS = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILS.append(name)
    print(f"  {'✓' if ok else '✗'} {name:52} {got}" + ("" if ok else f"  (期望 {want})"))


RUN = tray._RUN_KEY_PATH
NAME = tray._RUN_VALUE_NAME


# ── 假注册表 ─────────────────────────────────────────────────────────────────
class FakeRegistry:
    """winreg 的最小可用替身。只实现被调到的五个函数，语义按真 winreg 对齐：
    键 / 值不存在抛 FileNotFoundError（真 winreg 就是抛这个，OSError 的子类）。
    read_locked / write_locked 模拟组策略禁读禁写。"""

    HKEY_CURRENT_USER = 0x80000001
    REG_SZ = 1
    KEY_SET_VALUE = 0x0002
    KEY_WRITE = 0x20006

    def __init__(self, keys=None, read_locked=False, write_locked=False):
        self.keys = {k: dict(v) for k, v in (keys or {}).items()}
        self.read_locked = read_locked
        self.write_locked = write_locked
        self.writes = 0          # SetValueEx 调用次数，用来断言"没重复写"

    class _Handle:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return self._Handle(path)

    def CreateKeyEx(self, root, path, reserved=0, access=0):
        if self.write_locked:
            raise PermissionError("policy")
        self.keys.setdefault(path, {})
        return self._Handle(path)

    def QueryValueEx(self, handle, name):
        if self.read_locked:
            raise PermissionError("policy")
        values = self.keys[handle.path]
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], self.REG_SZ

    def SetValueEx(self, handle, name, reserved, typ, value):
        if self.write_locked:
            raise PermissionError("policy")
        self.writes += 1
        self.keys[handle.path][name] = value

    def DeleteValue(self, handle, name):
        if self.write_locked:
            raise PermissionError("policy")
        values = self.keys[handle.path]
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]


_REAL_PLATFORM = sys.platform


def install_fake(**kw) -> FakeRegistry:
    reg = FakeRegistry(**kw)
    mod = types.ModuleType("winreg")
    for attr in ("HKEY_CURRENT_USER", "REG_SZ", "KEY_SET_VALUE", "KEY_WRITE"):
        setattr(mod, attr, getattr(FakeRegistry, attr))
    for fn in ("OpenKey", "CreateKeyEx", "QueryValueEx", "SetValueEx", "DeleteValue"):
        setattr(mod, fn, getattr(reg, fn))
    sys.modules["winreg"] = mod
    sys.platform = "win32"
    return reg


def uninstall_fake():
    sys.modules.pop("winreg", None)
    sys.platform = _REAL_PLATFORM


def value_of(reg):
    return reg.keys.get(RUN, {}).get(NAME)


# ── 1. 非 Windows 降级 ───────────────────────────────────────────────────────
print("\n【非 Windows 优雅降级】")
check("开发机上 platform 不是 win32", sys.platform == "win32", False)
check("拿不到 winreg", tray._winreg_or_none(), None)
check("查询 → False 而不是异常", tray.autostart_enabled(), False)
check("启用 → 静默 False", tray.set_autostart(True), False)
check("禁用 → 静默 False", tray.set_autostart(False), False)
check("toggle → 静默 False", tray.toggle_autostart(), False)
check("自愈 → 不动作", tray.sync_autostart_path(), False)

# 就算 winreg 能 import（比如别的测试污染了 sys.modules），平台判断也要挡住：
# 在 macOS 上真去读 HKCU 是没有意义的，函数必须只认 win32
_leak = install_fake()
sys.platform = _REAL_PLATFORM
check("有 winreg 但平台不对 → 仍降级", tray.autostart_enabled(), False)
check("有 winreg 但平台不对 → 不写任何值", (tray.set_autostart(True), _leak.writes), (False, 0))
uninstall_fake()


# ── 2. 启用 / 查询 / 禁用 ────────────────────────────────────────────────────
print("\n【启用 / 查询 / 禁用】")
reg = install_fake()
check("Run 键整个不存在 → 未开启（不报错）", tray.autostart_enabled(), False)

check("启用返回成功", tray.set_autostart(True), True)
check("值写在 HKCU 的 Run 键下", RUN, r"Software\Microsoft\Windows\CurrentVersion\Run")
check("值名是 ai-limit-tray", NAME, "ai-limit-tray")
check("值 = 当前命令行", value_of(reg), tray.autostart_command())
check("查询 → 已开启", tray.autostart_enabled(), True)

before = reg.writes
tray.set_autostart(True)
check("重复启用幂等（只有一个值）", len(reg.keys[RUN]), 1)
check("重复启用仍写一次（不依赖旧值）", reg.writes - before, 1)

check("禁用返回成功", tray.set_autostart(False), True)
check("禁用后值被删掉", value_of(reg), None)
check("禁用后查询 → 未开启", tray.autostart_enabled(), False)
check("重复禁用不抛（值本来就没有）", tray.set_autostart(False), True)

check("toggle 关→开", (tray.toggle_autostart(), tray.autostart_enabled()), (True, True))
check("toggle 开→关", (tray.toggle_autostart(), tray.autostart_enabled()), (True, False))

reg.keys[RUN][NAME] = ""
check("空值当作未开启", tray.autostart_enabled(), False)
uninstall_fake()

# Run 键压根不存在时禁用：目标状态本来就成立，不该报成失败
reg = install_fake()
check("Run 键不存在时禁用 → 视作已达成", tray.set_autostart(False), True)
check("禁用不会凭空建出 Run 键", RUN in reg.keys, False)
uninstall_fake()


# ── 3. 路径漂移自愈 ─────────────────────────────────────────────────────────
print("\n【路径漂移自愈】")
OLD = r'"C:\Old\pythonw.exe" "C:\Old\ai-limit-tray.py"'
reg = install_fake(keys={RUN: {NAME: OLD}})
check("自愈前是旧路径", value_of(reg), OLD)
check("检测到漂移 → 返回已改写", tray.sync_autostart_path(), True)
check("值被改成当前路径", value_of(reg), tray.autostart_command())
check("自愈后菜单仍显示已开启", tray.autostart_enabled(), True)
uninstall_fake()

reg = install_fake(keys={RUN: {NAME: tray.autostart_command()}})
before = reg.writes
check("路径已一致 → 不改写", tray.sync_autostart_path(), False)
check("路径已一致 → 一次注册表写都没有", reg.writes - before, 0)
uninstall_fake()

# 最关键的一条：没开自启的用户，跑一次 App 不能被偷偷加上
reg = install_fake(keys={RUN: {"OtherApp": "whatever.exe"}})
check("键存在但没本 App 的值 → 不动作", tray.sync_autostart_path(), False)
check("没开自启的用户不会被偷偷开启", value_of(reg), None)
check("别人的启动项没被碰", reg.keys[RUN].get("OtherApp"), "whatever.exe")
uninstall_fake()

reg = install_fake()
check("Run 键都不存在 → 不动作", tray.sync_autostart_path(), False)
check("Run 键不会被自愈凭空建出来", RUN in reg.keys, False)
uninstall_fake()

reg = install_fake(keys={RUN: {NAME: OLD}}, write_locked=True)
check("漂移了但写不进去 → 静默 False", tray.sync_autostart_path(), False)
check("写不进去时旧值保持原样", value_of(reg), OLD)
uninstall_fake()


# ── 4. 命令行拼接（frozen vs 源码，含空格路径） ─────────────────────────────
print("\n【命令行拼接】")
_real_exe = sys.executable
_real_script = tray._SCRIPT_PATH
_had_frozen = hasattr(sys, "frozen")

# 4a. 打包 exe：只有 exe 自己，且必须带引号
sys.frozen = True
sys.executable = r"C:\Program Files\AI Limit\ai-limit-tray.exe"
cmd = tray.autostart_command()
check("frozen → 只有 exe 且带引号", cmd, r'"C:\Program Files\AI Limit\ai-limit-tray.exe"')
check("frozen → 不追加脚本路径", ".py" in cmd, False)
check("frozen → 引号成对", cmd.count('"'), 2)
del sys.frozen

# 4b. 源码运行：pythonw.exe + 脚本，两段各自带引号
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai limit "))   # 目录名故意带空格
(tmp / "python.exe").write_text("")
(tmp / "pythonw.exe").write_text("")
script_dir = tmp / "AI Limit"
script_dir.mkdir()
script = script_dir / "ai-limit-tray.py"
script.write_text("")
sys.executable = str(tmp / "python.exe")
tray._SCRIPT_PATH = script
cmd = tray.autostart_command()
check("源码 → pythonw + 脚本两段", cmd, f'"{tmp / "pythonw.exe"}" "{script}"')
check("源码 → 用 pythonw 而不是 python", cmd.split('" "')[0].endswith("pythonw.exe"), True)
check("空格路径两段都被引号包住", cmd.count('"'), 4)
check("解释器段左引号紧贴开头", cmd.startswith('"'), True)
check("脚本段右引号收尾", cmd.endswith('"'), True)

# 4c. 推导不出 pythonw（精简发行版）→ 退回 sys.executable，不能拼出不存在的路径
bare = pathlib.Path(tempfile.mkdtemp(prefix="bare "))
(bare / "python.exe").write_text("")
sys.executable = str(bare / "python.exe")
check("无 pythonw → 退回原解释器", tray.autostart_command(),
      f'"{bare / "python.exe"}" "{script}"')

# 4d. 写进注册表的就是拼出来的这一串（拼接与写入没脱节）
reg = install_fake()
sys.executable = str(tmp / "python.exe")
tray.set_autostart(True)
check("注册表里存的正是该命令行", value_of(reg), f'"{tmp / "pythonw.exe"}" "{script}"')
uninstall_fake()

sys.executable = _real_exe
tray._SCRIPT_PATH = _real_script
if _had_frozen:      # 理论上跑测试时不会有，兜一手别把解释器状态改坏
    sys.frozen = True


# ── 5. 注册表被锁 ───────────────────────────────────────────────────────────
print("\n【注册表被策略锁定时不崩】")
reg = install_fake(write_locked=True)
check("写不进去 → 启用返回 False", tray.set_autostart(True), False)
check("写不进去 → 没有残留值", value_of(reg), None)
check("写不进去 → toggle 也只返回 False", tray.toggle_autostart(), False)
uninstall_fake()

reg = install_fake(keys={RUN: {NAME: "whatever"}}, read_locked=True)
check("读不出来 → 查询按未开启处理", tray.autostart_enabled(), False)
check("读不出来 → 自愈不动作", tray.sync_autostart_path(), False)
uninstall_fake()

reg = install_fake(keys={RUN: {NAME: "whatever"}}, write_locked=True)
check("删不掉 → 禁用返回 False", tray.set_autostart(False), False)
check("删不掉 → 值仍在（状态如实反映）", value_of(reg), "whatever")
uninstall_fake()


# ── 6. 状态不落本地文件 ─────────────────────────────────────────────────────
print("\n【开关状态只认注册表】")
check("state 文件不掺开机自启字段", "autostart" in tray.load_state(), False)
check("state 只有 mode / refresh_min", sorted(tray.load_state()), ["mode", "refresh_min"])

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
