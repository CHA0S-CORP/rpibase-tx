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
from dataclasses import dataclass
from typing import Protocol

from app.config import settings

log = logging.getLogger("rpibase.tx")


@dataclass
class ProcHandle:
    proc: asyncio.subprocess.Process
    argv: list[str]

    @property
    def pid(self) -> int:
        return self.proc.pid


class Backend(Protocol):
    async def start(self, argv: list[str], duration: int) -> ProcHandle: ...
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


class RealBackend:
    async def start(self, argv: list[str], duration: int) -> ProcHandle:
        # Absolute argv[0] (e.g. "/bin/sh -c <pipeline>") runs as-is; a bare
        # name resolves against BIN_DIR. Either way, BIN_DIR is on PATH so
        # pipeline tools (sendiq, csdr) resolve.
        exe = argv[0] if argv[0].startswith("/") else os.path.join(settings.bin_dir, argv[0])
        cmd = [exe, *argv[1:]]
        env = {**os.environ, "PATH": settings.bin_dir + os.pathsep + os.environ.get("PATH", "")}
        log.warning("REAL TX spawning: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        return ProcHandle(proc=proc, argv=argv)

    async def stop(self, handle: ProcHandle) -> None:
        await _terminate(handle.proc)

    def running(self, handle: ProcHandle) -> bool:
        return handle.proc.returncode is None


class MockBackend:
    async def start(self, argv: list[str], duration: int) -> ProcHandle:
        log.info("MOCK TX would run: %s (for %ss)", " ".join(argv), duration)
        # Stand-in process so pid/stop/supervise behave like the real thing.
        proc = await asyncio.create_subprocess_exec(
            "sleep", str(duration),
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
