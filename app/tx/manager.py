"""Single-slot transmission manager.

rpitx drives the Pi's DMA/PLL hardware exclusively — only one transmission may
run at a time. TxManager enforces that with a lock and tracks the one active TX.

Each transmission gets a monotonically increasing *generation* id and a single
supervisor task. The supervisor waits for the process to exit; if it overruns
the duration cap it kills it, and either way it clears the active-TX state — but
only if that state still belongs to *its* generation. This is what prevents a
stale watchdog from a finished TX from tearing down a newer one, and guarantees
processes are always reaped (no zombies, no hung carrier).
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import time
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.tx.backends import Backend, ProcHandle, make_backend
from app.tx.modes import get_mode


class TxBusyError(Exception):
    """A transmission is already running."""


class _ProcLock:
    """Host-global advisory lock over the physical transmitter.

    The in-process asyncio lock only serialises *this* process. A flock on a
    shared file additionally blocks a second process (a second uvicorn worker,
    a CLI run, anything) from keying the transmitter concurrently.
    """

    def __init__(self) -> None:
        self._fd: int | None = None

    def acquire(self) -> bool:
        path = settings.lock_file
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


@dataclass
class TxState:
    mode: str
    params: dict[str, Any]
    argv: list[str]
    pid: int
    started_at: float
    deadline: float


class TxManager:
    def __init__(self, backend: Backend | None = None) -> None:
        self._backend = backend or make_backend()
        self._lock = asyncio.Lock()
        self._handle: ProcHandle | None = None
        self._state: TxState | None = None
        self._supervisor: asyncio.Task | None = None
        self._gen = 0
        self._proclock = _ProcLock()

    def _clamp_duration(self, requested: int | None) -> int:
        cap = settings.max_tx_seconds
        if requested is None or requested <= 0:
            return cap
        return min(requested, cap)

    async def start(self, mode_name: str, data: dict) -> TxState:
        mode = get_mode(mode_name)  # raises KeyError on unknown mode
        validated, argv = mode.build(data)  # raises ValidationError on bad input
        duration = self._clamp_duration(getattr(validated, "max_seconds", None))

        async with self._lock:
            # Busy only if a process is actually still alive.
            if self._handle is not None and self._backend.running(self._handle):
                raise TxBusyError()

            # Cross-process guard: another worker/process may hold the tx.
            if not self._proclock.acquire():
                raise TxBusyError()
            try:
                handle = await self._backend.start(argv, duration)
            except BaseException:
                self._proclock.release()
                raise
            self._gen += 1
            gen = self._gen
            now = time.monotonic()
            self._handle = handle
            self._state = TxState(
                mode=mode_name,
                params=validated.model_dump(),
                argv=argv,
                pid=handle.pid,
                started_at=now,
                deadline=now + duration,
            )
            self._supervisor = asyncio.create_task(self._supervise(gen, handle, duration))
            return self._state

    async def _supervise(self, gen: int, handle: ProcHandle, duration: int) -> None:
        """Reap the process on natural exit; kill it if it overruns the cap."""
        try:
            await asyncio.wait_for(handle.proc.wait(), timeout=duration)
        except asyncio.TimeoutError:
            # Exceeded the max-duration cap — force it down.
            await self._backend.stop(handle)
        except asyncio.CancelledError:
            return  # a manual stop() is taking over cleanup
        # Clear state only if we are still the current transmission.
        async with self._lock:
            if self._gen == gen:
                self._handle = None
                self._state = None
                self._supervisor = None
                self._proclock.release()

    async def stop(self) -> bool:
        """Stop the active TX. Returns True if something was actually stopped."""
        async with self._lock:
            if self._supervisor is not None and not self._supervisor.done():
                self._supervisor.cancel()
            self._supervisor = None
            if self._handle is None:
                return False
            await self._backend.stop(self._handle)
            # Bump the generation so any in-flight supervisor becomes a no-op.
            self._gen += 1
            self._handle = None
            self._state = None
            self._proclock.release()
            return True

    def _running(self) -> bool:
        return self._handle is not None and self._backend.running(self._handle)

    def status(self) -> dict[str, Any]:
        # Read-only: never mutate here. The supervisor owns cleanup, so status
        # just reflects whether the process is currently alive.
        if not self._running() or self._state is None:
            return {"running": False, "mode": settings.rpitx_mode}
        now = time.monotonic()
        return {
            "running": True,
            "mode": settings.rpitx_mode,
            "tx_mode": self._state.mode,
            "pid": self._state.pid,
            "params": self._state.params,
            "argv": self._state.argv,
            "elapsed_s": round(now - self._state.started_at, 1),
            "remaining_s": round(max(0.0, self._state.deadline - now), 1),
        }


# App-wide singleton.
manager = TxManager()
