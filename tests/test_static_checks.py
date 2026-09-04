#!/usr/bin/env python3
"""静态检查：函数体里引用了不存在的名字（NameError 只在运行到那一行才爆）。

起因：usage.py 的 `render_codex` 里写了 `_WARN`（真名是 `_WRN`），CLI 在
「无 Codex 权限」分支必崩——那条分支平时走不到，所以一直没被发现。这类
bug 靠人眼和普通测试都容易漏，用 AST 扫一遍最省事。

不联网、不跑被测代码，只解析源码。
跑法：python3 tests/test_static_checks.py
"""
import ast
import builtins
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent

FAILS = []

TARGETS = [
    "usage.py", "quotacore.py", "ipsec.py", "boardlink.py",
    "menubar/ai-limit-app.py", "menubar/panelui.py",
    "winbar/ai-limit-tray.py", "linuxbar/ai-limit-tray.py",
]


def _bound_names(node):
    """一个作用域里被绑定的名字：参数、赋值、for、with、except、import、
    嵌套定义、海象、推导式变量。宁可多收（漏报）也不误报。"""
    out = set()
    args = getattr(node, "args", None)
    if args is not None:
        for a in list(args.args) + list(args.kwonlyargs) + list(getattr(args, "posonlyargs", [])):
            out.add(a.arg)
        if args.vararg:
            out.add(args.vararg.arg)
        if args.kwarg:
            out.add(args.kwarg.arg)
    for x in ast.walk(node):
        if isinstance(x, ast.Assign):
            for t in x.targets:
                out.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
        elif isinstance(x, (ast.AugAssign, ast.AnnAssign)):
            out.update(n.id for n in ast.walk(x.target) if isinstance(n, ast.Name))
        elif isinstance(x, (ast.For, ast.AsyncFor, ast.comprehension)):
            out.update(n.id for n in ast.walk(x.target) if isinstance(n, ast.Name))
        elif isinstance(x, ast.withitem) and x.optional_vars is not None:
            out.update(n.id for n in ast.walk(x.optional_vars) if isinstance(n, ast.Name))
        elif isinstance(x, ast.ExceptHandler) and x.name:
            out.add(x.name)
        elif isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(x.name)
        elif isinstance(x, (ast.Import, ast.ImportFrom)):
            for a in x.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(x, ast.Lambda):
            for a in x.args.args:
                out.add(a.arg)
        elif isinstance(x, ast.NamedExpr) and isinstance(x.target, ast.Name):
            out.add(x.target.id)
        elif isinstance(x, ast.Global):
            out.update(x.names)
        elif isinstance(x, ast.Nonlocal):
            out.update(x.names)
    return out


def undefined_names(path):
    """按作用域栈递归检查：嵌套函数能读外层函数绑定的名字（闭包），
    不带作用域栈会把每个闭包都误报一遍。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__spec__"}
    module |= _bound_names(tree)
    bad = []

    def _direct_loads(node):
        """只看本函数体里的 Name 读取，不下钻进嵌套函数（它们自己递归查）。"""
        out = []
        stack = list(ast.iter_child_nodes(node))
        while stack:
            x = stack.pop()
            if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue                      # 嵌套定义由 _walk 单独处理
            if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load):
                out.append(x)
            stack.extend(ast.iter_child_nodes(x))
        return out

    def _descend(node, visible):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local = visible | _bound_names(node)
            for x in _direct_loads(node):
                if x.id not in local:
                    bad.append((node.name, x.id, x.lineno))
            for child in ast.iter_child_nodes(node):
                _descend(child, local)
        elif isinstance(node, ast.ClassDef):
            local = visible | _bound_names(node)
            for child in ast.iter_child_nodes(node):
                _descend(child, local)
        else:
            for child in ast.iter_child_nodes(node):
                _descend(child, visible)

    for child in ast.iter_child_nodes(tree):
        _descend(child, module)
    return bad


print("\n【函数体里引用不存在的名字（NameError 前哨）】")
for rel in TARGETS:
    path = _ROOT / rel
    if not path.exists():
        print(f"  ~ {rel:34} 跳过（文件不存在）")
        continue
    bad = undefined_names(path)
    ok = not bad
    if not ok:
        FAILS.append(rel)
    print(f"  {'✓' if ok else '✗'} {rel:34} " +
          ("无" if ok else "; ".join(f"{fn}() 用了 {nm} (行 {ln})" for fn, nm, ln in bad)))

print("\n" + ("FAILED: " + ", ".join(FAILS) if FAILS else "ALL PASS"))
sys.exit(1 if FAILS else 0)
