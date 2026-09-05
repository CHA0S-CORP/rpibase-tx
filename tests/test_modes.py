import pytest
from pydantic import ValidationError

from app.tx.modes import get_mode

FREQ = 434_000_000


@pytest.fixture
def upload(tmp_path, monkeypatch):
    """Point upload_dir at tmp and return a factory for files inside it."""
    from app.config import settings

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    def _mk(name: str) -> str:
        f = tmp_path / name
        f.write_bytes(b"x")
        return str(f)

    return _mk


def test_tune_argv_is_hz():
    # rpitx `tune -f` takes Hz (only pifmrds takes MHz).
    _, argv = get_mode("tune").build({"freq_hz": FREQ})
    assert argv == ["tune", "-f", "434000000"]


def test_pifmrds_argv_is_mhz(upload):
    _, argv = get_mode("pifmrds").build({"freq_hz": FREQ, "audio_file": upload("a.wav")})
    assert argv[:3] == ["pifmrds", "-freq", "434.0"]


def test_pocsag_argv_and_stdin():
    mode = get_mode("pocsag")
    m, argv = mode.build({"freq_hz": FREQ, "ric": 123456, "message": "hello", "baud": 1200})
    # -f Hz, -r baud; message goes on stdin as "ric:message", never in argv.
    assert argv == ["pocsag", "-f", "434000000", "-r", "1200"]
    assert mode.build_stdin(m) == b"123456:hello\n"


def test_pocsag_rejects_multiline_message():
    with pytest.raises(ValidationError):
        get_mode("pocsag").build({"freq_hz": FREQ, "ric": 1, "message": "a\nb"})


def test_pisstv_raw_rgb_is_positional(upload):
    _, argv = get_mode("pisstv").build({"freq_hz": FREQ, "image_file": upload("pic.rgb")})
    assert argv == ["pisstv", argv[1], "434000000"]


def test_pisstv_other_formats_go_through_convert(upload):
    path = upload("pic.png")
    _, argv = get_mode("pisstv").build({"freq_hz": FREQ, "image_file": path})
    assert argv[0] == "/bin/sh"
    assert "convert" in argv[2] and "pisstv /dev/stdin 434000000" in argv[2]


def test_file_param_outside_upload_dir_rejected(upload, tmp_path):
    outside = tmp_path.parent / "elsewhere.wav"
    outside.write_bytes(b"x")
    with pytest.raises(ValidationError):
        get_mode("sendiq").build({"freq_hz": FREQ, "iq_file": str(outside)})


def test_file_param_missing_file_rejected(upload, tmp_path):
    with pytest.raises(ValidationError):
        get_mode("sendiq").build({"freq_hz": FREQ, "iq_file": str(tmp_path / "nope.iq")})


def test_modes_without_stdin_return_none():
    m, _ = get_mode("tune").build({"freq_hz": FREQ})
    assert get_mode("tune").build_stdin(m) is None


def test_freq_outside_allowlist_rejected():
    with pytest.raises(ValidationError):
        get_mode("tune").build({"freq_hz": 100_000_000})  # 100 MHz not allowed


def test_bad_baud_rejected():
    with pytest.raises(ValidationError):
        get_mode("pocsag").build(
            {"freq_hz": 434_000_000, "ric": 1, "message": "x", "baud": 999}
        )


def test_unknown_mode():
    with pytest.raises(KeyError):
        get_mode("nope")
