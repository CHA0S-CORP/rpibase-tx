# rpibase-tx

FastAPI service + HTMX dashboard wrapping [f5oeo/rpitx](https://github.com/f5oeo/rpitx).
Start/stop/monitor Raspberry Pi RF transmissions from a browser or a JSON API.

Develops on any machine against a **mock backend** (no radio). Flip one env var to
drive real rpitx binaries on a Pi.

> ## ⚠️ RF SAFETY / LEGAL
> rpitx emits **unfiltered, harmonic-rich RF** on GPIO4. Transmitting outside your
> license or a permitted ISM band is illegal in most countries and can interfere with
> aviation and emergency services. You are responsible for what you key up.
>
> Guardrails built in: authorization checkbox/flag required per TX, configurable
> frequency allow-list, hard max-duration auto-kill, always-visible STOP.

## Quick start (mock, no hardware)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional; defaults are mock-safe
.venv/bin/uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 — the badge reads **MOCK — no RF**.

## Configuration (`.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `RPITX_MODE` | `mock` | `mock` (no hardware) or `real` (spawn binaries) |
| `BIN_DIR` | `/opt/rpitx` | Directory holding rpitx binaries |
| `FREQ_ALLOWLIST` | `430000000-440000000` | Allowed Hz ranges, `low-high,low-high` |
| `MAX_TX_SECONDS` | `60` | Hard cap; watchdog kills any TX at this age |

## Modes

`tune`, `pifmrds`, `pocsag`, `sendiq`, `pisstv`, `pichirp`, `nbfm`. Definitions and
argv builders live in `app/tx/modes.py` — add a mode by adding a pydantic model +
builder.

`nbfm` (narrowband FM voice) has no dedicated rpitx binary: it runs the documented
`sox | csdr | sendiq` pipeline under `/bin/sh`. It needs `sox` and `csdr` on the host
(the NixOS module installs them). Lower `gain` = narrower deviation.

## Uploads

`POST /api/upload` (multipart `file`) stores an audio/image/IQ file under `UPLOAD_DIR`
and returns its host `path`; feed that back as `audio_file` / `image_file` / `iq_file`
when starting a TX. Client filenames are reduced to a safe basename inside the upload
dir — nothing can escape it — and an existing name is never overwritten (`clip-1.wav`).
`GET /api/uploads` lists what's stored.

File params **must** point at an existing file inside `UPLOAD_DIR`; anything else is a
`400`. The service runs as root on the Pi, so this is what stops `/etc/shadow` from being
handed to `sendiq`. `ALLOW_ANY_PATH=true` lifts the restriction if you really want it.

`pisstv` wants raw 320x256 RGB; any other image format is piped through ImageMagick
`convert` first (installed by the NixOS module), so upload a PNG/JPEG and go.

## TX process safety

One transmission at a time (rpitx owns the DMA/PLL hardware). Each TX gets a
generation id and a single supervisor task that (a) reaps the process the instant it
exits — no zombies, no hung carrier — and (b) kills it at the duration cap if it
overruns. The generation guard means a finished TX's watchdog can never tear down a
later one. Every child runs in its own session so a stop signals the whole process
group (killing all pipeline stages), and shutdown always stops an active TX.

## JSON API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/modes` | Mode list + JSON schema of each input |
| GET | `/api/status` | Current TX state |
| POST | `/api/tx/{mode}` | Start; body = mode fields + `"authorized": true` |
| POST | `/api/stop` | Stop active TX |
| POST | `/api/upload` | Multipart `file` → stores it, returns host `path` |
| GET | `/api/uploads` | List stored upload files |

```bash
curl -X POST localhost:8000/api/tx/tune \
  -H 'content-type: application/json' \
  -d '{"freq_hz":434000000,"max_seconds":5,"authorized":true}'
```

Responses: `202` started · `403` not authorized · `400` invalid/blocked freq ·
`409` already transmitting · `404` unknown mode.

## n8n node

Drive this API from [n8n](https://n8n.io) with the **`n8n-nodes-rpitx`** community
node: [CHA0S-CORP/n8n-nodes → packages/n8n-nodes-rpitx](https://github.com/CHA0S-CORP/n8n-nodes/tree/main/packages/n8n-nodes-rpitx).
It exposes status/modes, start/stop, dedicated Broadcast FM · Send POCSAG · Send SSTV
operations (with binary upload from a prior node), and a generic Start for any mode.

## Architecture

```
app/
  config.py          settings + frequency allow-list logic
  tx/modes.py        mode registry (pydantic model + argv builder)
  tx/backends.py     RealBackend (spawn rpitx) | MockBackend (sleep stand-in)
  tx/manager.py      single-slot lock, active-TX state, duration watchdog
  api/routes.py      JSON API
  web/routes.py      HTMX dashboard + partials
  templates/, static/
```

Only **one** transmission runs at a time — rpitx owns the DMA/PLL hardware exclusively.
`TxManager` enforces this; a start while busy returns `409`.

## Tests

```bash
.venv/bin/python -m pytest
```

Covers argv building per mode, allow-list rejection, single-slot `409`, and the
watchdog auto-kill — all on the mock backend, no hardware.

## Nix build (NixOS Pi image)

The flake builds rpitx from source, packages this dashboard, and bakes both into a
bootable SD image for a Raspberry Pi 3B.

```
flake.nix                 outputs: packages, nixosConfigurations.rpitx-pi3, .#sdImage
pkgs/rpitx.nix            builds F5OEO/rpitx + librpitx (aarch64)
pkgs/rpitx-dashboard.nix  wraps uvicorn app.main:app with its Python deps
modules/rpitx.nix         services.rpitx-dashboard NixOS module (systemd unit)
```

```bash
nix build .#rpitx-dashboard        # just the dashboard package
nix build .#sdImage                # full flashable Pi 3 image
```

### CI / releases

`.github/workflows/sd-image.yml` builds `.#sdImage` on GitHub Actions (x86 runners
build the aarch64 image via QEMU binfmt). It runs on manual dispatch (uploads the
`.img.zst` as a build artifact) and on `v*` tags (also publishes a GitHub Release with
the image attached). To cut a release:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

Module options (set in `flake.nix` under `services.rpitx-dashboard`):

| Option | Default | Maps to |
|--------|---------|---------|
| `mode` | `real` | `RPITX_MODE` |
| `port` / `host` | `8080` / `0.0.0.0` | uvicorn `--port` / `--host` |
| `freqAllowlist` | `430000000-440000000` | `FREQ_ALLOWLIST` |
| `maxTxSeconds` | `60` | `MAX_TX_SECONDS` |
| `openFirewall` | `true` | firewall rule |

`BIN_DIR` is wired to the built `rpitx` package automatically. The unit runs as root
(rpitx needs `/dev/mem` + DMA) — keep it on a trusted network.

## Deploy on the Pi (non-Nix)

1. Build/install rpitx so its binaries are in `BIN_DIR`.
2. `RPITX_MODE=real BIN_DIR=/opt/rpitx .venv/bin/uvicorn app.main:app --host 0.0.0.0`
   (rpitx needs root/GPIO — run accordingly).
3. Wire an antenna to GPIO4 (header pin 7). Badge now reads **LIVE — REAL TX**.
