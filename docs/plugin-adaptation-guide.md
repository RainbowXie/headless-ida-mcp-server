# 把一个 IDA plugin 改造成 agent 能用的形态

这篇文章的对象是「我已经写了一个 IDA plugin，怎么让它接到 fork 的 plugin contract，让 agent 能直接调」。它不是讲 contract spec 长什么样 —— spec 在 `docs/agent-quickstart.md` §12 + `openspec/specs/mcp-plugin-contract/spec.md` 已经写完整了。这篇讲**改造路径**：原来的 plugin 是什么形态，改造后是什么形态，中间每一步具体动哪些代码、躲哪些坑。

参考实现是 d810pk（`/mnt/data/Work/Projects/d810pk`）—— 它是 fork plugin contract 的第一个 reference 落地，能拿来当蓝本对照。但这篇也会同时记录 d810pk 改造时遇到的、还没解的限制（Qt/PyQt5、debugpy、IDA API 主线程要求），让后来人不要踩同样的坑。

## 为什么必须改造，不能"原 plugin 直接挂上"

写这套机制的最直接动因是：agent 第一次连上 fork server 看到 85+ 个 built-in tool，但看不到任何 plugin 暴露的能力。fork 内部的 IDA wrapper（decompile、disasm、xrefs_to 这些）是 fork 自己注册到 FastMCP 的，agent 通过 `list_tools` 就能拿到 typed schema 直接调。但 plugin 的代码不在 fork 进程视野里，更不用说有 typed 的入口。

之前 fork 提供过一条妥协路径：用 `IDA_MCP_PLUGIN_PATHS` env 把 plugin 的 checkout 根目录注入 `sys.path[0]`，agent 通过 `py_eval(code="from <plugin> import api; api.foo(...)")` 调用。这条路两个问题：

1. agent 必须先**知道** plugin 名 + module 路径 + 函数签名。这些 fork 都不告诉 agent，agent 要么读源码，要么靠 system prompt 写死。每次换 plugin 都要重做 onboarding。
2. agent 调用要写一段 Python 代码塞进 `py_eval(code=...)`。错率高，丢失 typed schema 验证，IDE 提示也用不上。

更深一层的问题是：**普通 IDA plugin 是反应式形态**。一个标准 IDA plugin 长这样：

```python
class UnflattenAction(idaapi.action_handler_t):
    def activate(self, ctx):
        ea = idaapi.get_screen_ea()
        # 一大段业务逻辑：分析当前函数、装 hook、跑 microcode 优化、刷新 pseudocode...
        self._do_unflatten(ea)
        return 1

    def update(self, ctx):
        return idaapi.AST_ENABLE_FOR_WIDGET
```

入口是 `activate()`，触发源是用户点 GUI 菜单。业务逻辑写在 `activate()` 里面，没有独立 module-level callable。即使 agent 通过 `py_eval` 进了 plugin 的进程空间，也调不动 —— agent 没办法 simulate "用户点了菜单"。

→ 所以 contract 不是把 plugin 简单"挂上"，而是要求 plugin 先**重构**成能被代码调用的形态：业务逻辑沉到 module-level pure function，GUI handler 退化成 thin wrapper，然后用 `mcp_manifest.py` 把这些 function 挂出去。fork 拿到 manifest 后用 `inspect.signature` 反射成 typed MCP tool，agent 就能 `list_tools` 看到 + 调用。

这步改造不是 fork 强加的麻烦 —— 即使不是为了 agent，把业务跟 UI 解耦本来就是好事（业务逻辑可单测、可被 batch CLI 复用、可在 idalib 模式跑），只是 agent 的需求让它从"应该做"变成"必须做"。

## 改造前后形态对比

最直观的对比是 d810pk 自己。原来 d810pk 的"标记 dispatcher 块"功能是这样的（伪代码，对应 `d810pk/ida_ui.py`）：

```python
class MarkDispatcherAction(idaapi.action_handler_t):
    def activate(self, ctx):
        ea = idaapi.get_screen_ea()
        state = D810PKState.instance()  # 从 GUI singleton 拿状态
        if not state.is_running:
            ida_kernwin.warning("d810pk not running, click Start first")
            return 0
        try:
            state.mark_dispatcher(ea)
            ida_kernwin.refresh_idaview_anyway()
        except Exception as e:
            ida_kernwin.warning(f"failed: {e}")
        return 1
```

