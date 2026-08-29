{ lib
, python3
, makeWrapper
, stdenvNoCC
, rpitx
}:

# The dashboard is a FastAPI + HTMX app (app/main.py -> `app`) served by uvicorn.
# We build a Python environment with its runtime deps, copy the `app` package into
# the store, and wrap uvicorn so `rpitx-dashboard` just works. Extra CLI args (the
# systemd unit passes --host/--port) are forwarded by makeWrapper via "$@".
let
  py = python3.withPackages (ps: [
    ps.fastapi
    ps.uvicorn
    ps.jinja2
    ps.pydantic
    ps.pydantic-settings
    ps.python-multipart
  ]);
in
stdenvNoCC.mkDerivation {
  pname = "rpitx-dashboard";
  version = "0.1.0";

  # Package the app tree, excluding the dev virtualenv / caches so they never
  # land in the store. We only install the importable `app` package below.
  src = lib.cleanSourceWith {
    src = ../.;
    filter = path: type:
      let base = baseNameOf path;
      in base != ".venv" && base != "__pycache__" && base != ".git";
  };

  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/rpitx-dashboard
    cp -r app $out/share/rpitx-dashboard/app

    # uvicorn resolves `app.main:app` from --app-dir; templates/static are found
    # via Path(__file__), so the process CWD does not matter.
    makeWrapper ${py}/bin/uvicorn $out/bin/rpitx-dashboard \
      --add-flags "app.main:app --app-dir $out/share/rpitx-dashboard" \
      --prefix PATH : ${lib.makeBinPath [ rpitx ]}

    runHook postInstall
  '';

  meta = with lib; {
    description = "FastAPI + HTMX web control panel for rpitx transmissions";
    license = licenses.mit;
    platforms = platforms.linux;
    mainProgram = "rpitx-dashboard";
  };
}
