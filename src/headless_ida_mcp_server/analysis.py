# -*- coding: utf-8 -*-
"""Auto-analysis pump for idalib.

``idapro.open_database(path, False)`` returns in about a second even for a
7 MB library, but IDA's auto-analysis queue then needs minutes: measured on
``libcronet.so``, ``auto_wait()`` took 77 s and produced 21,609 functions.
Draining that queue inside ``set_binary_path`` blocks the event loop (FastMCP
calls synchronous tools on the loop thread), so every other tool call waits
with it and the client's per-tool timeout fires first.

This module drains the queue instead: one asyncio task stepping
``ida_auto.auto_make_step`` in bounded slices and yielding between them. The
pump must stay on the event-loop thread — idalib requires every IDA call on
the thread that initialised the library, so a worker thread would race the
tool calls rather than help.

Chunked stepping reaches the same end state as ``auto_wait()`` (verified on
``libcronet.so``: 8.8 M steps, 80 s, identical function count, 3.8 % overhead).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

# 2000 steps is ~18 ms at the measured 110k steps/s: long enough to make
# progress, short enough that a queued tool call or status poll is not starved.
SLICE_STEPS = 2000

# Status polls are cheap IDA reads; 50 ms keeps `wait_sec` responsive without
# spinning the loop.
POLL_SEC = 0.05

# `idle` -> `running` -> `done`; `stop()` ends a run as `cancelled`. A snapshot
# taken with no database open always reports `idle` — see `snapshot()`.
_state: str = "idle"
_task: "asyncio.Task[None] | None" = None
_started_at: float = 0.0
_elapsed_sec: float = 0.0
_steps: int = 0
_error: "str | None" = None


# IDA modules are imported lazily so `import headless_ida_mcp_server.analysis`
# works on hosts without idalib (the test suite imports the server on those).
def _ida_auto():
    import ida_auto

    return ida_auto


def _ida_funcs():
    import ida_funcs

    return ida_funcs


def _idaapi():
    import idaapi

    return idaapi


def _idc():
    import idc

    return idc


def _queues_empty() -> bool:
    try:
        return bool(_ida_auto().auto_is_ok())
    except Exception:
        # Without an initialised library the queue state is unknowable; the
        # caller sees `queues_empty: false` rather than a crashed tool.
        return False


def _function_count() -> int:
    try:
        return int(_ida_funcs().get_func_qty())
    except Exception:
        return 0


def _current_elapsed() -> float:
    if _state == "running" and _started_at:
        return time.monotonic() - _started_at
    return _elapsed_sec


async def _run() -> None:
    global _state, _steps, _elapsed_sec, _error
    try:
        idaapi = _idaapi()
        auto = _ida_auto()
        lo, hi = idaapi.inf_get_min_ea(), idaapi.inf_get_max_ea()
        while True:
            processed = False
            for _ in range(SLICE_STEPS):
                if not auto.auto_make_step(lo, hi):
                    break
                _steps += 1
                processed = True
            if not processed:
                break
            # Yield so tool calls and status polls interleave with the pump.
            await asyncio.sleep(0)
        # `auto_make_step` returning False only means nothing is left inside
        # [lo, hi]; the queue can still hold work elsewhere. Completion is
        # whatever `auto_is_ok()` says, never our own loop bookkeeping.
        if auto.auto_is_ok():
            _state = "done"
        else:
            _state = "cancelled"
            _error = "auto-analysis queue still holds work outside the analyzed range"
    except asyncio.CancelledError:
        _state = "cancelled"
        raise
    except Exception as exc:  # pragma: no cover - idalib failure
        # The pump is the driver; nobody awaits it for a result, so record the
        # failure in the snapshot instead of leaving an unretrieved exception.
        _state = "cancelled"
        _error = f"{type(exc).__name__}: {exc}"
    finally:
        _elapsed_sec = time.monotonic() - _started_at


def start() -> None:
    """Start the pump for the currently open IDB (no-op while already running).

    Must be called from the event-loop thread: it creates the task that owns
    every subsequent ``auto_make_step`` call.
    """
    global _state, _task, _started_at, _steps, _elapsed_sec, _error
    if _task is not None and not _task.done():
        return
    _state = "running"
    _started_at = time.monotonic()
    _elapsed_sec = 0.0
    _steps = 0
    _error = None
    _task = asyncio.create_task(_run())


async def stop() -> None:
    """Cancel the pump and wait for the task to unwind.

    Callers must await this before closing the database: a task still inside
    ``auto_make_step`` would touch a database that no longer exists.
    """
    global _state, _task
    task = _task
    _task = None
    if task is not None and not task.done():
        task.cancel()
        # `return_exceptions=True`: cancellation is expected here, and the
        # pump's own failures are already recorded in `_error`.
        await asyncio.gather(task, return_exceptions=True)
    _state = "cancelled"


async def wait(seconds: "float | None" = None) -> bool:
    """Wait for the analysis queue to drain; returns whether it is empty.

    ``seconds=None`` waits for the pump to finish, preserving the old blocking
    contract for ``set_binary_path(wait=True)``. A numeric budget polls until
    the queue is empty or the budget expires, so a caller can wait without
    risking a client-side tool timeout.
    """
    if seconds is None:
        task = _task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return _queues_empty()

    deadline = time.monotonic() + max(0.0, float(seconds))
    while not _queues_empty():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(POLL_SEC)
    return True


def snapshot() -> dict[str, Any]:
    """Return the current analysis state as a JSON-friendly dict."""
    idb_path = ""
    try:
        idb_path = _idc().get_idb_path()
    except Exception:
        idb_path = ""

    if not idb_path:
        # With no database open, the last run's outcome is no longer
        # observable (the queue it described is gone), so report `idle`.
        return {
            "state": "idle",
            "idb_path": None,
            "elapsed_sec": 0.0,
            "functions": 0,
            "queues_empty": True,
            "steps": 0,
            "error": None,
        }

    return {
        "state": _state,
        "idb_path": idb_path,
        "elapsed_sec": round(_current_elapsed(), 3),
        "functions": _function_count(),
        "queues_empty": _queues_empty(),
        "steps": _steps,
        "error": _error,
    }
