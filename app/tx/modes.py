"""TX mode registry.

Each mode pairs a pydantic input model with a builder that turns validated
input into the argv list handed to a backend. This is the single source of
truth shared by the JSON API, the web forms, and validation.

argv[0] is the binary NAME (not a full path); the RealBackend resolves it
against settings.bin_dir — unless it is an absolute path (e.g. "/bin/sh" for a
pipeline mode), which runs as-is. The MockBackend just logs it.
"""
from __future__ import annotations

import shlex
from typing import Callable

from pydantic import BaseModel, Field, field_validator

from app.config import settings


class FreqMixin(BaseModel):
    freq_hz: int = Field(..., description="Carrier frequency in Hz")
    max_seconds: int | None = Field(
        None, description="Optional TX length cap; clamped to MAX_TX_SECONDS"
    )

    @field_validator("freq_hz")
    @classmethod
    def _freq_allowed(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("freq_hz must be positive")
        if not settings.freq_allowed(v):
            raise ValueError(
                f"freq_hz {v} outside allowed ranges {settings.allowed_ranges}"
            )
        return v


class TuneInput(FreqMixin):
    """Single unmodulated carrier."""


class PiFmRdsInput(FreqMixin):
    audio_file: str = Field(..., description="Path to audio (WAV/raw) to broadcast")
    rds_ps: str = Field("RPITX", max_length=8, description="RDS program service name")
    rds_rt: str = Field("rpibase-tx", max_length=64, description="RDS radiotext")


class PocsagInput(FreqMixin):
    baud: int = Field(1200, description="POCSAG baud rate")
    ric: int = Field(..., ge=1, description="Receiver capcode")
    message: str = Field(..., description="Alphanumeric message body")

    @field_validator("baud")
    @classmethod
    def _valid_baud(cls, v: int) -> int:
        if v not in (512, 1200, 2400):
            raise ValueError("baud must be 512, 1200, or 2400")
        return v


class SendIqInput(FreqMixin):
    iq_file: str = Field(..., description="Path to IQ sample file")
    sample_rate: int = Field(48000, ge=1, description="IQ sample rate in Hz")


class PiSstvInput(FreqMixin):
    image_file: str = Field(..., description="Path to image to encode as SSTV")


class PiChirpInput(FreqMixin):
    bandwidth_hz: int = Field(..., ge=1, description="Chirp sweep bandwidth in Hz")
    duration_s: float = Field(..., gt=0, description="Chirp sweep duration in seconds")


class NbfmInput(FreqMixin):
    audio_file: str = Field(..., description="Path to audio file to transmit")
    sample_rate: int = Field(48000, ge=1, description="Audio sample rate in Hz")
    gain: float = Field(
        0.1, gt=0, le=1.0,
        description="Modulation gain — controls FM deviation. ~0.1 for narrowband voice.",
    )


# builder signature: (validated_model) -> list[str] argv
Builder = Callable[[BaseModel], list[str]]


def _mhz(hz: int) -> str:
    """Format Hz as MHz for rpitx, never in scientific notation."""
    s = f"{hz / 1e6:.6f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _tune(m: TuneInput) -> list[str]:
    return ["tune", "-f", _mhz(m.freq_hz)]  # tune expects MHz


def _pifmrds(m: PiFmRdsInput) -> list[str]:
    return [
        "pifmrds",
        "-freq", _mhz(m.freq_hz),
        "-audio", m.audio_file,
        "-ps", m.rds_ps,
        "-rt", m.rds_rt,
    ]


def _pocsag(m: PocsagInput) -> list[str]:
    # pocsag reads "<ric>:<message>" on stdin; we pass it via a here-arg the
    # backend feeds to stdin. Encode as a trailing sentinel arg the backend
    # recognizes. Simpler: pass ric/baud as flags, message on stdin.
    return [
        "pocsag",
        "-f", str(m.freq_hz),
        "-b", str(m.baud),
        "-r", str(m.ric),
        m.message,
    ]


def _sendiq(m: SendIqInput) -> list[str]:
    return [
        "sendiq",
        "-i", m.iq_file,
        "-s", str(m.sample_rate),
        "-f", str(m.freq_hz),
        "-t", "float",
    ]


def _pisstv(m: PiSstvInput) -> list[str]:
    # pisstv produces an IQ/WAV that sendiq transmits. We expose the sstv
    # generation + TX as one logical mode; the real pipeline is wired in the
    # backend script. Here we hand off image + freq.
    return [
        "pisstv",
        "-p", "m1",
        "-i", m.image_file,
        "-f", str(m.freq_hz),
    ]


def _pichirp(m: PiChirpInput) -> list[str]:
    return [
        "pichirp",
        str(m.freq_hz),
        str(m.bandwidth_hz),
        str(m.duration_s),
    ]


def _nbfm(m: NbfmInput) -> list[str]:
    # Narrowband FM has no dedicated rpitx binary — it is the documented
    # sox -> csdr (FM-modulate) -> sendiq pipeline. Low gain keeps deviation
    # narrow. Run it under a shell; the RealBackend puts BIN_DIR on PATH and
    # kills the whole process group on stop so no stage is left keying.
    audio = shlex.quote(m.audio_file)
    pipeline = (
        f"sox {audio} -t raw -r {m.sample_rate} -c 1 -b 16 -e signed-integer - "
        f"| csdr convert_i16_f "
        f"| csdr gain_ff {m.gain} "
        f"| csdr fmmod_fc "
        f"| sendiq -i /dev/stdin -s {m.sample_rate} -f {m.freq_hz} -t float"
    )
    return ["/bin/sh", "-c", pipeline]


class Mode:
    def __init__(self, name: str, model: type[BaseModel], builder: Builder, desc: str):
        self.name = name
        self.model = model
        self.builder = builder
        self.desc = desc

    def build(self, data: dict) -> tuple[BaseModel, list[str]]:
        validated = self.model(**data)
        return validated, self.builder(validated)  # type: ignore[arg-type]


REGISTRY: dict[str, Mode] = {
    m.name: m
    for m in [
        Mode("tune", TuneInput, _tune, "Single unmodulated carrier tone"),
        Mode("pifmrds", PiFmRdsInput, _pifmrds, "FM broadcast with RDS text"),
        Mode("pocsag", PocsagInput, _pocsag, "POCSAG pager message"),
        Mode("sendiq", SendIqInput, _sendiq, "Replay an IQ sample file"),
        Mode("pisstv", PiSstvInput, _pisstv, "Transmit an image as SSTV"),
        Mode("pichirp", PiChirpInput, _pichirp, "Frequency chirp sweep"),
        Mode("nbfm", NbfmInput, _nbfm, "Narrowband FM audio (sox|csdr|sendiq)"),
    ]
}


def get_mode(name: str) -> Mode:
    mode = REGISTRY.get(name)
    if mode is None:
        raise KeyError(name)
    return mode
