# 致谢

本项目基于以下工作：
- 工具代码改编自 mrexodia 的 [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp)
- idalib 重写基于 A1Lin 的 [headless-ida-mcp-server](https://github.com/A1Lin/headless-ida-mcp-server)
- 血统起源于 cnitlrt 的 [headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server)

# Headless IDA MCP Server

以 headless 方式运行 IDA Pro 分析后端，并以 MCP server 形式暴露其能力。
适用于从 CLI / agent / CI 驱动 IDA 的场景，而非作为交互式插件使用。

> English version see [README.md](./README.md).

## 特性

- **完整上游 MCP 工具面**。85 个 MCP tool + 11 个 MCP resource（其中 81
  个 vendored from
  [`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp)，4
  个 fork-only —— `set_binary_path` / `unset` / `py_eval` / `undo`），
  覆盖分析（`decompile` / `disasm` / `xrefs_to` / `callgraph` / ...）、
  memory、types、structures、modify、stack、search、sigmaker、debugger
  各类。随上游演进按需 ad-hoc resync。
- **Headless IDA 完全由 agent 驱动**。通过进程内 `idalib` SDK 跑 IDA
  后端 —— 没有 `idat` subprocess，没有 per-call spawn 开销。一次 MCP
  连接驱动一整段 agent session 跑同一个 IDB。rename / comments / type
  变更走 `idb_save` 持久化回 IDB。client 连上时 server 通过 `instructions`
  字段把 5 步 workflow primer 推给 agent，**agent 不用读任何外部文档就能
  发出第一个有效 tool call**。
- **Plugin 通过 `mcp_manifest.py` 暴露强类型 MCP tool**。在你的 plugin
  源码旁边放一个 `mcp_manifest.py`，server 用 `inspect.signature` 反射
  每个 handler 的签名生成 MCP JSON Schema，自动接入 capability tag、
  自动 undo（`kind:write`）、per-tool timeout。三条发现路径：pip entry
  point（`[project.entry-points."headless_ida_mcp.plugins"]`）、
  `~/.idapro/plugins/<name>/mcp_manifest.py`、以及
  `IDA_MCP_PLUGIN_PATHS` 列出的每个目录。最小例子：

  ```python
  # mcp_manifest.py
  def ping() -> dict:
      return {"ok": True}

  PLUGIN = {"name": "demo", "description": "Demo", "version": "0.1"}
  TOOLS  = [{"name": "ping", "handler": ping, "description": "ping",
             "tags": ["kind:read"]}]
  ```

  Agent 通过 `enable_plugin(name)` per-session 启用（server 自动发
  `notifications/tools/list_changed`，client 重新拉 `list_tools`）。
  无 manifest 的纯 Python plugin 仍可走 `IDA_MCP_PLUGIN_PATHS`
  + `py_eval`。
- **为 agent 自动化工作流打造**。专为 **unattended / 长跑 / 批量分析**
  优化：**不走 MCP elicitation**（server 永远不会中途打断 agent 找真人
  确认），没有前台/后台询问、没有确认对话框。失败统一返回
  `error: ...` 字符串而**不是抛进 MCP transport**，单个 tool 失败不会
  断连接、不会中止多步工作流。连一次，扔个目标给 agent，关掉终端走人。
- **能力分级 + 自动 undo**。每个 tool 标 `kind:read` / `kind:write` /
  `kind:unsafe`。`kind:write` 类 tool 在执行前自动建 `ida_undo` undo
  point，agent 写错只要一次 `undo()` 调用就能回滚 —— 不用重开 IDB。
  `kind:unsafe`（`patch` / `patch_asm` / `undefine` / `py_eval` /
  `unset`）opt out —— `ida_undo` 救不回。运维方启动时用
  `--exclude-tags`（或 `IDA_MCP_EXCLUDE_TAGS` env）选要不要砍：
  `kind:write,kind:unsafe` 跑严格只读批量分析、`kind:unsafe` 保留写
  但禁破坏性、`core::debug::*` 砍整套 `dbg_*` 调试面（没 debugger
  的部署）。详见
  [`docs/agent-quickstart.md`](./docs/agent-quickstart.md) §11。

## 快速开始（5 行）

```bash
# 不需要 clone 仓库，uvx 直接从 git 跑。
# `--with <wheel>` 把 IDA Pro 自带的 idapro wheel 注入 uvx 临时 venv。
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
uvx --python 3.12 \
    --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
    --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
    headless_ida_mcp_server
```

跑起来就完事。Server 起来了，IDB 加载了，最多 85 个 MCP tool + 11 个
resource 暴露完毕（实际数量取决于 `--exclude-tags` 过滤配置）。任意 MCP
client 接上即可分析。

MCP client 连上时 server 会通过 `instructions` 字段把 5 步 workflow + 错误
约定推到 agent 的 system context，**agent 不读 README 也能直接出 tool call**。

## 详细参考

每个 env / CLI flag、MCP client config snippet、85 个 tool 和 11 个
resource、能力分级 tag + `undo()`、plugin 加载机制、debugger 注意事项、
排错 —— 全在
**[docs/agent-quickstart.md](./docs/agent-quickstart.md)**。
5 行 quickstart 之外的事都在那。

## 架构

进程内 `idalib` SDK 跑 IDA 后端；FastMCP 把分析能力暴露成 85 个 MCP tool
和 11 个 MCP resource。工具层 vendored from
[`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp)，随上游
按需 ad-hoc resync。没有 `idat` subprocess、没有 per-call spawn 开销 ——
连一次 server，跑长 agent session 对同一个 IDB。

## 先决条件

- Python 3.12 或更高
- IDA Pro >= 9.3，已装 `idapro` Python wheel
  （[idalib 文档](https://docs.hex-rays.com/user-guide/idalib)）
- [`uv`](https://github.com/astral-sh/uv)（用于 `uvx`）

## 贡献

End user 按上面 5 行 uvx quickstart 走。**本节"贡献"只针对要 patch server
本身的人**。clone 本仓 + `uv sync`，按
[docs/agent-quickstart.md](./docs/agent-quickstart.md) 里的贡献者流程走。
PR 落 `v2` 分支；`main` 是稳定 promote 目标。

![](./images/pic.png)

![](./images/pic2.png)
