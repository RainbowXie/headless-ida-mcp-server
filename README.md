# Acknowledgments

This project builds upon the work of:
- Tools code adapted from [ida-pro-mcp](https://github.com/mrexodia/ida-pro-mcp) by mrexodia
- Utilizes the [headless-ida](https://github.com/DennyDai/headless-ida) library by DennyDai
- Fork and develop from [headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server) by cnitlrt

# Headless IDA MCP Server

Run an IDA Pro analysis backend headlessly and expose it as an MCP server.
Useful when you want to drive IDA from a CLI / agent / CI rather than as an
interactive plugin.

> 中文版本见 [README_CN.md](./README_CN.md)。

## Quick start (5 lines)

```bash
# Run the server straight from git, no clone needed.
# `--with <wheel>` injects IDA Pro's idapro wheel into the uvx-managed venv.
IDA_INSTALL_DIR=/opt/ida-pro-9.3 \
IDB_PATH=/path/to/sample.i64 \
uvx --python 3.12 \
    --with /opt/ida-pro-9.3/idalib/python/idapro-*.whl \
    --from git+https://github.com/RainbowXie/headless-ida-mcp-server \
    headless_ida_mcp_server
```

That's it. Server is up, IDB is loaded, 84 MCP tools and 11 resources are
exposed. Connect any MCP client and start analyzing.

## Full reference

The detailed reference — every env / CLI flag, MCP client config snippets,
all 84 tools and 11 resources, plugin loading, debugger caveats,
troubleshooting — lives in
**[docs/agent-quickstart.md](./docs/agent-quickstart.md)**. Read that for
anything beyond the 5-line quickstart above.

## Architecture notes

This fork tracks two execution lines:

- **v1**: original implementation forked from
  [cnitlrt/headless-ida-mcp-server](https://github.com/cnitlrt/headless-ida-mcp-server),
  using the `headless_ida` library to spawn `idat` per call. Async support
  added on top.
- **v2** (current default): rewrites all helpers in `helper.py` against the
  in-process `idalib` SDK. Removes the `headless_ida` dependency and the
  per-call `idat` startup, dramatically improving tool latency.

`IDA_INSTALL_DIR` (replacing the legacy `IDA_PATH`) drives both lines: v1
uses it to locate `idat`, v2 hands it to idalib activation.

## Prerequisites

- Python 3.12 or higher
- IDA Pro >= 9.3 with the `idapro` Python wheel
  ([idalib docs](https://docs.hex-rays.com/user-guide/idalib))
- [`uv`](https://github.com/astral-sh/uv) (for `uvx`)
- v1 only: `headless_ida` and an accessible `idat` binary
  ([DennyDai/headless-ida](https://github.com/DennyDai/headless-ida))

## Contributors

Contributing patches? Clone the repo, `uv sync`, and follow the contributor
flow in [docs/agent-quickstart.md](./docs/agent-quickstart.md). PRs land on
the `v2` branch; `main` is the stable promotion target.

![](./images/pic.png)

![](./images/pic2.png)