这段代码 agent 完全用不上：调不到 `activate`、调到了也不知道传 `ctx` 是什么、还嵌着 `ida_kernwin.warning` 弹窗（idalib 模式没 GUI，会挂）。

改造后形态是这样的（对应 `d810pk/api.py:471-518` 的 `mark_dispatcher`）：

```python
def mark_dispatcher(ea: int) -> dict:
    """Mark `ea` as a dispatcher block.

    GUI equivalent: 'Mark dispatcher' context menu in IDA.
    Returns {status, ea, function_ea} or {status: 'failed', error}.
    """
    state = _get_state()
    if state is None or not state.is_running:
        return {"status": "failed", "error": "d810pk not running. Call api.start() first."}
    import ida_funcs  # lazy import 函数体内
    fn = ida_funcs.get_func(ea)
    if fn is None:
        return {"status": "failed", "error": f"no function at {ea:#x}"}
    try:
        state.mark_dispatcher(ea)
        return {"status": "ok", "ea": ea, "function_ea": fn.start_ea}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
```

然后 GUI handler 退化成调这个 function：

```python
class MarkDispatcherAction(idaapi.action_handler_t):
    def activate(self, ctx):
        from d810pk import api
        ea = idaapi.get_screen_ea()
        result = api.mark_dispatcher(ea)
        if result["status"] == "ok":
            ida_kernwin.refresh_idaview_anyway()
        else:
            ida_kernwin.warning(f"failed: {result['error']}")
        return 1
```

这层抽离之后两件事自动成立：

- `api.mark_dispatcher(ea)` 现在是个**module-level callable**，签名 typed，return shape 明确。agent 通过 `py_eval` 已经能调（虽然还是要写 `from d810pk import api; api.mark_dispatcher(0x12345)` 这种 Python 代码）。
- 业务逻辑跟 GUI 完全解耦。GUI 失败弹窗、单测调直接 mock IDA 模块、batch CLI 调写 for loop 跑一堆地址 —— 都不会再撞到 `idaapi.get_screen_ea` / `ida_kernwin.warning` 这些 GUI-only 东西。

但这还不够 —— agent 仍然要靠 `py_eval` 自摸函数名 / 签名。再往前一步是 **manifest**：写一个 `d810pk/mcp_manifest.py` 把 12 个 api function 全部声明出来：

```python
from d810pk import __version__, api

PLUGIN = {
    "name": "d810pk",
    "description": "Control flow unflattening for ollvm-style obfuscated binaries.",
    "version": __version__,
    "categories": ["lifecycle", "projects", "marks", "decompile", "scan", "hot-reload"],
}

TOOLS = [
    {
        "name": "mark_dispatcher",
        "handler": api.mark_dispatcher,
        "description": "Mark `ea` as a dispatcher block. ...",
        "tags": ["kind:write"],
    },
    # ... 11 more entries
]
```

Fork 启动期通过 `importlib.metadata.entry_points(group="headless_ida_mcp.plugins")` 发现这个 manifest（如果走 pip 装），或扫 `~/.idapro/plugins/` + `IDA_MCP_PLUGIN_PATHS` 列出的目录找 `mcp_manifest.py`（Path B），import 它，对每个 TOOLS entry 用 `inspect.signature(handler)` 反射出 MCP JSON Schema，注册到 fork 的 plugin-private dispatch table。

agent 视角下整个路径变成：

```
list_tools                            # 默认 87（85 fork + 4 meta），不含 plugin tool
plugins()                             # → [{name: "d810pk", description, version, ...}]
plugin_tools("d810pk")                # → 12 个 prefixed tool 的 schema
enable_plugin("d810pk")               # → 注册到当前 session + 发 tools/list_changed 通知
list_tools                            # 现在含 d810pk__mark_dispatcher 等 12 个
call_tool("d810pk__mark_dispatcher",  # typed 调用，跟普通 fork tool 没区别
          {"ea": 0x12345})
```

