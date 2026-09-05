"""TX mode registry.

Each mode pairs a pydantic input model with a builder that turns validated
input into the argv list handed to a backend. This is the single source of
truth shared by the JSON API, the web forms, and validation.

argv[0] is the binary NAME (not a full path); the RealBackend resolves it
against settings.bin_dir — unless it is an absolute path (e.g. "/bin/sh" for a
pipeline mode), which runs as-is. The MockBackend just logs it.

A mode may also supply a `stdin` builder returning bytes fed to the child's
stdin (pocsag reads its messages that way). Frequencies: every rpitx tool takes
Hz on the command line except pifmrds, which takes MHz.

Verified against rpitx ee7ff57 (the rev pinned in pkgs/rpitx.nix).
"""
from __future__ import annotations

import shlex
from typing import Callable

from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.uploads import is_allowed_file


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


def _file_param(v: str) -> str:
    if not is_allowed_file(v):
        raise ValueError(
            "must be an existing file inside the upload directory "
            "(POST /api/upload and use the returned path)"
        )
    return v


class TuneInput(FreqMixin):
    """Single unmodulated carrier."""


class PiFmRdsInput(FreqMixin):
    audio_file: str = Field(..., description="Path to audio (WAV/raw) to broadcast")
    rds_ps: str = Field("RPITX", max_length=8, description="RDS program service name")
    rds_rt: str = Field("rpibase-tx", max_length=64, description="RDS radiotext")

    _file = field_validator("audio_file")(_file_param)


class PocsagInput(FreqMixin):
    baud: int = Field(1200, description="POCSAG baud rate")
    ric: int = Field(..., ge=1, description="Receiver capcode")
    message: str = Field(..., max_length=80, description="Alphanumeric message body")

    @field_validator("message")
    @classmethod
    def _one_line(cls, v: str) -> str:
        # pocsag reads one "ric:message" line from stdin; newlines would split it.
        if "\n" in v or "\r" in v:
            raise ValueError("message must be a single line")
        return v

    @field_validator("baud")
    @classmethod
    def _valid_baud(cls, v: int) -> int:
        if v not in (512, 1200, 2400):
            raise ValueError("baud must be 512, 1200, or 2400")
        return v


class SendIqInput(FreqMixin):
    iq_file: str = Field(..., description="Path to IQ sample file")
    sample_rate: int = Field(48000, ge=1, description="IQ sample rate in Hz")

    _file = field_validator("iq_file")(_file_param)


class PiSstvInput(FreqMixin):
    image_file: str = Field(
        ..., description="Image to send (any format ImageMagick reads, or raw 320x256 .rgb)"
    )

    _file = field_validator("image_file")(_file_param)


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

    _file = field_validator("audio_file")(_file_param)


# builder signature: (validated_model) -> list[str] argv
Builder = Callable[[BaseModel], list[str]]
# stdin builder: (validated_model) -> bytes written to the child's stdin
StdinBuilder = Callable[[BaseModel], bytes]


def _mhz(hz: int) -> str:
    """Format Hz as MHz for pifmrds, never in scientific notation."""
    s = f"{hz / 1e6:.6f}".rstrip("0")
    return s + "0" if s.endswith(".") else s


def _tune(m: TuneInput) -> list[str]:
    return ["tune", "-f", str(m.freq_hz)]  # Hz


def _pifmrds(m: PiFmRdsInput) -> list[str]:
    return [
        "pifmrds",
        "-freq", _mhz(m.freq_hz),
        "-audio", m.audio_file,
        "-ps", m.rds_ps,
        "-rt", m.rds_rt,
    ]


def _pocsag(m: PocsagInput) -> list[str]:
    # -f Hz, -r baud rate. (-b is *function bits*, not baud.) The message is
    # NOT an argument: pocsag reads "<ric>:<message>" lines from stdin.
    return ["pocsag", "-f", str(m.freq_hz), "-r", str(m.baud)]


def _pocsag_stdin(m: PocsagInput) -> bytes:
    return f"{m.ric}:{m.message}\n".encode()


def _sendiq(m: SendIqInput) -> list[str]:
    return [
        "sendiq",
        "-i", m.iq_file,
        "-s", str(m.sample_rate),
        "-f", str(m.freq_hz),
        "-t", "float",
    ]


def _pisstv(m: PiSstvInput) -> list[str]:
    # pisstv is positional: `pisstv picture.rgb frequency(Hz)` and wants raw
    # 320x256 8-bit RGB. It read()s the file sequentially, so a pipe works: for
    # anything but a ready-made .rgb, run ImageMagick `convert` in front of it.
    if m.image_file.lower().endswith(".rgb"):
        return ["pisstv", m.image_file, str(m.freq_hz)]
    img = shlex.quote(m.image_file)
    pipeline = (
        f"convert {img} -resize 320x256! -depth 8 rgb:- "
        f"| pisstv /dev/stdin {m.freq_hz}"
    )
    return ["/bin/sh", "-c", pipeline]


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
    def __init__(
        self,
        name: str,
        model: type[BaseModel],
        builder: Builder,
        desc: str,
        stdin: StdinBuilder | None = None,
    ):
        self.name = name
        self.model = model
        self.builder = builder
        self.desc = desc
        self.stdin = stdin

    def build(self, data: dict) -> tuple[BaseModel, list[str]]:
        validated = self.model(**data)
        return validated, self.builder(validated)  # type: ignore[arg-type]

    def build_stdin(self, validated: BaseModel) -> bytes | None:
        return self.stdin(validated) if self.stdin else None  # type: ignore[arg-type]


REGISTRY: dict[str, Mode] = {
    m.name: m
    for m in [
        Mode("tune", TuneInput, _tune, "Single unmodulated carrier tone"),
        Mode("pifmrds", PiFmRdsInput, _pifmrds, "FM broadcast with RDS text"),
        Mode("pocsag", PocsagInput, _pocsag, "POCSAG pager message", stdin=_pocsag_stdin),
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
