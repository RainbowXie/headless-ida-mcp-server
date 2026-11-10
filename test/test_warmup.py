# -*- coding: utf-8 -*-
"""Unit tests for the bounded auto-analysis wait in `server_warmup`.

`api_core` imports the IDA SDK modules at module load, so this file skips on
hosts without IDA. The test itself stubs the pump and the health payload, so it
never touches a real database.
"""
from __future__ import annotations

import os

import pytest


def _bootstrap_if_possible() -> bool:
    """Make `import api_core` work, or report that this host has no IDA."""
    try:
        import ida_auto  # noqa: F401

        return True
    except ImportError:
        pass

    install_dir = os.environ.get("IDA_INSTALL_DIR", "")
    if not install_dir:
        for candidate in ("/opt/ida-pro-9.3", "/opt/ida-pro-9.0"):
            if os.path.isdir(candidate):
                os.environ["IDA_INSTALL_DIR"] = candidate
                install_dir = candidate
                break
    if not install_dir or not any(
        os.path.isfile(os.path.join(install_dir, libname))
        for libname in ("libidalib.so", "libidalib.dylib", "idalib.dll")
    ):
        return False

    import headless_ida_mcp_server as pkg

    if pkg._idapro_module is None:
        pkg._bootstrap_idalib()
    return True


if not _bootstrap_if_possible():  # pragma: no cover - hosts without IDA
    pytest.skip("IDA python modules unavailable", allow_module_level=True)

from headless_ida_mcp_server.ida_mcp import api_core  # noqa: E402


@pytest.mark.asyncio
async def test_warmup_reports_incomplete_when_budget_expires(monkeypatch):
    # An unbounded `auto_wait()` is what tripped the client timeout; the tool
    # must return inside its budget and say the step did not complete.
    budgets: list[float] = []

    async def _wait(seconds):
        budgets.append(seconds)
        return False

    monkeypatch.setattr(api_core.analysis, "wait", _wait)
    monkeypatch.setattr(api_core, "_build_health_payload", lambda: {})

    out = await api_core.server_warmup(
        wait_auto_analysis=True,
        build_caches=False,
        init_hexrays=False,
        auto_wait_sec=1.5,
    )

    assert budgets == [1.5]
    assert out["ok"] is False
    assert out["steps"][0]["step"] == "auto_wait"
    assert out["steps"][0]["ok"] is False
    assert "1.5" in out["steps"][0]["error"]


@pytest.mark.asyncio
async def test_warmup_reports_completion_when_queue_drains(monkeypatch):
    async def _wait(seconds):
        return True

    monkeypatch.setattr(api_core.analysis, "wait", _wait)
    monkeypatch.setattr(api_core, "_build_health_payload", lambda: {})

    out = await api_core.server_warmup(
        wait_auto_analysis=True,
        build_caches=False,
        init_hexrays=False,
        auto_wait_sec=30,
    )

    assert out["ok"] is True
    assert out["steps"][0]["ok"] is True
    assert "error" not in out["steps"][0]
