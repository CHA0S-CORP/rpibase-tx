import asyncio

import pytest

from app.tx.backends import MockBackend, ProcHandle
from app.tx.manager import TxBusyError, TxManager

FREQ = 434_000_000


@pytest.fixture(autouse=True)
def _isolate_lock(tmp_path, monkeypatch):
    # Each test gets its own tx lock file so managers don't couple across tests.
    from app.config import settings

    monkeypatch.setattr(settings, "tx_lock_file", str(tmp_path / "tx.lock"))


def _mgr(backend=None):
    return TxManager(backend=backend or MockBackend())


class FixedBackend(MockBackend):
    """Spawns a `sleep` of a fixed wall time, ignoring the requested duration.

    Lets tests decouple 'how long the process actually runs' from 'the duration
    cap the manager enforces', which is what exercises the reap vs. kill paths.
    """

    def __init__(self, seconds: float):
        self._seconds = seconds

    async def start(self, argv, duration):  # noqa: ARG002 - duration intentionally ignored
        proc = await asyncio.create_subprocess_exec(
            "sleep", str(self._seconds), start_new_session=True
        )
        return ProcHandle(proc=proc, argv=argv)


@pytest.mark.asyncio
async def test_start_status_stop():
    m = _mgr()
    state = await m.start("tune", {"freq_hz": FREQ, "max_seconds": 5})
    assert state.pid > 0
    s = m.status()
    assert s["running"] and s["tx_mode"] == "tune"
    assert s["elapsed_s"] >= 0 and s["remaining_s"] <= 5
    assert await m.stop() is True
    assert m.status()["running"] is False


@pytest.mark.asyncio
async def test_single_slot_busy():
    m = _mgr(FixedBackend(5))
    await m.start("tune", {"freq_hz": FREQ, "max_seconds": 5})
    with pytest.raises(TxBusyError):
        await m.start("tune", {"freq_hz": FREQ + 1_000_000, "max_seconds": 5})
    await m.stop()


@pytest.mark.asyncio
async def test_natural_exit_is_reaped():
    # Process exits on its own well before the cap; the supervisor must clear
    # state without anyone polling status().
    m = _mgr(FixedBackend(0.3))
    await m.start("tune", {"freq_hz": FREQ, "max_seconds": 30})
    await asyncio.sleep(0.8)
    assert m.status()["running"] is False
    # Slot is free again — starting must not raise TxBusyError.
    await m.start("tune", {"freq_hz": FREQ, "max_seconds": 30})
    await m.stop()


@pytest.mark.asyncio
async def test_overrun_is_killed_at_cap():
    # Process would run 30s but the cap is 1s: the supervisor must kill it.
    m = _mgr(FixedBackend(30))
    await m.start("tune", {"freq_hz": FREQ, "max_seconds": 1})
    assert m.status()["running"] is True
    await asyncio.sleep(1.8)
    assert m.status()["running"] is False


@pytest.mark.asyncio
async def test_stale_supervisor_never_kills_a_newer_tx():
    # Regression: TX A finishes early; its watchdog must not later tear down a
    # subsequent TX B. A runs 0.2s with a 1s cap; B starts after A is gone.
    m = _mgr(FixedBackend(0.2))
    await m.start("tune", {"freq_hz": FREQ, "max_seconds": 1})
    await asyncio.sleep(0.5)  # A has exited and been reaped
    assert m.status()["running"] is False

    # B runs long; keep it up past where A's old 1s watchdog would have fired.
    m_b = FixedBackend(30)
    m._backend = m_b
    await m.start("tune", {"freq_hz": FREQ, "max_seconds": 30})
    await asyncio.sleep(1.0)
    assert m.status()["running"] is True, "a stale watchdog killed the new TX"
    await m.stop()


@pytest.mark.asyncio
async def test_duration_clamped_to_cap():
    from app.config import settings

    m = _mgr(FixedBackend(5))
    state = await m.start(
        "tune", {"freq_hz": FREQ, "max_seconds": settings.max_tx_seconds + 999}
    )
    assert round(state.deadline - state.started_at) == settings.max_tx_seconds
    await m.stop()


@pytest.mark.asyncio
async def test_stop_when_idle_is_noop():
    m = _mgr()
    assert await m.stop() is False


@pytest.mark.asyncio
async def test_cross_process_lock_blocks_second_manager():
    # Two managers share one lock file (stands in for two worker processes).
    a = TxManager(FixedBackend(5))
    b = TxManager(FixedBackend(5))
    await a.start("tune", {"freq_hz": FREQ, "max_seconds": 5})
    with pytest.raises(TxBusyError):
        await b.start("tune", {"freq_hz": FREQ, "max_seconds": 5})
    await a.stop()
    # Lock released — the other manager can now key up.
    await b.start("tune", {"freq_hz": FREQ, "max_seconds": 5})
    await b.stop()


@pytest.mark.asyncio
async def test_nbfm_builds_pipeline():
    m = _mgr(FixedBackend(5))
    state = await m.start(
        "nbfm", {"freq_hz": FREQ, "audio_file": "/tmp/a.wav", "max_seconds": 5}
    )
    assert state.argv[0] == "/bin/sh" and state.argv[1] == "-c"
    assert "sendiq" in state.argv[2] and "fmmod_fc" in state.argv[2]
    await m.stop()
