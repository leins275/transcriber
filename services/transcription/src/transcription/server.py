"""Server entrypoint: loopback binding and the FR-14 ready line.

The listening socket is created and bound by this module, never by uvicorn,
so the actually-bound port is known (needed when ``port=0`` picks a free
one) before anything is printed. :func:`transcription.logging_setup.emit_ready_line`
is called only after the socket is already listening; uvicorn is then handed
the pre-bound socket via ``Server.run(sockets=[sock])`` so it never binds
anything itself. Uvicorn's own logging is left unconfigured
(``log_config=None``) and ``access_log=False`` disables its stdout access
logger, so stdout carries nothing but the single ready line for the whole
process lifetime (FR-14).
"""

from __future__ import annotations

import logging
import os
import socket
from typing import IO

import uvicorn
from fastapi import FastAPI

from transcription.app import create_app
from transcription.config import Config
from transcription.logging_setup import configure_logging, emit_ready_line


def create_listening_socket(host: str, port: int) -> socket.socket:
    """Bind and listen on ``(host, port)``.

    The caller reads the real, bound port back via ``sock.getsockname()``
    -- required when ``port=0`` asks the OS to pick a free one.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(socket.SOMAXCONN)
    return sock


def build_uvicorn_config(config: Config, sock: socket.socket, app: FastAPI) -> uvicorn.Config:
    """Build the ``uvicorn.Config`` uvicorn drives off the pre-bound socket.

    ``host``/``port`` here only feed uvicorn's own log line and the ASGI
    ``server`` scope value -- the real bind already happened in
    :func:`create_listening_socket`.
    """
    bound_port = sock.getsockname()[1]
    return uvicorn.Config(
        app,
        host=config.host,
        port=bound_port,
        log_config=None,
        access_log=False,
    )


def _resolve_log_level(name: str) -> int:
    level = logging.getLevelName(name.upper())
    return level if isinstance(level, int) else logging.INFO


class TranscriptionServer:
    """Owns the bound socket and the ``uvicorn.Server`` driving it.

    Split out from :func:`run_server` so callers (and tests) can bind,
    inspect the socket/config, run the server (blocking) -- typically on a
    background thread in tests -- and request a clean shutdown without
    spawning a real subprocess.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._sock: socket.socket | None = None
        self._uvicorn_server: uvicorn.Server | None = None

    def bind(self) -> socket.socket:
        """Create the listening socket once; idempotent across calls."""
        if self._sock is None:
            self._sock = create_listening_socket(self.config.host, self.config.port)
        return self._sock

    @property
    def bound_port(self) -> int:
        port: int = self.bind().getsockname()[1]
        return port

    def bound_port_or_none(self) -> int | None:
        """The bound port, or ``None`` before :meth:`bind`/:meth:`run` has run.

        Never binds as a side effect -- safe to poll from another thread
        while :meth:`run` is starting up.
        """
        if self._sock is None:
            return None
        port: int = self._sock.getsockname()[1]
        return port

    def run(self, *, ready_stream: IO[str] | None = None) -> None:
        """Blocking: bind (if needed), emit the ready line, serve until shutdown."""
        sock = self.bind()
        app = create_app(self.config)
        uv_config = build_uvicorn_config(self.config, sock, app)
        server = uvicorn.Server(uv_config)
        self._uvicorn_server = server

        emit_ready_line(
            port=self.bound_port,
            token=self.config.token,
            pid=os.getpid(),
            stream=ready_stream,
        )
        server.run(sockets=[sock])

    def shutdown(self) -> None:
        """Ask a running server to stop; a no-op before :meth:`run` starts serving."""
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True


def run_server(config: Config, *, ready_stream: IO[str] | None = None) -> None:
    """Blocking entrypoint used by ``cli.py serve``.

    Binds the loopback socket, emits the FR-14 ready line, then serves until
    SIGINT/SIGTERM (handled by uvicorn itself when running on the main
    thread) or a programmatic :meth:`TranscriptionServer.shutdown`.
    """
    configure_logging(_resolve_log_level(config.log_level))
    TranscriptionServer(config).run(ready_stream=ready_stream)
