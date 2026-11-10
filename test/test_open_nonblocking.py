# -*- coding: utf-8 -*-
"""idalib-bound tests for the non-blocking open contract.

These exercise the real `set_binary_path` / `unset` tools against a copy of the
small `test/heap/main` fixture, so the analysis queue is genuine. They skip when
idalib or the fixture is unavailable, and when another test module already holds
an open database (idalib allows one per process).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_BINARY = REPO_ROOT / "test" / "heap" / "main"


def _idalib_available() -> bool:
    install_dir = os.environ.get("IDA_INSTALL_DIR", "")
    if not install_dir:
        for candidate in ("/opt/ida-pro-9.3", "/opt/ida-pro-9.0"):
            if os.path.isdir(candidate):
                os.environ["IDA_INSTALL_DIR"] = candidate
                install_dir = candidate
                break
    if not install_dir or not os.path.isdir(install_dir):
        return False
    return any(
        os.path.isfile(os.path.join(install_dir, libname))
        for libname in ("libidalib.so", "libidalib.dylib", "idalib.dll")
    )


@pytest.fixture(scope="module")
def server_module():
    if not _idalib_available():
        pytest.skip("idalib not available on this host")
    if not SAMPLE_BINARY.exists():
        pytest.skip(f"sample binary missing: {SAMPLE_BINARY}")

    import headless_ida_mcp_server as pkg

    if pkg._idapro_module is None:
        pkg._bootstrap_idalib()
    if getattr(pkg, "_idb_open", False):
        pytest.skip("an IDB is already open in this process")

    from headless_ida_mcp_server import server

    yield server
    pkg._idb_open = False


@pytest.mark.asyncio
async def test_open_returns_before_analysis_finishes(server_module, tmp_path):
    from headless_ida_mcp_server import analysis

    binary = tmp_path / "sample"
    shutil.copy(SAMPLE_BINARY, binary)

    try:
        result = await server_module._set_binary_path_impl(str(binary))

        assert "analysis running" in result
        # The pump task has not been scheduled yet: `set_binary_path` returned
        # without draining the queue.
        snap = analysis.snapshot()
        assert snap["state"] == "running"
        assert snap["queues_empty"] is False
    finally:
        await server_module._unset_impl()


@pytest.mark.asyncio
async def test_wait_true_returns_an_analyzed_idb(server_module, tmp_path):
    from headless_ida_mcp_server import analysis

    binary = tmp_path / "sample"
    shutil.copy(SAMPLE_BINARY, binary)

    try:
        result = await server_module._set_binary_path_impl(str(binary), wait=True)

        assert "analysis finished" in result
        snap = analysis.snapshot()
        assert snap["state"] == "done"
        assert snap["queues_empty"] is True
        assert snap["functions"] > 0
    finally:
        await server_module._unset_impl()


@pytest.mark.asyncio
async def test_unset_stops_the_pump_and_reports_idle(server_module, tmp_path):
    from headless_ida_mcp_server import analysis

    binary = tmp_path / "sample"
    shutil.copy(SAMPLE_BINARY, binary)

    await server_module._set_binary_path_impl(str(binary))
    assert analysis.snapshot()["state"] == "running"

    result = await server_module._unset_impl()

    assert result == "Binary path unset"
    assert analysis.snapshot()["state"] == "idle"


@pytest.mark.asyncio
async def test_missing_binary_starts_no_pump(server_module):
    from headless_ida_mcp_server import analysis

    result = await server_module._set_binary_path_impl("/nonexistent/sample.bin")

    assert result == "Failed to set binary path"
    assert analysis.snapshot()["state"] == "idle"


@pytest.mark.asyncio
async def test_analysis_status_is_registered_and_idle(server_module):
    from headless_ida_mcp_server import analysis

    names = {t.name for t in await server_module.mcp.list_tools()}
    assert "analysis_status" in names

    assert (await server_module._analysis_status_impl())["state"] == "idle"
