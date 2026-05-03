# 致谢

本项目基于以下工作：
- 工具代码改编自 mrexodia 的 [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp)
- idalib 重写基于 A1Lin 的 [headless-ida-mcp-server](https://github.com/A1Lin/headless-ida-mcp-server)
- 血统起源于 cnitlrt 的 [headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server)

# Headless IDA MCP Server

以 headless 方式运行 IDA Pro 分析后端，并以 MCP server 形式暴露其能力。
适用于从 CLI / agent / CI 驱动 IDA 的场景，而非作为交互式插件使用。

> English version see [README.md](./README.md).

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

跑起来就完事。Server 起来了，IDB 加载了，84 个 MCP tool + 11 个 resource
暴露完毕。任意 MCP client 接上即可分析。

MCP client 连上时 server 会通过 `instructions` 字段把 5 步 workflow + 错误
约定推到 agent 的 system context，**agent 不读 README 也能直接出 tool call**。

## 详细参考

每个 env / CLI flag、MCP client config snippet、84 个 tool 和 11 个
resource、plugin 加载机制、debugger 注意事项、排错 —— 全在
**[docs/agent-quickstart.md](./docs/agent-quickstart.md)**。
5 行 quickstart 之外的事都在那。

## 架构

进程内 `idalib` SDK 跑 IDA 后端；FastMCP 把分析能力暴露成 84 个 MCP tool
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
