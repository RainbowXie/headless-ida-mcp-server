# -*- coding: utf-8 -*-
"""Tests for stdio_stub session mode: CLI parsing, ephemeral ports, and
daemon child lifecycle (spawn tracking + cleanup)."""

import signal
import socket
import subprocess
from unittest.mock import MagicMock

import pytest

from headless_ida_mcp_server import stdio_stub
from headless_ida_mcp_server.stdio_stub import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    HeadlessIdaStub,
    _build_parser,
    _find_free_port,
    _resolve_endpoint,
)


def _fake_running_proc() -> MagicMock:
    """A Popen double that looks alive: poll() -> None, wait() succeeds."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.poll.return_value = None
    return proc


class TestCliParsing:
    def test_defaults_when_no_args(self):
        # Backward compatibility: no flags must mean 127.0.0.1:8392.
        args = _build_parser().parse_args([])
        assert args.session_mode is False
        assert args.port is None
        assert args.host == DEFAULT_HOST == "127.0.0.1"
        assert _resolve_endpoint(args) == ("127.0.0.1", 8392)

    def test_explicit_port_and_host(self):
        args = _build_parser().parse_args(["--port", "8395", "--host", "0.0.0.0"])
        assert _resolve_endpoint(args) == ("0.0.0.0", 8395)

    def test_short_port_flag(self):
        args = _build_parser().parse_args(["-p", "8396"])
        assert _resolve_endpoint(args) == ("127.0.0.1", 8396)

    def test_session_mode_and_port_mutually_exclusive(self):
        # argparse must reject the combo with exit code 2 (usage error),
        # matching the stub-cli-arguments spec scenario.
        with pytest.raises(SystemExit) as exc:
            _build_parser().parse_args(["--session-mode", "--port", "8395"])
        assert exc.value.code == 2

    def test_invalid_port_rejected(self):
        with pytest.raises(SystemExit) as exc:
            _build_parser().parse_args(["--port", "not-a-number"])
        assert exc.value.code == 2

    def test_session_mode_allocates_ephemeral_port(self):
        args = _build_parser().parse_args(["--session-mode"])
        host, port = _resolve_endpoint(args)
        assert host == "127.0.0.1"
        assert 0 < port < 65536

    def test_session_mode_with_custom_host(self):
        args = _build_parser().parse_args(["--session-mode", "--host", "0.0.0.0"])
        host, port = _resolve_endpoint(args)
        assert host == "0.0.0.0"
        assert 0 < port < 65536


class TestFindFreePort:
    def test_returns_still_bindable_port(self):
        # The daemon re-binds this exact port right after we close the probe
        # socket, so the returned port must actually be free.
        port = _find_free_port()
        assert 0 < port < 65536
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))

    def test_respects_host(self):
        port = _find_free_port("127.0.0.1")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))


class TestDynamicUrl:
    def test_default_url_uses_default_endpoint(self):
        stub = HeadlessIdaStub()
        assert stub._daemon_url == f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/mcp"

    def test_session_url_uses_ephemeral_port(self):
        # stub-session-isolation: the daemon target URL must reflect the
        # dynamically allocated port, not the historical fixed 8392.
        port = _find_free_port()
        stub = HeadlessIdaStub(port=port, session_mode=True)
        assert stub._daemon_url == f"http://127.0.0.1:{port}/mcp"


class TestSpawnTracking:
    def test_session_mode_tracks_child_and_stays_attached(self, monkeypatch):
        stub = HeadlessIdaStub(port=40000, session_mode=True)
        fake = _fake_running_proc()
        popen = MagicMock(return_value=fake)
        monkeypatch.setattr(stdio_stub.subprocess, "Popen", popen)

        stub._spawn_daemon()

        assert stub._daemon_proc is fake
        _, kwargs = popen.call_args
        # Session mode must NOT detach: the child belongs to this process
        # group so stub exit takes it down.
        assert kwargs["start_new_session"] is False
        env = kwargs["env"]
        assert env["PORT"] == "40000"
        assert env["HOST"] == "127.0.0.1"
        assert env["TRANSPORT"] == "streamable-http"

    def test_default_mode_detached_and_untracked(self, monkeypatch):
        # Non-session mode must preserve the historical warm-daemon behavior:
        # detached from the stub and never tracked for cleanup.
        stub = HeadlessIdaStub()
        fake = _fake_running_proc()
        popen = MagicMock(return_value=fake)
        monkeypatch.setattr(stdio_stub.subprocess, "Popen", popen)

        stub._spawn_daemon()

        assert stub._daemon_proc is None
        _, kwargs = popen.call_args
        assert kwargs["start_new_session"] is True


class TestCleanup:
    def test_terminate_sends_sigterm_and_clears_handle(self):
        stub = HeadlessIdaStub(session_mode=True)
        fake = _fake_running_proc()
        stub._daemon_proc = fake

        stub._terminate_tracked_daemon()

        fake.terminate.assert_called_once()
        fake.kill.assert_not_called()
        assert stub._daemon_proc is None

    def test_terminate_escalates_to_kill_on_timeout(self):
        stub = HeadlessIdaStub(session_mode=True)
        fake = _fake_running_proc()
        fake.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="daemon", timeout=5),
            None,
        ]
        stub._daemon_proc = fake

        stub._terminate_tracked_daemon()

        fake.terminate.assert_called_once()
        fake.kill.assert_called_once()

    def test_already_exited_child_is_not_signalled(self):
        stub = HeadlessIdaStub(session_mode=True)
        fake = MagicMock(spec=subprocess.Popen)
        fake.poll.return_value = 0
        stub._daemon_proc = fake

        stub._terminate_tracked_daemon()

        fake.terminate.assert_not_called()
        fake.kill.assert_not_called()

    def test_no_tracked_child_is_noop(self):
        stub = HeadlessIdaStub(session_mode=True)
        stub._terminate_tracked_daemon()  # must not raise

    def test_cleanup_is_idempotent(self):
        # atexit + finally + signal handler can all fire for one exit; the
        # second call must see an empty handle and do nothing.
        stub = HeadlessIdaStub(session_mode=True)
        fake = _fake_running_proc()
        stub._daemon_proc = fake

        stub._terminate_tracked_daemon()
        stub._terminate_tracked_daemon()

        fake.terminate.assert_called_once()


@pytest.fixture
def restore_signals():
    # _install_signal_handlers mutates process-global dispositions; pytest
    # must not be left with the stub's handlers installed.
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


class TestSignalHandlers:
    def test_sigterm_handler_cleans_up_and_reraises(self, monkeypatch, restore_signals):
        stub = HeadlessIdaStub(session_mode=True)
        stub._install_signal_handlers()
        fake = _fake_running_proc()
        stub._daemon_proc = fake
        killed: list[int] = []
        monkeypatch.setattr(stdio_stub.os, "kill", lambda _pid, sig: killed.append(sig))

        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

        fake.terminate.assert_called_once()
        assert killed == [signal.SIGTERM]
        # Disposition restored to default so the re-raised signal actually
        # terminates the process with the conventional status.
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL

    def test_sigint_handler_also_installed(self, restore_signals):
        stub = HeadlessIdaStub(session_mode=True)
        stub._install_signal_handlers()
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        assert handler is signal.getsignal(signal.SIGTERM)


class TestStdioRunnerCleanup:
    @pytest.mark.asyncio
    async def test_unwind_triggers_daemon_cleanup(self, monkeypatch):
        # Simulates client disconnect / server-side failure: whatever the
        # exit path, the finally block reaps the session-mode daemon.
        stub = HeadlessIdaStub(session_mode=True)
        fake = _fake_running_proc()
        stub._daemon_proc = fake

        class _FakeStdio:
            async def __aenter__(self):
                return (None, None)

            async def __aexit__(self, *exc):
                return False

        async def _failing_run(*_args, **_kwargs):
            raise RuntimeError("client disconnected")

        monkeypatch.setattr(stdio_stub, "stdio_server", lambda: _FakeStdio())
        monkeypatch.setattr(stub._mcp_server, "run", _failing_run)

        with pytest.raises(RuntimeError):
            await stub.run_stdio_async()

        fake.terminate.assert_called_once()
        assert stub._daemon_proc is None