完全没有 `py_eval`。

## 改造步骤逐条讲

下面是把一个普通 IDA plugin 改到能用 contract 的状态需要走的 7 步。每步都对应实际代码动点和容易踩的坑。

### Step 1 — 把业务逻辑从 GUI handler 抽到 module-level function

这是整个改造的根。GUI handler 的 `activate()` / `update()` 里写的业务必须**完整**搬到独立 module 的独立 function 里去，参数显式、返回值结构化、不依赖 `idaapi.get_screen_ea` 这种"问 GUI 当前在哪"的 API。

实际操作就是把 `ctx`-driven 的 imperative 代码翻译成 `function(args) -> dict`。你需要拆出来的东西通常是：

- 当前光标地址 `idaapi.get_screen_ea()` → 改成参数 `ea: int`
- 当前函数 `idaapi.get_func(get_screen_ea())` → 调用方传 `ea` 进来
- "选中范围" `read_selection()` → 改成 `start: int, end: int` 双参数
- GUI 弹窗 `ida_kernwin.warning(msg)` → 删掉，业务函数返回 `{"status": "failed", "error": msg}` 让上层决定怎么呈现

抽完之后 GUI handler 应当变成这样：

```python
class MyAction(idaapi.action_handler_t):
    def activate(self, ctx):
        ea = idaapi.get_screen_ea()
        result = my_module.api.do_thing(ea)        # ← 这里调抽出来的 function
        # 把 result 翻译成 GUI 反馈（refresh / warning 弹窗）
        return 1
```

如果你的 plugin 有 5 个 GUI button，那就抽 5 个 function。每个 function 自包含。

**坑 1**：抽出来的 function 不要在顶层 import IDA module。改在函数体内 lazy import：

```python
def do_thing(ea: int) -> dict:
    import ida_funcs, ida_name              # ← 函数体内 import
    ...
```

为什么：fork 启动期会**先 import manifest module 做 schema 反射**，那时 idalib 还可能没 bootstrap 完，顶层 `import idaapi` 会抛 `ModuleNotFoundError`。即使 bootstrap 完了，单测时（`pytest tests/test_mcp_manifest.py`）也希望 manifest 在没有 IDA 的环境下能 import 成功，纯做 schema 校验。

**坑 2**：抽出来的 function 不要返回 IDA SDK 对象（`func_t` / `mop_t` / `minsn_t`）。这些对象生命周期跟 IDA database open 状态绑定，序列化也没法序列化。返回**纯数据 dict**：address 用 hex string 或 int、name 用 str、状态用 `{"status": "ok"|"failed", ...}` 约定。

### Step 2 — 写 typed facade module（推荐放 `<plugin>/api.py`）

把 Step 1 抽出来的 function 集中放一个 module 里。d810pk 的做法是 `d810pk/api.py`，包含 12 个 function 一共 1500+ 行。这个 module 的 docstring 顶上声明它的角色：

```python
"""<plugin>.api — typed Python facade for headless / agent (idalib) usage.

This module is the GUI-decoupled entry point. It produces internal state
equivalent to the GUI path, but never imports PyQt or .ida_ui.
"""
```

每个 function 用 `typing.Annotated` 给参数加描述，让 fork 反射出来的 schema 带 description（agent 看到的 `inputSchema` 才有用）：

```python
from typing import Annotated, Optional

def mark_dispatcher(
    ea: Annotated[int, "Effective address of the dispatcher block"],
) -> dict:
    """One-line summary of what this does for an agent caller."""
    ...
```

类型注解被 fork 的 `reflect_signature`（`src/headless_ida_mcp_server/plugins/__init__.py`）按这张表转成 JSON Schema：

| Python type | JSON Schema |
|---|---|
| `int` | `{"type": "integer"}` |
| `float` | `{"type": "number"}` |
| `str` | `{"type": "string"}` |
| `bool` | `{"type": "boolean"}` |
| `list[T]` | `{"type": "array", "items": <T>}` |
| `dict` / `dict[str, T]` | `{"type": "object"}` |
| `Optional[T]` / `T \| None` | `T` 的 schema + 不 required + default `null` |
| `Annotated[T, "doc"]` | `T` 的 schema + `description: "doc"` |
| 无 annotation | `{"type": "string"}`（保守 fallback） |

