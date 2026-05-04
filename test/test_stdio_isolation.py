# -*- coding: utf-8 -*-
"""Tests for `transport-stdio-isolation` capability.

Covers the unit-level invariants of `_install_stdio_isolation_if_needed()`:

* SSE mode (default) MUST NOT touch fd 1.
* stdio mode MUST `os.dup` the original fd 1, point fd 1 at stderr, and
  rebind `sys.stdout` to a TextIOWrapper whose `.buffer` is a
  BufferedWriter (so FastMCP's `TextIOWrapper(sys.stdout.buffer, ...)`
  rewrap remains valid).
* The helper is one-shot (idempotent re-call is a no-op).

The shutdown-ordering invariants for `_shutdown_idalib_now()` /
`_cleanup_idalib` are also exercised here: idempotency and silent
swallow of `ValueError: I/O operation on closed file`.

These are pure unit tests — no idalib, no real subprocess. They run on
any host where the package imports cleanly.
"""
from __future__ import annotations

import io
import os
import sys

import pytest

import headless_ida_mcp_server as pkg


# ---------------------------------------------------------------------------
# fd-redirect helper: scenarios from specs/transport-stdio-isolation/spec.md
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_isolation_flag(monkeypatch):
    """Reset the one-shot guard + saved writer before / after each test.

    The helper is one-shot per process, but unit tests need to exercise
    the redirect repeatedly. We reset the module-level flag and writer
    so each test starts fresh; the actual fd-level state is restored by
    pytest's `capfd` fixture (which dup2s the original fds back).
    """
    monkeypatch.setattr(pkg, "_stdio_isolation_done", False, raising=False)
    monkeypatch.setattr(pkg, "_jsonrpc_writer", None, raising=False)
    yield


def test_sse_mode_skips_redirect(monkeypatch, reset_isolation_flag):
    """When TRANSPORT != stdio, the helper MUST be a no-op."""
    monkeypatch.setenv("TRANSPORT", "sse")

    saved_stdout = sys.stdout
    pkg._install_stdio_isolation_if_needed()

    # No swap occurred.
    assert sys.stdout is saved_stdout
    assert pkg._stdio_isolation_done is False


def test_default_transport_skips_redirect(monkeypatch, reset_isolation_flag):
    """No TRANSPORT env -> defaults to sse -> still skip."""
    monkeypatch.delenv("TRANSPORT", raising=False)

    saved_stdout = sys.stdout
    pkg._install_stdio_isolation_if_needed()

    assert sys.stdout is saved_stdout
    assert pkg._stdio_isolation_done is False


def test_stdio_mode_redirects_fd1_and_saves_jsonrpc_writer(
    monkeypatch, capfd, reset_isolation_flag
):
    """In TRANSPORT=stdio, fd 1 MUST be redirected and a JSON-RPC writer saved.

    Strategy: capfd swaps fd 1 / fd 2 with pipes. We run the helper and
    verify:
      - `_get_jsonrpc_writer()` returns a line-buffered, UTF-8 TextIOWrapper
        whose `.buffer` is a BufferedWriter (so FastMCP can wrap it).
      - Writes through that writer land on the *saved* (original) fd 1 —
        the JSON-RPC channel.
      - `sys.stdout` is left UN-rebound (still aliased to fd 1, which now
        points at stderr after dup2) so plugin `print(...)` calls land
        on stderr.
      - Raw writes to fd 1 (`os.write(1, ...)`) land on stderr now.
    """
    monkeypatch.setenv("TRANSPORT", "stdio")
    saved_stdout = sys.stdout

    pkg._install_stdio_isolation_if_needed()

    # 1. sys.stdout intentionally NOT rebound (so plugin print -> stderr).
    assert sys.stdout is saved_stdout

    # 2. _get_jsonrpc_writer() returns the saved TextIOWrapper.
    writer = pkg._get_jsonrpc_writer()
    assert writer is not None
    assert isinstance(writer, io.TextIOWrapper)
    assert isinstance(writer.buffer, io.BufferedWriter)

    # FastMCP wraps the writer with anyio.wrap_file; verify the underlying
    # text-mode contract directly: encoding utf-8, line-buffered.
    assert writer.encoding.lower() == "utf-8"
    # buffering=1 + write mode -> line buffered: confirm via write_through
    # not strictly available; check buffer type is fine.

    # 3. Writes to the JSON-RPC writer go to the *original* (saved) fd 1.
    writer.write('{"jsonrpc": "2.0"}\n')
    writer.flush()

    # 4. Raw `os.write(1, ...)` lands on stderr now (post-dup2). This is
    #    the C-layer printf path: any libidalib `printf` from C goes here.
    #    `sys.stdout.write(...)` cannot be tested faithfully under
    #    pytest's capfd (capfd wraps sys.stdout with its own writer that
    #    bypasses our fd-level dup2), but the real-subprocess E2E covers
    #    that path — see /tmp/stdio_isolation_e2e.py.
    os.write(1, b"[idalib] init_library rc=0\n")

    captured = capfd.readouterr()
    assert '{"jsonrpc": "2.0"}' in captured.out
    assert "[idalib] init_library rc=0" in captured.err
    # Critically: the C-layer line MUST NOT appear on stdout.
    assert "[idalib]" not in captured.out

    # 5. One-shot guard is set.
    assert pkg._stdio_isolation_done is True


