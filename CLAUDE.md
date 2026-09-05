# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FastAPI + HTMX service that wraps [f5oeo/rpitx](https://github.com/f5oeo/rpitx) to start/stop/monitor
Raspberry Pi RF transmissions from a browser or JSON API. Develops on any machine against a **mock
backend** (spawns `sleep` instead of a radio); flipping `RPITX_MODE=real` spawns the actual rpitx
binaries on a Pi. No radio hardware is needed to run or test everything except live transmission.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload      # dev server on :8000 (mock by default)
.venv/bin/python -m pytest                    # full suite (mock backend, no hardware)
.venv/bin/python -m pytest tests/test_modes.py::test_pocsag_argv   # single test
```

Config is env/`.env` (see `.env.example`): `RPITX_MODE`, `BIN_DIR`, `FREQ_ALLOWLIST`, `MAX_TX_SECONDS`.
`pytest.ini` sets `asyncio_mode = auto`, so async tests need no marker.

## Architecture

Request → `api/routes.py` (JSON) or `web/routes.py` (HTMX) → `tx/manager.py` → `tx/backends.py` → OS process.

- **`tx/modes.py` is the single source of truth.** Each mode = a pydantic input model + a builder that
  turns validated input into an `argv` list, registered in `REGISTRY`. The JSON API, the web forms, and
  validation all derive from this — `model_json_schema()` drives both `/api/modes` and the HTMX form
  partials. **Add a mode** by defining a `FreqMixin` subclass + a `_builder(model) -> list[str]` and
  appending a `Mode(...)` to `REGISTRY`; nothing else needs touching.
- **`argv[0]` is the binary NAME, not a path.** `RealBackend` resolves it against `settings.bin_dir`;
  `MockBackend` only logs it. Builders must never emit absolute paths.
- **`TxManager` (singleton `manager`) enforces one TX at a time.** rpitx owns the DMA/PLL hardware
  exclusively, so a start while busy raises `TxBusyError` → HTTP `409` (fail fast, never queue). It also
  runs an asyncio **watchdog** that auto-stops at `min(requested, MAX_TX_SECONDS)` — the hard duration cap.
- **`backends.py`** abstracts process lifecycle behind a `Backend` Protocol. Stop is always
  **SIGINT-then-SIGKILL**: rpitx cleans up DMA on SIGINT, so never hard-kill first.
- **Safety invariants** (don't regress these): every start requires an explicit `authorized` ack
  (`403` without it); `freq_hz` is validated against `settings.allowed_ranges` at the pydantic layer
  (`400`/`ValidationError` on reject); the lifespan shutdown hook calls `manager.stop()` so the app never
  exits mid-transmission.

HTTP contract: `202` started · `403` unauthorized · `400` invalid/blocked freq · `409` busy · `404` unknown mode.

## Gotchas

- Jinja's template cache is explicitly disabled in `web/routes.py` (`templates.env.cache = None`) — it is
  broken under Python 3.14, which this repo runs on. Leave it off.
- `RealBackend` needs root/GPIO on a real Pi; it is never exercised by the test suite.
- **rpitx CLI conventions** (verified against the rev pinned in `pkgs/rpitx.nix`): `tune`, `pocsag`,
  `sendiq`, `pichirp` take **Hz**; only `pifmrds -freq` takes MHz. `pocsag` reads `ric:message` lines on
  **stdin** (`-r` = baud, `-b` = function bits). `pisstv` is positional (`pisstv file.rgb hz`) and wants
  raw 320x256 RGB, so non-`.rgb` images go through an ImageMagick `convert` pipe.
- **File params (`audio_file`/`iq_file`/`image_file`) must be existing files inside `UPLOAD_DIR`**
  (`ALLOW_ANY_PATH=true` disables that). The service runs as root on the Pi; this stops arbitrary host
  files being put on the air.
- `RealBackend` drains the child's stderr into the log. Never leave it as an unread pipe: rpitx tools
  fill the 64 KiB buffer and block mid-transmission.

## Nix deployment path

`flake.nix`, `pkgs/`, and `modules/` build a NixOS SD image for a Pi 3B: `pkgs/rpitx.nix` compiles
rpitx from source and `pkgs/rpitx-dashboard.nix` packages **this same `app/` package** under uvicorn as
`rpitx-dashboard`. `modules/rpitx.nix` is the systemd unit and sets the env vars above. Pipeline tools
(sox, csdr, ImageMagick) must be in the unit's `path`, not just `environment.systemPackages` — systemd
units on NixOS do not see `/run/current-system/sw/bin`.

`modules/headless.nix` provisions from files on the FAT partition (`/boot/firmware`): Raspberry Pi
Imager's `firstrun.sh` (via shims at `/usr/lib/raspberrypi-sys-mods/imager_custom` and
`/usr/lib/userconf-pi/userconf`) or plain files (`userconf.txt`, `authorized_keys`,
`wpa_supplicant.conf`, `hostname`, `network.txt`, `*.network`). Wi-Fi goes through
`networking.wireless.allowAuxiliaryImperativeNetworks`, networking is networkd. `imager/os_list.json.in`
is filled by CI on tags so Imager 2.x will customise the image (`init_format: systemd`).