**坑 3**：复杂参数类型（联合类型、嵌套 dict）反射拿不到精确 schema，**显式在 manifest 写 `params` block** 覆盖反射结果。d810pk 的 `scan_indirect(scope=None)` 就是这种情况：`scope` 可以是 `None` / `int` / `dict[ea_start, ea_end]`，反射只能看到 default 是 None 没法表达全。manifest 里手动写：

```python
{
    "name": "scan_indirect",
    "handler": api.scan_indirect,
    "tags": ["kind:write"],
    "timeout": 300,
    "params": {
        "scope": {
            "type": "object",
            "required": False,
            "default": None,
            "description": "None for whole IDB / int ea / {ea_start, ea_end} half-open range",
        },
    },
},
```

显式 `params` 优先级高于反射，可以逐字段覆盖。

### Step 3 — 写 `mcp_manifest.py`

这个文件是 fork 跟 plugin 之间的契约面。它必须在 plugin package 顶层（跟 `__init__.py` / `api.py` 同级）。两个 module-level 对象：

`PLUGIN: dict` —— plugin 自己的元数据：

```python
PLUGIN = {
    "name": "d810pk",                    # required, ^[a-z][a-z0-9_]*$, 1-32 chars
    "description": "...",                # required, 给 agent 看
    "version": __version__,              # required, semver
    "categories": ["..."],               # optional, 信息性分组
}
```

`name` 是全局唯一的 plugin 标识。fork 启动期会检查所有发现的 plugin 名是否冲突，撞了直接 abort + 列出冲突的 manifest 路径。它也是 prefix 来源 —— `<plugin>__<short>` 构造时用的就是这个。

`version` 推荐从 `<plugin>/__init__.py` 的 `__version__` 拿（d810pk 是这条路），别在 manifest 里写死字符串。这样 plugin 升级版本号只改一处。

`TOOLS: list[dict]` —— 每条对应一个暴露给 agent 的 callable。最小字段集：

```python
{
    "name": "mark_dispatcher",         # short name, plugin 内唯一
    "handler": api.mark_dispatcher,    # 直接 callable，不是 string lookup
    "description": "...",              # required, 不允许 fallback 空
    "tags": ["kind:write"],            # required, 必须含 kind:read|write|unsafe 之一
}
```

可选字段：

```python
{
    ...
    "timeout": 120,                    # 秒，默认 30
    "params": {...},                   # 反射结果的 override
    "mcp": False,                      # 注册到 dispatch 但不进 list_tools
}
```

**坑 4**：`handler` 字段必须传**真 callable** 不是字符串。fork 是 in-process Python 不需要 string lookup（Ramune-ida 是 worker subprocess，所以那边走 string，跟我们这条路不一样）。直接 `api.mark_dispatcher` 就行。

**坑 5**：`description` **手写**，别偷懒 fallback handler 的 docstring 第一行。docstring 是写给 Python 开发者看的，描述什么时候 raise 什么、参数细节怎么用；description 是写给 agent 看的，应当覆盖 typical use case + return shape 的关键 key 名。这两个角度差很多，d810pk 12 条 description 都是手写的。

### Step 4 — 决定 tag

每个 tool 必须恰好一个 `kind:` tag。三档语义：

- `kind:read` —— 不改 IDB 状态。fork wrapper 不做任何额外动作，调用就是裸调。`get_status` / `list_marks` / `decompile`（即使内部跑 hexrays 优化，也是 query 性质）属于这档。
- `kind:write` —— 改 IDB metadata，**且** `ida_undo.perform_undo()` 能恢复。fork wrapper 在 dispatch 之前调一次 `ida_undo.create_undo_point("<plugin>__<tool>", "<plugin>__<tool>")`，agent 调 `undo()` 就能回滚。`mark_dispatcher` / `set_project` / `scan_indirect` 是这档。
- `kind:unsafe` —— 破坏性 / 不可逆。fork wrapper **不**自动建 undo point，因为 `ida_undo` 救不回来。raw byte patching、Python module hot-reload、关闭 IDB 等等都进这档。d810pk 的 `reload_modules` 是典型 —— `importlib.reload()` 改的是 process-wide module 状态，跟 IDA 数据库无关，`ida_undo` 完全管不到。

