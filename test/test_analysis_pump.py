# -*- coding: utf-8 -*-
"""Tests for the auto-analysis pump.

The pump must stay on the event-loop thread (idalib is single-threaded), so
these tests drive it through fake `ida_auto` / `idaapi` / `ida_funcs` / `idc`
objects and assert the observable contract: it makes progress without blocking
the loop, it reports completion only when the queue is actually empty, and a
bounded wait returns within its budget.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from headless_ida_mcp_server import analysis


class _FakeAuto:
    def __init__(self, steps: int) -> None:
        self.remaining = steps

    def auto_make_step(self, _lo: int, _hi: int) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def auto_is_ok(self) -> bool:
        return self.remaining <= 0


class _FakeFuncs:
    def __init__(self, count: int) -> None:
        self.count = count

    def get_func_qty(self) -> int:
        return self.count


class _FakeIdaapi:
    def inf_get_min_ea(self) -> int:
        return 0x1000

    def inf_get_max_ea(self) -> int:
        return 0x2000


class _FakeIdc:
    def __init__(self, idb_path: str) -> None:
        self.idb_path = idb_path

    def get_idb_path(self) -> str:
        return self.idb_path


@pytest.fixture(autouse=True)
def reset_pump():
    """Each test starts from a clean pump; leftovers are cancelled."""
    analysis._state = "idle"
    analysis._task = None
    analysis._started_at = 0.0
    analysis._elapsed_sec = 0.0
    analysis._steps = 0
    analysis._error = None
    yield
    task = analysis._task
    analysis._task = None
    if task is not None and not task.done():
        task.cancel()


def _install(monkeypatch, *, steps: int, funcs: int = 7, idb_path: str = "/tmp/x.i64"):
    fake_auto = _FakeAuto(steps)
    monkeypatch.setattr(analysis, "_ida_auto", lambda: fake_auto)
    monkeypatch.setattr(analysis, "_ida_funcs", lambda: _FakeFuncs(funcs))
    monkeypatch.setattr(analysis, "_idaapi", lambda: _FakeIdaapi())
    monkeypatch.setattr(analysis, "_idc", lambda: _FakeIdc(idb_path))
    return fake_auto


@pytest.mark.asyncio
async def test_pump_reaches_done_and_reports_steps(monkeypatch):
    _install(monkeypatch, steps=5000)

    analysis.start()
    assert analysis.snapshot()["state"] == "running"

    assert await analysis.wait(None) is True

    snap = analysis.snapshot()
    assert snap["state"] == "done"
    assert snap["steps"] == 5000
    assert snap["queues_empty"] is True
    assert snap["functions"] == 7


@pytest.mark.asyncio
async def test_pump_yields_to_the_event_loop(monkeypatch):
    # A large queue must not monopolise the loop: the ticker has to run while
    # the pump is still working.
    _install(monkeypatch, steps=analysis.SLICE_STEPS * 3)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        for _ in range(3):
            await asyncio.sleep(0)
            ticks += 1

    analysis.start()
    await ticker()
    assert ticks == 3
    assert analysis.snapshot()["state"] == "running"
    assert analysis._steps > 0

    await analysis.wait(None)


@pytest.mark.asyncio
async def test_stop_cancels_and_clears_the_task(monkeypatch):
    _install(monkeypatch, steps=analysis.SLICE_STEPS * 10)

    analysis.start()
    await analysis.stop()

    assert analysis._task is None
    assert analysis.snapshot()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_snapshot_reports_idle_without_a_database(monkeypatch):
    _install(monkeypatch, steps=0, idb_path="")

    snap = analysis.snapshot()

    assert snap == {
        "state": "idle",
        "idb_path": None,
        "elapsed_sec": 0.0,
        "functions": 0,
        "queues_empty": True,
        "steps": 0,
        "error": None,
    }


@pytest.mark.asyncio
async def test_bounded_wait_returns_within_budget(monkeypatch):
    # A wall-clock fake: the queue only drains after `duration`, so the budget
    # test is independent of how fast the CPU chews through fake steps.
    class _TimedAuto:
        def __init__(self, duration: float) -> None:
            self.deadline = time.monotonic() + duration

        def auto_make_step(self, _lo: int, _hi: int) -> bool:
            return time.monotonic() < self.deadline

        def auto_is_ok(self) -> bool:
            return time.monotonic() >= self.deadline

    fake_auto = _TimedAuto(1.0)
    monkeypatch.setattr(analysis, "_ida_auto", lambda: fake_auto)
    monkeypatch.setattr(analysis, "_ida_funcs", lambda: _FakeFuncs(7))
    monkeypatch.setattr(analysis, "_idaapi", lambda: _FakeIdaapi())
    monkeypatch.setattr(analysis, "_idc", lambda: _FakeIdc("/tmp/x.i64"))

    analysis.start()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    finished = await analysis.wait(0.1)
    elapsed = loop.time() - t0

    assert finished is False
    assert elapsed < 0.6
    assert analysis.snapshot()["state"] == "running"

    await analysis.stop()


@pytest.mark.asyncio
async def test_bounded_wait_returns_true_once_the_queue_drains(monkeypatch):
    _install(monkeypatch, steps=analysis.SLICE_STEPS * 2)

    analysis.start()

    assert await analysis.wait(5.0) is True
    assert analysis.snapshot()["state"] == "done"


@pytest.mark.asyncio
async def test_done_requires_an_empty_queue(monkeypatch):
    # `auto_make_step` can run out of in-range work while the queue still
    # holds addresses outside [lo, hi]. That is not completion.
    fake_auto = _install(monkeypatch, steps=0)
    fake_auto.remaining = 0
    fake_auto.auto_is_ok = lambda: False  # type: ignore[method-assign]

    analysis.start()
    await analysis.wait(None)

    snap = analysis.snapshot()
    assert snap["state"] == "cancelled"
    assert snap["error"] is not None
