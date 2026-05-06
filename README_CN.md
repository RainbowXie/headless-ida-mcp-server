# Headless IDA MCP Server

Headless 跑 IDA Pro，让任意 MCP agent 驱动它。

> 基于 [`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp)（工具）、[A1Lin/headless-ida-mcp-server](https://github.com/A1Lin/headless-ida-mcp-server)（idalib 重写）、[cnitlrt/headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server)（血统起源）。
>
> English version see [README.md](./README.md).

## 是什么

进程内 `idalib` SDK 跑 IDA 后端，FastMCP 把它暴露成 **85 个 MCP tool + 11 个 resource**，第三方 plugin 通过 [`mcp_manifest.py`](./docs/plugin-adaptation-guide.md) 热挂载，capability tag 自动 undo，`instructions` primer 在 client 连上时推过去。专为 unattended 批量 agent 工作流，不是给交互式插件用的。

## 特性

- **完整工具面** —— 分析（`decompile` / `disasm` / `xrefs_to` / `callgraph` / ...）、memory、types、structures、modify、stack、search、sigmaker、debugger 各类。81 个 vendored from upstream + 4 个 fork-only（`set_binary_path` / `unset` / `py_eval` / `undo`）。随上游按需 resync。
- **进程内 idalib** —— 没有 `idat` subprocess，没有 per-call spawn 开销。一次 MCP 连接驱动一整段 agent session 跑同一个 IDB，写入走 `idb_save` 持久化。
- **Plugin 通过 `mcp_manifest.py` 暴露强类型 MCP tool** —— 在你的 plugin 源码旁边放一个 manifest，server 反射每个 handler 成 typed MCP tool，自动接 capability tag、auto-undo、per-tool timeout。三条发现路径：pip entry point、`~/.idapro/plugins/`、`IDA_MCP_PLUGIN_PATHS`。要把现存 IDA plugin 接进来，看 [`docs/plugin-adaptation-guide.md`](./docs/plugin-adaptation-guide.md)。**完整工作样例**：见 [`headless-ida-mcp-comment-helper`](https://github.com/RainbowXie/headless-ida-mcp-comment-helper) —— 完整 reference plugin，`pip install` 完用 MCP client 直接驱动。覆盖给函数 / 指令读 / 列 / 写 / 批量清 `[mcp]` 标记 comment 的 4 个 tool，三种 capability tier（read / write / unsafe）一应俱全。
- **能力分级 + 自动 undo** —— 每个 tool 标 `kind:read` / `kind:write` / `kind:unsafe`。`kind:write` 自动建 `ida_undo` undo point，agent 写错一次 `undo()` 就回滚。运维方用 `--exclude-tags 'kind:write,kind:unsafe'` 跑严格只读批量分析。
- **为 agent 自动化工作流打造** —— 不走 MCP elicitation，没有确认对话框。失败统一返回 `error: ...` 字符串而不是抛进 transport，单个 tool 失败不断连接也不停多步工作流。连一次，扔个目标，走人。

## 快速开始

```bash
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
uvx --python 3.12 \
    --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
    --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
    headless_ida_mcp_server
```

Server 起来，IDB 加载，工具暴露。任意 MCP client 接上即可分析。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/agent-quickstart.md`](./docs/agent-quickstart.md) | 完整参考：每个 env / CLI flag、MCP client 配置、tool 目录、能力分级 §11、plugin 契约 §12、排错 |
| [`docs/plugin-adaptation-guide.md`](./docs/plugin-adaptation-guide.md) | 现存 IDA plugin 适配契约：7 步具体改造路径、代码模板、已知限制 |

## 先决条件

- Python 3.12+
- IDA Pro ≥ 9.3，已装 `idapro` Python wheel（[idalib 文档](https://docs.hex-rays.com/user-guide/idalib)）
- [`uv`](https://github.com/astral-sh/uv)

## 贡献

clone、`uv sync`、按 [`docs/agent-quickstart.md`](./docs/agent-quickstart.md) 的贡献者流程走。PR 落 `v2`；`main` 是稳定 promote 目标。