**坑 6**：千万别把 `reload_modules` 这种**进程级状态变更**标 `kind:write` —— 这会让 fork wrapper 装 undo point 给一个根本回滚不了的操作，agent 调 `undo()` 后会以为 reload 撤销了，但实际 module 已经被替换。语义错位很危险。规则是：**只有 IDB 数据库行为且 `ida_undo` 能恢复的才标 write**。其他都是 unsafe（除非纯只读）。

### Step 5 — 让 plugin 能被 fork discover

Fork 有三条 discovery 路径，按优先级：

**Path A — pip entry_points**（推荐）

在 `pyproject.toml` 加：

```toml
[project.entry-points."headless_ida_mcp.plugins"]
d810pk = "d810pk.mcp_manifest"
```

`pip install -e .`（或 wheel install）之后 fork 启动期跑 `importlib.metadata.entry_points(group="headless_ida_mcp.plugins")` 就能拿到。这条路最稳，跟当前 cwd / sys.path 无关。

**Path B — 目录扫描**

Fork 扫 `~/.idapro/plugins/*` 一层（IDA 标准 plugin 路径）+ `IDA_MCP_PLUGIN_PATHS` env 列出的每个路径，每个目录里看有没有 `<pkg_name>/mcp_manifest.py`。找到就把 parent dir 加进 `sys.path[0]`，然后 `import <pkg_name>.mcp_manifest`。

`IDA_MCP_PLUGIN_PATHS` 是冒号分隔（PYTHONPATH 风格）。比如：

```bash
IDA_MCP_PLUGIN_PATHS=/home/me/checkout/d810pk:/home/me/another-plugin
```

→ fork 扫 `/home/me/checkout/d810pk/<x>/mcp_manifest.py` 和 `/home/me/another-plugin/<x>/mcp_manifest.py`。注意 env 给的是 plugin checkout 的**父目录**，不是 plugin 包目录本身。

**Path A + Path B 同 plugin name**：Path A 优先，Path B 静默跳过 + 一行 INFO log 说明。这条规则在 `src/headless_ida_mcp_server/plugins/discovery.py` 里实现。

**坑 7**：Path B 要求 plugin 是个 Python **package**（有 `__init__.py`），不是单个 `.py` 文件。如果你的 plugin 现在只是 `~/.idapro/plugins/myplugin.py` 单文件，要重构成目录形态：

```
~/.idapro/plugins/
└── myplugin/
    ├── __init__.py        # 暴露 PLUGIN_ENTRY 给 IDA auto-load
    ├── mcp_manifest.py    # fork 找的契约
    ├── api.py             # Step 2 的 typed facade
    └── ida_ui.py          # 原来的 GUI handler
```

`__init__.py` 里的 `PLUGIN_ENTRY` 给 IDA 用（auto-load 时跑），`mcp_manifest.py` 给 fork 用 —— 两条路并行不冲突。

### Step 6 — Lazy IDA import 是硬性要求

这条单独拎出来强调因为踩坑率 100%。

`mcp_manifest.py` 顶层**绝对不能** import `idaapi` / `idc` / `ida_*` 任何 IDA module，**也不能**间接 import 它们。Fork 在 startup 时 import manifest module 做 schema 反射，这个时机比 idalib bootstrap 早。

最容易疏忽的间接 import 是这种：

```python
# d810pk/mcp_manifest.py (BAD)
from d810pk.api import (
    start, stop, mark_dispatcher, ...
)
```

如果 `d810pk/api.py` 顶层 import 了 `idaapi`，那 manifest import 时就连带触发 `idaapi` import → 挂。

正确做法是 `api.py` 把 IDA import 全部下放到函数体内：

```python
# d810pk/api.py (GOOD)
from typing import Annotated, Optional

def mark_dispatcher(ea: Annotated[int, "..."]) -> dict:
    import ida_funcs                       # ← 在函数体内，调用时才 import
    fn = ida_funcs.get_func(ea)
    ...
```

