import time

import pytest
from fastapi.testclient import TestClient

from app.main import app

FREQ = 434_000_000


@pytest.fixture(autouse=True)
def _isolate_lock(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "tx_lock_file", str(tmp_path / "tx.lock"))


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
        c.post("/api/stop")


def _wait_idle(client, timeout=5.0):
    """Poll status until the transmitter reports idle (or fail)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/api/status").json()["running"] is False:
            return True
        time.sleep(0.1)
    return False


def test_modes_endpoint(client):
    r = client.get("/api/modes")
    assert r.status_code == 200
    body = r.json()
    # All registered modes are advertised with a schema.
    for mode in ("tune", "pifmrds", "pocsag", "sendiq", "pisstv", "pichirp", "nbfm"):
        assert mode in body
        assert "schema" in body[mode]


def test_tx_requires_authorization(client):
    r = client.post("/api/tx/tune", json={"freq_hz": 434_000_000})
    assert r.status_code == 403


def test_tx_start_stop(client):
    r = client.post(
        "/api/tx/tune", json={"freq_hz": 434_000_000, "max_seconds": 5, "authorized": True}
    )
    assert r.status_code == 202
    assert client.get("/api/status").json()["running"] is True
    assert client.post("/api/stop").json()["stopped"] is True


def test_tx_busy_returns_409(client):
    body = {"freq_hz": 434_000_000, "max_seconds": 5, "authorized": True}
    assert client.post("/api/tx/tune", json=body).status_code == 202
    assert client.post("/api/tx/tune", json=body).status_code == 409


def test_bad_freq_returns_400(client):
    r = client.post(
        "/api/tx/tune", json={"freq_hz": 100_000_000, "authorized": True}
    )
    assert r.status_code == 400


def test_upload_then_transmit(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    r = client.post("/api/upload", files={"file": ("clip.wav", b"RIFFdata", "audio/wav")})
    assert r.status_code == 201
    path = r.json()["path"]
    assert r.json()["filename"] == "clip.wav"
    assert (tmp_path / "clip.wav").read_bytes() == b"RIFFdata"

    assert path in [u["path"] for u in client.get("/api/uploads").json()["uploads"]]

    r = client.post(
        "/api/tx/pifmrds",
        json={"freq_hz": 434_000_000, "authorized": True, "audio_file": path},
    )
    assert r.status_code == 202


def test_upload_filename_is_sanitized(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    r = client.post("/api/upload", files={"file": ("../../etc/evil.wav", b"x", "audio/wav")})
    assert r.status_code == 201
    # Reduced to a basename inside the upload dir; nothing escapes.
    assert r.json()["filename"] == "evil.wav"
    assert str(tmp_path) in r.json()["path"]


def test_full_lifecycle_and_monitoring(client):
    r = client.post(
        "/api/tx/tune", json={"freq_hz": FREQ, "max_seconds": 10, "authorized": True}
    )
    assert r.status_code == 202
    s = client.get("/api/status").json()
    assert s["running"] is True
    assert s["tx_mode"] == "tune"
    assert s["pid"] > 0
    assert 0 <= s["elapsed_s"] and 0 < s["remaining_s"] <= 10
    assert client.post("/api/stop").json()["stopped"] is True
    assert client.get("/api/status").json()["running"] is False


def test_short_tx_is_reaped_and_slot_frees(client):
    # A 1s transmission must self-clear (supervisor reaps) with no stop call,
    # and the slot must accept a new TX afterwards.
    r = client.post(
        "/api/tx/tune", json={"freq_hz": FREQ, "max_seconds": 1, "authorized": True}
    )
    assert r.status_code == 202
    assert _wait_idle(client, timeout=5.0), "TX was never reaped"
    # Slot is free — not a 409.
    r2 = client.post(
        "/api/tx/tune", json={"freq_hz": FREQ, "max_seconds": 5, "authorized": True}
    )
    assert r2.status_code == 202
    client.post("/api/stop")


def test_nbfm_upload_then_transmit(client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    up = client.post("/api/upload", files={"file": ("voice.wav", b"RIFFxxxx", "audio/wav")})
    assert up.status_code == 201
    path = up.json()["path"]

    r = client.post(
        "/api/tx/nbfm",
        json={"freq_hz": FREQ, "authorized": True, "audio_file": path, "max_seconds": 5},
    )
    assert r.status_code == 202
    argv = client.get("/api/status").json()["argv"]
    assert argv[0] == "/bin/sh"
    assert "sendiq" in argv[2] and "fmmod_fc" in argv[2] and path in argv[2]
    client.post("/api/stop")


def test_nbfm_gain_out_of_range_rejected(client):
    r = client.post(
        "/api/tx/nbfm",
        json={"freq_hz": FREQ, "authorized": True, "audio_file": "/tmp/a.wav", "gain": 2.0},
    )
    assert r.status_code == 400