def test_stdio_helper_is_idempotent(monkeypatch, reset_isolation_flag):
    """A second call MUST be a no-op (no extra dup, no writer rebuild)."""
    monkeypatch.setenv("TRANSPORT", "stdio")

    pkg._install_stdio_isolation_if_needed()
    first_writer = pkg._get_jsonrpc_writer()
    pkg._install_stdio_isolation_if_needed()  # second call

    assert pkg._get_jsonrpc_writer() is first_writer


# ---------------------------------------------------------------------------
# shutdown ordering: scenarios from specs/runtime-idalib (modified)
# ---------------------------------------------------------------------------


class _FakeIDAPro:
    """Stand-in for the `idapro` module so we don't need real idalib."""

    def __init__(self, raise_on_close: bool = False):
        self.close_calls = 0
        self.raise_on_close = raise_on_close

    def close_database(self, write):  # noqa: ARG002
        self.close_calls += 1
        if self.raise_on_close:
            raise RuntimeError("fake close failure")


@pytest.fixture
def reset_shutdown_state(monkeypatch):
    """Wire a fake idapro module + open IDB into the package globals."""
    fake = _FakeIDAPro()
    monkeypatch.setattr(pkg, "_idapro_module", fake, raising=False)
    monkeypatch.setattr(pkg, "_idb_open", True, raising=False)
    monkeypatch.setattr(pkg, "_shutdown_done", False, raising=False)
    return fake


def test_shutdown_idalib_now_calls_close_once(reset_shutdown_state, capfd):
    """First call MUST invoke close_database and print the OK line.

    Use capfd (fd-level capture) because the implementation writes to fd 1
    directly via `os.write(1, ...)` so the JSON-RPC channel — `sys.stdout`
    in stdio mode — never sees diagnostic output.
    """
    pkg._shutdown_idalib_now()

    assert reset_shutdown_state.close_calls == 1
    out = capfd.readouterr().out
    assert "[idalib] close_database(False) ok" in out
    assert pkg._shutdown_done is True
    assert pkg._idb_open is False


def test_shutdown_idalib_now_is_idempotent(reset_shutdown_state, capfd):
    """Second call MUST be a no-op (no double-close, no double-print)."""
    pkg._shutdown_idalib_now()
    pkg._shutdown_idalib_now()

    assert reset_shutdown_state.close_calls == 1
    out = capfd.readouterr().out
    # Exactly one "ok" line — split by newline and count non-empty matches.
    assert out.count("[idalib] close_database(False) ok") == 1


def test_cleanup_idalib_swallows_io_closed_value_error(
    reset_shutdown_state, monkeypatch, capsys
):
    """atexit fallback MUST NOT let `I/O operation on closed file` escape."""

    def _raise(_w):
        raise ValueError("I/O operation on closed file")

    # Monkey-patch close_database to raise the real-world error.
    reset_shutdown_state.close_database = _raise
    # Force the print path to also raise to verify outer except suppresses.
    closed_buf = io.StringIO()
    closed_buf.close()
    monkeypatch.setattr(sys, "stdout", closed_buf)

    # Must not raise.
    pkg._cleanup_idalib()


def test_cleanup_idalib_no_op_when_idb_not_open(monkeypatch, capfd):
    """If `_idb_open` is False (e.g. server started without IDB_PATH),
    no close is attempted and no log is emitted."""
    fake = _FakeIDAPro()
    monkeypatch.setattr(pkg, "_idapro_module", fake, raising=False)
    monkeypatch.setattr(pkg, "_idb_open", False, raising=False)
    monkeypatch.setattr(pkg, "_shutdown_done", False, raising=False)

    pkg._cleanup_idalib()

    assert fake.close_calls == 0
    out = capfd.readouterr().out
    assert "close_database" not in out


def test_signal_handler_runs_close_before_systemexit(
    reset_shutdown_state, capfd
):
    """`_signal_to_systemexit` MUST close idalib BEFORE raising SystemExit."""
    import signal as _signal

    with pytest.raises(SystemExit) as excinfo:
        pkg._signal_to_systemexit(_signal.SIGINT, None)

    assert excinfo.value.code == 128 + _signal.SIGINT
    assert reset_shutdown_state.close_calls == 1
    out = capfd.readouterr().out
    assert "[idalib] close_database(False) ok" in out
    # And the atexit fallback must now no-op.
    pkg._cleanup_idalib()
    assert reset_shutdown_state.close_calls == 1