manifest 里这样 import api module 就不会出问题（顶层只触碰 `from typing import ...`）。

验证 lazy 是否做对：在 plugin venv 里跑

```python
import sys
from d810pk import mcp_manifest
ida_modules = [m for m in sys.modules if m.startswith(("ida_", "idaapi", "idc", "idautils"))]
print(ida_modules)
```

输出必须是 `[]`。任何条目说明哪条路径泄漏了 IDA import。

### Step 7 — 写个最小测试 + 跑 fork discovery 验证

测试至少两层：

**第 1 层 — manifest 自包含合法**

```python
# tests/test_mcp_manifest.py
from <plugin> import mcp_manifest

def test_plugin_block():
    assert mcp_manifest.PLUGIN["name"] == "<plugin>"
    assert mcp_manifest.PLUGIN["description"]
    assert mcp_manifest.PLUGIN["version"]

def test_tools_count_and_shape():
    assert len(mcp_manifest.TOOLS) == <expected>
    for t in mcp_manifest.TOOLS:
        assert t["name"]
        assert callable(t["handler"])
        assert t["description"]
        kinds = [tag for tag in t["tags"] if tag.startswith("kind:")]
        assert len(kinds) == 1, f"{t['name']} must have exactly one kind:* tag"
```

这层测**完全不需要 idalib**。pytest 在普通 venv 跑就行。

**第 2 层 — fork discovery 端到端**

写一个脚本起 fork server（subprocess + stdio），用 mcp Python client 连，跑发现链路：

```python
import asyncio, json
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

async def main():
    params = StdioServerParameters(
        command=".../headless-ida-mcp-server/.venv/bin/python",
        args=["-m", "headless_ida_mcp_server"],
        env={
            "IDA_INSTALL_DIR": "/opt/ida-pro-9.3",
            "IDB_PATH": "/path/to/test.i64",
            "TRANSPORT": "stdio",
            "IDA_MCP_PLUGIN_PATHS": "/path/to/your-plugin/checkout-parent",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            r = await session.call_tool("plugins", {})
            print("plugins:", r.content[0].text)
            r = await session.call_tool("plugin_tools", {"name": "<your_plugin>"})
            print("tools:", r.content[0].text)
            r = await session.call_tool("enable_plugin", {"name": "<your_plugin>"})
            print("enabled:", r.content[0].text)
            r = await session.call_tool("<plugin>__<some_read_tool>", {})
            print("call:", r.content[0].text)

asyncio.run(main())
```

跑这个能确认 discovery → enable → call 全链路通。如果 plugins() 没列你的 plugin，问题在 discovery 路径（manifest 文件位置 / entry_point 配置 / 顶层 import 失败）；如果 plugin_tools 报 schema 错，问题在 manifest 字段 / 类型注解。

## 已知限制 —— 改造之外还要处理的事

下面这些限制不是 contract 设计的问题，是 fork + idalib + plugin 共存时的现实坑。d810pk 改造完仍卡在这几条上，所以单独讲。

### 限制 1：idalib 模式不能拉 PyQt5 / PySide6

idalib 是 IDA 的 "no GUI" SDK，启动后没有 Qt event loop，Qt module import 不上。任何 plugin 在 plugin auto-load 路径上拉 Qt 都会让 plugin init 报：

```
NotImplementedError: Can't import PySide6. Are you trying to use Qt without GUI?
```

d810pk 踩这个坑是因为 `reloader_backend.py:36` 写了：

```python
from PyQt5 import QtCore, QtWidgets, QtGui
```

而且这条 import 在 `start()` → `state.start_d810()` → `manager.reload_modules` → `reloader_backend` 的 call chain 上必然触发。结果 `d810pk__start` 通过 fork 调用就报错，hooks 装不上，后续 `d810pk__decompile` 全部返回 `not running`。

**怎么躲**：

- plugin 的 reload / hot-reload 路径**不要**碰 Qt module。reload Python module 跟 GUI widget 是两回事，没必要绑在一起
- 必须 reload Qt 的话，加 try/except + GUI-mode 检查：

