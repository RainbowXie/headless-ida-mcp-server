# 致谢

本项目基于以下工作：
- 工具代码改编自 mrexodia 的 [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp)
- 使用了 DennyDai 的 [headless-ida](https://github.com/DennyDai/headless-ida) 库
- Fork 自 cnitlrt 的 [headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server)，并在此基础上继续开发

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

## 详细参考

每个 env / CLI flag、MCP client config snippet、84 个 tool 和 11 个
resource、plugin 加载机制、debugger 注意事项、排错 —— 全在
**[docs/agent-quickstart.md](./docs/agent-quickstart.md)**。
5 行 quickstart 之外的事都在那。

## 架构说明

本 fork 维护两条执行线：

- **v1**：原始实现，fork 自
  [cnitlrt/headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server)，
  通过 `headless_ida` 库每次调用都 spawn `idat`。在此基础上加了异步支持。
- **v2**（当前默认）：基于进程内 `idalib` SDK 重写所有 helper。去掉
  `headless_ida` 依赖和每次调用都启动 `idat` 的开销，工具时延显著改善。

`IDA_INSTALL_DIR`（替代旧的 `IDA_PATH`）驱动两条线：v1 用它定位 `idat`，
v2 把它交给 idalib 激活。

## 先决条件

- Python 3.12 或更高
- IDA Pro >= 9.3，已装 `idapro` Python wheel
  （[idalib 文档](https://docs.hex-rays.com/user-guide/idalib)）
- [`uv`](https://github.com/astral-sh/uv)（用于 `uvx`）
- 仅 v1 需要：`headless_ida` 和可达的 `idat` 二进制
  （[DennyDai/headless-ida](https://github.com/DennyDai/headless-ida)）

## 贡献者

要提 patch？clone 本仓 + `uv sync`，按
[docs/agent-quickstart.md](./docs/agent-quickstart.md) 里的贡献者流程走。
PR 落 `v2` 分支；`main` 是稳定 promote 目标。

![](./images/pic.png)

![](./images/pic2.png)
