# 致谢

本项目基于以下工作：
- 工具代码改编自 mrexodia 的 [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp)
- 使用了 DennyDai 的 [headless-ida](https://github.com/DennyDai/headless-ida) 库
- Fork 自 cnitlrt 的 [headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server)，并在此基础上继续开发

# Headless IDA MCP Server

以 headless 方式运行 IDA Pro 分析后端，并以 MCP server 形式暴露其能力。
适用于从 CLI / agent / CI 驱动 IDA 的场景，而非作为交互式插件使用。

> 英文版本见 [README.md](./README.md)。Agent 快速上手见
> [docs/agent-quickstart.md](./docs/agent-quickstart.md)。

## 快速开始

需要 IDA Pro >= 9.3，并装好其自带的 `idapro` Python wheel。然后两条路：

### 路径 A：`uvx`（推荐给最终用户 —— 不用 clone 源码）

```bash
# 装 IDA Pro 自带的 idapro wheel
uv pip install /opt/ida-pro-9.3/idapro-*.whl
py-activate-idalib

# 直接从 git 跑 server，不需要 clone
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
uvx --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
    headless_ida_mcp_server
```

`uvx` 会把 git checkout 缓存在 `~/.cache/uv/`，建一个隔离 venv，跑 entry
point。要 pin 版本，git URL 后接 `@<tag>` 或 `@<sha>`。

### 路径 B：clone 仓库（贡献 / 本地改动用）

```bash
# 1. 装 idapro wheel
uv pip install /opt/ida-pro-9.3/idapro-*.whl
py-activate-idalib

# 2. clone + sync
git clone https://github.com/RainbowXie/headless-ida-mcp-server.git
cd headless-ida-mcp-server
uv sync

# 3. 配置
cp .env_example .env
# 编辑 .env：设置 IDA_INSTALL_DIR、可选 IDB_PATH、IDA_MCP_PLUGIN_PATHS、PORT 等

# 4. 跑
uv run headless_ida_mcp_server
```

## 配置项

Server 启动时从环境变量（一般通过 `.env` 加载）和 CLI flag 读取配置。
**CLI flag 覆盖 env，env 覆盖默认值。** 未传 CLI flag 不会覆盖已有 env。

| Env | CLI flag | 是否必需 | 默认 | 用途 |
|---|---|---|---|---|
| `IDA_INSTALL_DIR` | `--ida-install-dir` | 是（或回退到 `IDA_PATH`） | — | IDA Pro 安装目录，例如 `/opt/ida-pro-9.3` |
| `IDA_PATH` | — | 已弃用 | — | v1 字段，指向 `idat` 二进制。若设此项但未设 `IDA_INSTALL_DIR`，server 会推断 `IDA_INSTALL_DIR = dirname(IDA_PATH)` 并发出 deprecation warning |
| `IDB_PATH` | `--idb-path` | 否 | （空） | 启动时自动加载的 IDB 文件。空时 agent 必须先调 `set_binary_path` |
| `IDA_MCP_PLUGIN_PATHS` | `--plugin-paths` | 否 | （空） | 冒号分隔的路径列表（PYTHONPATH 风格），idalib bootstrap 之后注入到 `sys.path[0]`，让 agent 通过 `py_eval` 调用 `import <plugin>`。空 / 未设 = 不注入。详见"加载 IDA 插件" |
| `PORT` | `--port` | 否 | `8888` | MCP server 监听端口 |
| `HOST` | `--host` | 否 | `0.0.0.0` | MCP server 监听地址 |
| `TRANSPORT` | `--transport` | 否 | `sse` | MCP 传输协议：`sse` 或 `stdio` |

`uv run headless_ida_mcp_server --help` 列出所有 flag、对应 env 名、默认值。
缺少 `IDA_INSTALL_DIR`（且无 `IDA_PATH` 回退）是致命启动错误：server 会在
import 阶段就抛 `ValueError("IDA_INSTALL_DIR is not set; ...")`，让失败立刻
暴露。

### CLI 示例

```bash
# 纯 env 驱动（用 .env）
uv run headless_ida_mcp_server

# CLI 覆盖端口（其余仍走 env）
uv run headless_ida_mcp_server --port 13337

# 切到 stdio 传输（适配偏好 stdio 的 MCP client）
uv run headless_ida_mcp_server --transport stdio
```

## MCP client 配置

把 server 加到 MCP client 配置里，支持两种传输。

### stdio（桌面 MCP client 推荐）

`uvx` 形式 —— 不需要 clone，agent runtime 直接从 git 起 server：

```json
{
  "mcpServers": {
    "ida": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/RainbowXie/headless-ida-mcp-server",
        "headless_ida_mcp_server",
        "--transport", "stdio"
      ],
      "env": {
        "IDA_INSTALL_DIR": "/opt/ida-pro-9.3",
        "IDB_PATH": "/path/to/sample.i64",
        "IDA_MCP_PLUGIN_PATHS": "/path/to/plugin-a:/path/to/plugin-b"
      }
    }
  }
}
```

源码 clone 形式 —— 已经 clone 仓库做本地改动时：

```json
{
  "mcpServers": {
    "ida": {
      "command": "/path/to/uv",
      "args": [
        "--directory", "/path/to/headless-ida-mcp-server",
        "run", "headless_ida_mcp_server",
        "--transport", "stdio"
      ],
      "env": {
        "IDA_INSTALL_DIR": "/opt/ida-pro-9.3",
        "IDB_PATH": "/path/to/sample.i64"
      }
    }
  }
}
```

### sse（HTTP）

外部启动 server，再让 MCP client 连接监听地址：

```bash
uv run headless_ida_mcp_server --transport sse --port 8888 --host 127.0.0.1
```

```json
{
  "mcpServers": {
    "ida": {
      "url": "http://127.0.0.1:8888/sse"
    }
  }
}
```

### 调试

用 MCP Inspector 交互式戳一下运行中的 server：

```bash
npx -y @modelcontextprotocol/inspector
```

## Tools 与 resources

Server 启动时注册 **84 个 MCP tool + 11 个 MCP resource**（3 个 fork 独有
lifecycle tool + 81 个 vendored 上游 tool + 11 个 vendored 上游 resource）：

| 分组 | 来源 | 示例 |
|---|---|---|
| Lifecycle | fork 独有 | `set_binary_path`、`unset`、`py_eval` |
| Core / metadata | `ida_mcp/api_core.py` | `server_health`、`lookup_funcs`、`list_funcs`、`imports`、`idb_save`、`find_regex`、`search_text` |
| Analysis | `ida_mcp/api_analysis.py` | `decompile`、`disasm`、`xrefs_to`、`callees`、`callgraph`、`find_bytes`、`basic_blocks` |
| Memory | `ida_mcp/api_memory.py` | `get_bytes`、`get_int`、`get_string`、`get_global_value`、`patch`、`put_int` |
| Types | `ida_mcp/api_types.py` | `declare_type`、`enum_upsert`、`read_struct`、`search_structs`、`set_type`、`infer_types` |
| Modify | `ida_mcp/api_modify.py` | `set_comments`、`append_comments`、`patch_asm`、`rename`、`define_func`、`define_code` |
| Stack | `ida_mcp/api_stack.py` | `stack_frame`、`declare_stack`、`delete_stack` |
| Debug（idalib 下尽力服务） | `ida_mcp/api_debug.py` | `dbg_start`、`dbg_continue`、`dbg_regs`、`dbg_bps`、`dbg_read`、`dbg_write` |
| Survey / Composite | `ida_mcp/api_survey.py` + `api_composite.py` | `survey_binary`、`analyze_function`、`analyze_component`、`diff_before_after`、`trace_data_flow` |
| Sigmaker | `ida_mcp/api_sigmaker.py` | `make_signature`、`make_signature_for_function`、`find_xref_signatures` |
| Resources（静态） | `ida_mcp/api_resources.py` | `ida://idb/metadata`、`ida://idb/segments`、`ida://idb/entrypoints`、`ida://cursor`、`ida://selection`、`ida://types`、`ida://structs` |
| Resources（模板） | `ida_mcp/api_resources.py` | `ida://struct/{name}`、`ida://import/{name}`、`ida://export/{name}`、`ida://xrefs/from/{addr}` |

`ida_mcp/` 子包从上游
[`mrexodia/ida-pro-mcp`](https://github.com/mrexodia/ida-pro-mcp) vendored
而来，随上游演进按需 ad-hoc resync。`api_discovery.py`（进程发现 / 多实例
管线）**不**纳入 vendored —— 在 idalib 下无意义。

### Debugger tools 是尽力服务

`dbg_*` 工具（如 `dbg_regs` / `dbg_step_into` / `dbg_bps`）从上游 vendored
不动，但 **idalib 不托管 live debugger session**。调用它们会返回
`error: Debugger not running`（或类似结构化错误）而不是把 server 搞崩。
仅当你已经通过 `dbg_start` 驱动了 idalib debugger 才用它们；多数工作流应
靠静态分析（`decompile` / `disasm` / `xrefs_to` 等）。

### 错误不会拖垮 MCP

任何失败的 tool 都返回以 `error: ...` 开头的字符串，而不是把异常抛进 MCP
传输（那样会断 client 连接）。Resources 同理返回 `{"error": "..."}`。
`set_binary_path` 必须先调（或 `IDB_PATH` 已设）才能用任何触碰 IDA 状态
的 tool —— 缺 binary 时返回
`error: Binary path not set (call set_binary_path first; tool=...)`。

## 加载 IDA 插件

大多数 IDA 插件以"丢进 IDA 的 plugins/ 目录"形式发布，而不是 pip 可装的
package。要让连到 MCP 的 agent 通过 `py_eval` 调用 `import <plugin>`，
server 提供了一个通用的 sys.path 注入机制 —— 把 `IDA_MCP_PLUGIN_PATHS`
（或 `--plugin-paths`）设为冒号分隔的插件 checkout 根路径列表。

启动时 server 做的事：

1. idalib 初始化完成后（`init_library` 成功，`import idapro` 通过），
   bootstrap 调用 `_inject_plugin_paths()`。
2. 从 env / CLI flag 读 `IDA_MCP_PLUGIN_PATHS`。空 / 未设是严格 no-op
   （无 log、无 warning、不动 `sys.path`）。
3. 用 `:` 切分（`PYTHONPATH` 风格）；空 token 丢弃。每个非空路径**前插**
   到 `sys.path`，按从左到右顺序：写
   `IDA_MCP_PLUGIN_PATHS=/a:/b:/c` 得到 `sys.path[0..2] = [/a, /b, /c]`。
4. stdout 每路径一行：
   `[plugin-paths] sys.path injected: <path> (exists: <bool>)`。
   若路径不存在，stderr 多一条
   `[plugin-paths] warning: <path> does not exist`，但 server 仍会启动 ——
   通用 IDA 工具仍可用。

**Invariant**：server 启动时**永远不会** `import <plugin>`。IDA 插件通常
有全局副作用（`register_action`、hook 安装等）；首次 import 是 agent 通过
`py_eval` 显式发起的，不是 server 私自的。

### 为什么前插 sys.path？

`sys.path.insert(0, path)` 让用户的 plugin checkout 优先级高于同名的旧
pip wheel —— 在 venv 里恰好装了同名 wheel 时，本地 checkout 仍能 shadow
掉它，方便插件作者本地开发。

### 示例

单个插件：

```bash
IDA_MCP_PLUGIN_PATHS=/path/to/your-plugin \
  uv run headless_ida_mcp_server
```

```python
# Agent 端伪代码：
mcp.call_tool("py_eval", {"code": "from your_plugin import api; api.__file__"})
```

多插件（冒号分隔，左 = `sys.path` 上优先级最高）：

```bash
IDA_MCP_PLUGIN_PATHS=/path/to/plugin-a:/path/to/HexRaysCodeXplorer \
  uv run headless_ida_mcp_server
```

```python
mcp.call_tool("py_eval", {"code": "import plugin_a; plugin_a.__file__"})
mcp.call_tool("py_eval", {"code": "import HexRaysCodeXplorer"})
```

CLI flag 形式（等价于 env，接受相同的冒号分隔字符串）：

```bash
uv run headless_ida_mcp_server \
  --plugin-paths /path/to/plugin-a:/path/to/HexRaysCodeXplorer
```

如果你的插件本身可 `pip install`，根本不需要这个机制 —— `site-packages`
本就在 `sys.path` 上，`IDA_MCP_PLUGIN_PATHS` 留空即可。

## 架构说明

本 fork 维护两条执行线：

- **v1**：原始实现，fork 自
  [cnitlrt/headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server)，
  通过 `headless_ida` 库每次调用都 spawn `idat`。在此基础上加了异步支持。
- **v2**（当前默认）：基于进程内 `idalib` SDK 重写所有 helper。去掉
  `headless_ida` 依赖和每次调用都启动 `idat` 的开销，工具时延显著改善。

`IDA_INSTALL_DIR`（替代旧的 `IDA_PATH`）驱动两条线：v1 用它定位 `idat`，
v2 把它交给 idalib 激活。idalib bootstrap 实际逻辑 —— `idapro.open_database`
/ `close_database`、生命周期管理、`IDA_PRO_TIMEOUT` 旋钮 —— 在
`add-idalib-bootstrap` 提交里实现，本 README 主体配置范围**不涉及**。

## 先决条件

- Python 3.12 或更高
- IDA Pro >= 9.0，已装 `idapro` Python wheel
  （[idalib 文档](https://docs.hex-rays.com/user-guide/idalib)）
- 仅 v1 需要：`headless_ida` 和可达的 `idat` 二进制
  （[DennyDai/headless-ida](https://github.com/DennyDai/headless-ida)）

## 安装

1. 本地 clone 项目：

   ```bash
   git clone https://github.com/A1Lin/headless-ida-mcp-server.git
   cd headless-ida-mcp-server
   git checkout v1   # 或 v2
   ```

2. 装依赖：

   ```bash
   uv python install 3.12
   uv venv --python 3.12
   uv pip install -e .
   ```

3. 接着按上文 [快速开始](#快速开始) 走。

![](./images/pic.png)

![](./images/pic2.png)
