import pytest
from pydantic import ValidationError

from app.tx.modes import get_mode


def test_tune_argv():
    _, argv = get_mode("tune").build({"freq_hz": 434_000_000})
    assert argv == ["tune", "-f", "434.0"]


def test_pocsag_argv():
    _, argv = get_mode("pocsag").build(
        {"freq_hz": 434_000_000, "ric": 123456, "message": "hello", "baud": 1200}
    )
    assert argv[0] == "pocsag"
    assert "123456" in argv and "hello" in argv


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
