{ lib
, stdenv
, fetchFromGitHub
, libsndfile
, fftw
, fftwFloat
, libraspberrypi
}:

# librpitx is a hard build dependency of rpitx. It is a separate repo that the
# upstream install.sh clones into src/librpitx and `make install`s system-wide.
# We build it here and stage it into a temporary prefix that the rpitx apps link
# against via -lrpitx.
let
  librpitxSrc = fetchFromGitHub {
    owner = "F5OEO";
    repo = "librpitx";
    rev = "f01bdb64bcdb6207f448379193bc0a8accb9aa22";
    hash = "sha256-8OjsJISdRTZWesWtCGUcM2CwJvTVVilU99HHm7ylYDY=";
  };
in
stdenv.mkDerivation (finalAttrs: {
  pname = "rpitx";
  version = "unstable-2026-08-29";

  src = fetchFromGitHub {
    owner = "F5OEO";
    repo = "rpitx";
    rev = "ee7ff57b77962536fda4daa523749c04af6beec7";
    hash = "sha256-YY/KKoPQIyjJkuTp9uwt1PIBURLtE/py85mXJREbSPA=";
  };

  # libbcm_host comes from libraspberrypi (the raspberrypi/userland fork).
  # The upstream Makefiles hardcode -I/opt/vc/include and -L/opt/vc/lib; those
  # dirs simply do not exist here, so we append the real paths on top. The dead
  # /opt/vc flags are harmless.
  buildInputs = [ libsndfile fftw fftwFloat libraspberrypi ];

  # dvbrf/pift8/pissb/freedv are intentionally skipped:
  #   dvbrf  -> ships 32-bit ARM (.s) assembly, does not assemble on aarch64
  #   pift8  -> needs F5OEO/ft8_lib (-lft8)
  #   pissb  -> needs liquid-dsp (-lliquid)
  #   freedv -> needs codec2
  # Everything below only needs librpitx + libsndfile + libbcm_host.
  targets = [
    "../tune"
    "../morse"
    "../pichirp"
    "../sendiq"
    "../sendook"
    "../pocsag"
    "../pifmrds"
    "../spectrumpaint"
    "../pisstv"
    "../pirtty"
    "../pifsq"
    "../piopera"
    "../foxhunt"
    "../corel8"
    "../rpitx"
    # pifm/piam/pidcf77 are omitted: their Makefile recipes reference stale
    # ../fm ../am ../dcf77 paths that don't exist in this revision.
  ];

  buildPhase = ''
    runHook preBuild

    # librpitx is cloned into src/librpitx by upstream install.sh; stage it here.
    # Done in buildPhase (cwd == source root) to avoid relying on $sourceRoot,
    # which is empty under structuredAttrs.
    rm -rf src/librpitx
    cp -r ${librpitxSrc} src/librpitx
    chmod -R u+w src/librpitx

    export VCINC="${libraspberrypi}/include"
    export VCLIB="${libraspberrypi}/lib"

    echo "==> building librpitx"
    make -C src/librpitx/src \
      CXXFLAGS="-std=c++11 -Wall -O3 -Wno-unused-variable -fPIC -I$VCINC" \
      LDFLAGS="-lm -lrt -lpthread -L$VCLIB -lbcm_host -fPIC"

    # Upstream `make install`s librpitx into /usr/local so the apps can
    # #include <librpitx/librpitx.h> and link -lrpitx. Mirror that into a local
    # staging prefix and point the app build's -I/-L at it.
    export RPITX_PREFIX="$PWD/librpitx-prefix"
    make -C src/librpitx/src install PREFIX="$RPITX_PREFIX"

    # Several app recipes (pifmrds, ...) hardcode -L/opt/vc/lib and omit
    # -lbcm_host. Feed the real search paths + bcm_host through NIX_LDFLAGS so
    # every link resolves regardless of the recipe's own flags. NIX_LDFLAGS libs
    # are appended after the objects, satisfying librpitx.a's bcm_host refs.
    export NIX_LDFLAGS="$NIX_LDFLAGS -L$RPITX_PREFIX/lib -L$VCLIB -lbcm_host"

    echo "==> building rpitx tools"
    make -C src ${toString finalAttrs.targets} \
      CFLAGS="-Wall -O2 -Wno-unused-variable -I$VCINC -I$RPITX_PREFIX/include" \
      CXXFLAGS="-std=c++11 -Wall -O2 -Wno-unused-variable -I$VCINC -I$RPITX_PREFIX/include" \
      LDFLAGS="-L$RPITX_PREFIX/lib -L$VCLIB -lrpitx -lbcm_host -lm -lrt -lpthread"

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    mkdir -p $out/bin $out/share/rpitx
    for t in ${toString finalAttrs.targets}; do
      b=$(basename "$t")
      if [ -x "$b" ]; then
        install -m0755 "$b" "$out/bin/$b"
      else
        echo "WARN: expected binary $b was not built" >&2
      fi
    done

    # Ship the demo/test shell scripts + resources for reference.
    cp -r *.sh src/resources $out/share/rpitx/ 2>/dev/null || true

    runHook postInstall
  '';

  meta = with lib; {
    description = "General-purpose radio frequency transmitter for Raspberry Pi (GPIO4)";
    homepage = "https://github.com/F5OEO/rpitx";
    license = licenses.gpl2Plus;
    platforms = platforms.linux;
    # Uses /dev/mem + Broadcom DMA/clock peripherals: Raspberry Pi only.
    mainProgram = "rpitx";
  };
})
