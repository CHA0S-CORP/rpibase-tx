"""TX process backends.

A backend owns the OS-process lifecycle of a transmission. Two impls:

- RealBackend spawns the actual rpitx binary/pipeline (Pi only, needs root/GPIO).
- MockBackend spawns a harmless `sleep` stand-in so the whole start/stop/
  supervise machinery runs on a dev machine with no radio hardware.

Every child is started in its own session (`start_new_session=True`) so we can
signal the entire process group. That matters for two reasons: rpitx modes like
NBFM are shell *pipelines* (sox | csdr | sendiq) whose children would otherwise
outlive a kill of the shell and keep the carrier up, and group-signalling can
never leak to our own uvicorn/pytest process group.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Protocol

from app.config import settings

log = logging.getLogger("rpibase.tx")


@dataclass
class ProcHandle:
    proc: asyncio.subprocess.Process
    argv: list[str]
    # Background tasks tied to this process (stderr drain, stdin feed). Held
    # here so they are not garbage-collected mid-flight.
    tasks: list[asyncio.Task] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.proc.pid


class Backend(Protocol):
    async def start(
        self, argv: list[str], duration: int, stdin: bytes | None = None
    ) -> ProcHandle: ...
    async def stop(self, handle: ProcHandle) -> None: ...
    def running(self, handle: ProcHandle) -> bool: ...


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGINT the process group (rpitx cleans up DMA on it), then SIGKILL."""
    if proc.returncode is not None:
        return
    _killpg(proc.pid, signal.SIGINT)
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        _killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


async def _drain_stderr(name: str, stream: asyncio.StreamReader) -> None:
    """Log the child's stderr line by line.

    The pipe MUST be read: rpitx tools chatter on stderr, and once the 64 KiB
    pipe buffer fills the child blocks in write() with the carrier in an
    undefined state until the watchdog kills it.
    """
    try:
        while line := await stream.readline():
            log.info("[%s] %s", name, line.decode(errors="replace").rstrip())
    except (asyncio.CancelledError, ValueError):
        pass


async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # child exited before reading; the supervisor reports that
    finally:
        proc.stdin.close()


class RealBackend:
    async def start(
        self, argv: list[str], duration: int, stdin: bytes | None = None
    ) -> ProcHandle:
        # Absolute argv[0] (e.g. "/bin/sh -c <pipeline>") runs as-is; a bare
        # name resolves against BIN_DIR. Either way, BIN_DIR is on PATH so
        # pipeline tools (sendiq, csdr) resolve.
        exe = argv[0] if argv[0].startswith("/") else os.path.join(settings.bin_dir, argv[0])
        cmd = [exe, *argv[1:]]
        env = {**os.environ, "PATH": settings.bin_dir + os.pathsep + os.environ.get("PATH", "")}
        log.warning("REAL TX spawning: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            # Never inherit our stdin: under systemd it is /dev/null, under a
            # terminal it would steal keystrokes.
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        handle = ProcHandle(proc=proc, argv=argv)
        assert proc.stderr is not None
        handle.tasks.append(asyncio.create_task(_drain_stderr(argv[0], proc.stderr)))
        if stdin is not None:
            handle.tasks.append(asyncio.create_task(_feed_stdin(proc, stdin)))
        return handle

    async def stop(self, handle: ProcHandle) -> None:
        await _terminate(handle.proc)

    def running(self, handle: ProcHandle) -> bool:
        return handle.proc.returncode is None


class MockBackend:
    async def start(
        self, argv: list[str], duration: int, stdin: bytes | None = None
    ) -> ProcHandle:
        log.info("MOCK TX would run: %s (for %ss)", " ".join(argv), duration)
        if stdin is not None:
            log.info("MOCK TX stdin: %r", stdin)
        # Stand-in process so pid/stop/supervise behave like the real thing.
        proc = await asyncio.create_subprocess_exec(
            "sleep", str(duration),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return ProcHandle(proc=proc, argv=argv)

    async def stop(self, handle: ProcHandle) -> None:
        await _terminate(handle.proc)

    def running(self, handle: ProcHandle) -> bool:
        return handle.proc.returncode is None


def make_backend() -> Backend:
    return RealBackend() if settings.is_real else MockBackend()