```python
try:
    if idaapi.is_main_thread() and not idaapi.is_idalib():
        from PyQt5 import QtCore, QtWidgets, QtGui
        # rebuild Qt views
except (ImportError, NotImplementedError):
    pass  # idalib 模式下静默跳过
```

注意 `idaapi.is_idalib()` 是 IDA 9.3+ 才有的；老版本可以用 `os.environ.get("IDA_HEADLESS")` / 类似 hack 检测。

- 把 GUI 相关 reload 跟 backend reload 拆成两个函数，agent 调用的版本只 reload backend，不碰 Qt

### 限制 2：调试库（debugpy）强 import 一样会挂

类似 Qt 的问题。d810pk 的 `ida_ui.py:8` 写 `import debug_d810pk`，而 `debug_d810pk.py:1` 写 `import debugpy`。如果 fork venv 里没装 debugpy，整条 import 链断在这。

debug 库不该跟业务代码绑在 import chain 上。改成：

```python
# debug_d810pk.py
def attach_debugger():
    try:
        import debugpy
    except ImportError:
        return False
    debugpy.listen(5678)
    return True
```

按需调。`ida_ui.py` 顶层不要 `import debug_d810pk`。

### 限制 3：fork 当前 plugin wrapper 不自动跑 main thread 路由

这是 fork 自己的限制，不是 plugin 层面能解的。

idalib 没有 GUI thread，但 IDA 内部仍然把"main thread"作为 SDK 操作的合法点。某些 IDA API（`ida_kernwin.set_color`、`ida_bookmarks.add_bookmark`、`ida_kernwin.refresh_*`）在调用时会检查 thread context，发现不是主线程就抛：

```
RuntimeError: Function can be called from the main thread only
```

fork 的 plugin wrapper（`make_plugin_tool_wrapper` 在 `src/headless_ida_mcp_server/plugins/__init__.py:541`）当前是直接同步 dispatch，没自动包 `idaapi.execute_sync(MFF_WRITE)`。fork 自带的 vendored tool（`rename`、`set_type`）work 是因为它们用的 IDA API 不要求主线程，或者 mcp Python client 跑在主线程上凑巧绕过了。

**plugin 这边能做的**：handler 内部调有 main-thread 要求的 IDA API 时，自己包 `execute_sync`：

```python
def my_write_tool(ea: int) -> dict:
    import ida_kernwin, idaapi
    result = {"status": "pending"}

    def _do():
        ida_kernwin.set_color(ea, ida_kernwin.CIC_ITEM, 0xff0000)
        result["status"] = "ok"
        return 1

    idaapi.execute_sync(_do, idaapi.MFF_WRITE)
    return result
```

不太干净，但能 work。等 fork 后续 change 把 main-thread 路由集成到 wrapper 里，plugin 这边可以再清理。

如果 fork 这条 limitation 让你的 plugin 调用全部失败，**先往 fork 立一个 OpenSpec change**（`route-plugin-tools-through-ida-main-thread` 之类），把这层路由进 wrapper，比每个 plugin 自己包 `execute_sync` 干净得多。

### 限制 4：Singleton state 跟 multi-session 隔离

Fork 的 plugin contract 说 plugin enable 状态是 per-session 的。但 plugin 自己的内部 state（D810PKState 这种 singleton）是 per-process 的。两个 client 同时连 fork：

- A enable d810pk → 调 d810pk__start → 全局 D810PKState.is_running = True
- B enable d810pk → 调 d810pk__get_status → 看到 is_running=True（A 改的）

这不是 bug，是当前 contract 的 inherent 行为 —— fork in-process 一份 plugin module 一份全局 state，无法做 per-session 状态隔离。如果 plugin 跟 IDA 一样是单 IDB 单进程的，问题不大；要做 multi-tenant 必须重新设计。

## 验证流程串成一条线

最后给一份完整的 sanity check 顺序，照着一步步跑。

