"""Application settings loaded from environment / .env."""
from __future__ import annotations

import os
import tempfile

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # "mock" = no hardware; "real" = spawn rpitx binaries on the Pi
    rpitx_mode: str = "mock"
    bin_dir: str = "/opt/rpitx"

    # Allowed frequency ranges in Hz, parsed from "low-high,low-high"
    freq_allowlist: str = "430000000-440000000"

    # Hard cap on any single transmission (seconds)
    max_tx_seconds: int = 60

    # Where uploaded audio/image/IQ files are stored for later transmission.
    upload_dir: str = "uploads"

    # Host-global lock file guarding the single physical transmitter. Empty ->
    # a per-host default under the temp dir. Set this (e.g. /run/rpitx-dashboard/
    # tx.lock) so the single-TX guarantee holds even across uvicorn workers or
    # separate processes.
    tx_lock_file: str = ""

    @property
    def lock_file(self) -> str:
        return self.tx_lock_file or os.path.join(tempfile.gettempdir(), "rpitx-tx.lock")

    @field_validator("rpitx_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        v = v.lower()
        if v not in ("mock", "real"):
            raise ValueError("rpitx_mode must be 'mock' or 'real'")
        return v

    @property
    def is_real(self) -> bool:
        return self.rpitx_mode == "real"

    @property
    def allowed_ranges(self) -> list[tuple[int, int]]:
        """Parse freq_allowlist into a list of (low_hz, high_hz) tuples."""
        ranges: list[tuple[int, int]] = []
        for chunk in self.freq_allowlist.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            low_s, _, high_s = chunk.partition("-")
            low, high = int(low_s), int(high_s)
            if low > high:
                low, high = high, low
            ranges.append((low, high))
        return ranges

    def freq_allowed(self, hz: int) -> bool:
        return any(low <= hz <= high for low, high in self.allowed_ranges)


settings = Settings()
