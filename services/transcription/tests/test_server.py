"""Server entrypoint: loopback binding and the FR-14 ready line.

The listening socket is created by this module (never by uvicorn) so the
actually-bound port is known before the ready line is emitted (FR-14). These
tests prefer driving `TranscriptionServer`/`uvicorn.Config` objects directly
over spawning a real subprocess; one test runs the server on a background
thread purely to measure `/health` availability against the NFR-1 budget and
to prove the "exactly one stdout line" contract holds for a whole lifetime.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fakes import FakeProvider

from transcription import logging_setup, providers
from transcription import server as server_mod
from transcription.app import create_app
from transcription.config import Config


@pytest.fixture(autouse=True)
def _reset_ready_line_guard() -> None:
    """`emit_ready_line` enforces "once per process" (T9); each test here
    simulates its own process lifetime on a thread, so reset the module
    global between tests instead of poisoning later tests in this session.
    """
    logging_setup._ready_emitted = False


def _make_config(tmp_app_dir: Path, *, port: int = 0) -> Config:
    providers.register("fake", FakeProvider)
    return Config(
        app_dir=tmp_app_dir,
        config_path=tmp_app_dir / "config.json",
        provider="fake",
        allowed_roots=(str(tmp_app_dir),),
        db_path=str(tmp_app_dir / "data" / "jobs.sqlite3"),
        token="test-token",  # noqa: S106 -- test fixture
        port=port,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until(predicate: object, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return
        time.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def test_binds_socket_on_loopback_only(tmp_app_dir: Path) -> None:
    config = _make_config(tmp_app_dir)
    handle = server_mod.TranscriptionServer(config)

    sock = handle.bind()
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_build_uvicorn_config_uses_loopback_host(tmp_app_dir: Path) -> None:
    config = _make_config(tmp_app_dir)
    handle = server_mod.TranscriptionServer(config)
    sock = handle.bind()
    try:
        app = create_app(config)
        uv_config = server_mod.build_uvicorn_config(config, sock, app)
        assert uv_config.host == "127.0.0.1"
    finally:
        sock.close()


def test_fixed_configured_port_is_used_verbatim(tmp_app_dir: Path) -> None:
    fixed_port = _free_port()
    config = _make_config(tmp_app_dir, port=fixed_port)
    handle = server_mod.TranscriptionServer(config)

    sock = handle.bind()
    try:
        assert sock.getsockname()[1] == fixed_port
    finally:
        sock.close()


def test_socket_created_before_ready_line_and_port_matches(
    tmp_app_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _make_config(tmp_app_dir, port=0)
    order: list[tuple[str, int | None]] = []

    original_create = server_mod.create_listening_socket

    def spy_create(host: str, port: int) -> socket.socket:
        sock = original_create(host, port)
        order.append(("bind", sock.getsockname()[1]))
        return sock

    def fake_emit_ready_line(*, port: int, token: str, pid: int, stream: object = None) -> None:
        order.append(("ready", port))

    def fake_run(self: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        order.append(("uvicorn_run", None))
        for sock in sockets or []:
            sock.close()

    monkeypatch.setattr(server_mod, "create_listening_socket", spy_create)
    monkeypatch.setattr(server_mod, "emit_ready_line", fake_emit_ready_line)
    monkeypatch.setattr(uvicorn.Server, "run", fake_run)

    server_mod.run_server(config)

    assert [kind for kind, _ in order] == ["bind", "ready", "uvicorn_run"]
    bound_port = order[0][1]
    assert order[1] == ("ready", bound_port)


def test_exactly_one_ready_line_reaches_stdout_for_server_lifetime(
    tmp_app_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _make_config(tmp_app_dir, port=0)
    handle = server_mod.TranscriptionServer(config)

    thread = threading.Thread(target=handle.run, daemon=True)
    thread.start()
    try:
        _wait_until(lambda: handle.bound_port_or_none() is not None)
        port = handle.bound_port_or_none()
        assert port is not None

        _wait_until(lambda: _health_ok(port))
    finally:
        handle.shutdown()
        thread.join(timeout=5)

    assert not thread.is_alive()

    captured = capsys.readouterr()
    assert captured.err == "" or "listening" not in captured.err
    lines = [line for line in captured.out.splitlines() if line]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "event": "listening",
        "port": port,
        "token": config.token,
        "pid": os.getpid(),
    }


def _health_ok(port: int) -> bool:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def test_health_answers_within_three_seconds_of_process_start(tmp_app_dir: Path) -> None:
    config = _make_config(tmp_app_dir, port=0)
    handle = server_mod.TranscriptionServer(config)

    start = time.monotonic()
    thread = threading.Thread(target=handle.run, daemon=True)
    thread.start()
    try:
        _wait_until(lambda: handle.bound_port_or_none() is not None)
        port = handle.bound_port_or_none()
        assert port is not None
        _wait_until(lambda: _health_ok(port), timeout=3.0)
        elapsed = time.monotonic() - start
        assert elapsed < 3.0
    finally:
        handle.shutdown()
        thread.join(timeout=5)


def test_shutdown_stops_server_and_closes_ledger_without_non_terminal_row(
    tmp_app_dir: Path,
) -> None:
    config = _make_config(tmp_app_dir, port=0)
    handle = server_mod.TranscriptionServer(config)

    thread = threading.Thread(target=handle.run, daemon=True)
    thread.start()
    _wait_until(lambda: handle.bound_port_or_none() is not None)
    port = handle.bound_port_or_none()
    assert port is not None
    _wait_until(lambda: _health_ok(port))

    handle.shutdown()
    thread.join(timeout=5)

    assert not thread.is_alive()

    from transcription.ledger import Ledger

    ledger = Ledger(config.db_path)
    try:
        rows = ledger.list_jobs()
        assert all(row["status"] not in ("queued", "running") for row in rows)
    finally:
        ledger.close()