```bash
# Phase 1: manifest 在 IDA-free 环境能 import
cd <your-plugin>
python -c "
import sys
from <your_plugin> import mcp_manifest
print('PLUGIN:', mcp_manifest.PLUGIN['name'])
print('TOOLS:', len(mcp_manifest.TOOLS))
ida_leaked = [m for m in sys.modules if m.startswith(('ida_', 'idaapi', 'idc'))]
print('IDA leak:', ida_leaked)
"
# 期望：PLUGIN 名对、TOOLS 数对、IDA leak 是空 list

# Phase 2: pytest 通过
python -m pytest tests/test_mcp_manifest.py -v
# 期望：所有测试 PASS

# Phase 3: 给 fork venv 做 pip install
cd /path/to/fork
.venv/bin/pip install -e /path/to/your-plugin
# 或者用 Path B：导出 IDA_MCP_PLUGIN_PATHS env 跳过这步

# Phase 4: 起 fork server，确认 plugins() 列出 plugin
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
TRANSPORT=stdio \
.venv/bin/python -m headless_ida_mcp_server &
# 用 mcp Python client 连接，调 plugins() 看你的 plugin 在不在
# （或者直接看 server stderr 的启动 log："registered N plugin tools across K plugins"）

# Phase 5: 端到端调用
# 用 mcp client 走：
#   plugins() → plugin_tools(name) → enable_plugin(name)
#   → call_tool("<plugin>__<read_tool>", {}) 验证 read 路径
#   → call_tool("<plugin>__<write_tool>", {...}) 验证 write 路径
#   → undo() 验证 write 还原（如果 tag 是 kind:write）
#   → disable_plugin(name)

# Phase 6: 真业务路径 E2E
# 拿一个真 sample IDB，让 agent 跑一段实际工作流（mark → decompile → undo → list_marks）
# 看 plugin 内部 state 转换是不是符合预期
```

每一步过不去都对应**确定的**问题点：

- Phase 1 失败 → 顶层 IDA import 没清干净，回 Step 6 改 lazy import
- Phase 2 失败 → manifest schema 字段缺 / 类型不对，回 Step 3 + Step 4
- Phase 4 失败（plugins() 不列 plugin）→ discovery 路径错。检查 entry_point 配置 / Path B 路径是否正确指向 plugin checkout 的父目录
- Phase 5 失败（call 报 main thread only）→ 限制 3，handler 里包 `execute_sync`
- Phase 6 失败（plugin 内部 state 不对）→ 多半是 GUI handler 里还有业务逻辑没抽干净（Step 1 没做完）

走完 Phase 6，plugin 才算"真改造完"。fork 的 verify subagent 跑 happy-path discovery PASS 不等于业务路径 work —— 真业务 E2E 才是 done 的 gate（这条规则记在 `agent_mode_supervisor_log.md` + agent memory 里）。

## 收尾

整套 contract 真正解的问题不是"agent 怎么调 plugin"，而是"plugin 怎么把自己的能力**显性化**"。原来 plugin 的能力埋在 GUI handler 里、隐藏在用户操作流程里、纠缠在 Qt widget 生命周期里 —— 这些都不是 agent 能直接接管的形态。改造的过程其实是把这些隐性能力**结构化**：业务沉到 typed function、入口提到 manifest、依赖梳清楚。

完成之后副作用是：plugin 自己也变得更可测试、更可单独运维、更可被批量 CLI 复用。Agent 接入只是结构化之后顺手暴露出来的额外通道。

Fork 这条 contract 不是针对某种特殊 plugin 设计的，d810pk 只是它第一个真实落地。任何 IDA plugin —— 反编译辅助、签名识别、漏洞扫描、CFG 分析、协议解析 —— 走完这 7 步改造都能挂上来，难度跟 plugin 现有解耦程度成正比。已经把业务跟 GUI 分开的 plugin（比如某些有独立 CLI 工具的 reverse engineering 框架）改造成本很低，只要写一个 manifest 就行；耦合得深的（GUI handler 里塞业务逻辑、reload 走 Qt 那套）要先做内部解耦，那部分跟 contract 没关系，是 plugin 自身的工程债。

至于 d810pk 自己什么时候能完成解耦跑通真业务 E2E，看下一轮 fork + d810pk 的 OpenSpec change 怎么排（`route-plugin-tools-through-ida-main-thread` 跟 `decouple-d810pk-from-qt-and-debugpy` 是两个独立但相关的 change，前者落 fork，后者落 d810pk）。
