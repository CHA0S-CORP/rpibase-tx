{ config, lib, pkgs, ... }:

let
  cfg = config.services.rpitx-dashboard;
in
{
  options.services.rpitx-dashboard = {
    enable = lib.mkEnableOption "rpitx web control dashboard";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.rpitx-dashboard;
      description = "The rpitx-dashboard package to run.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "0.0.0.0";
      description = "Bind address for the dashboard HTTP server.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8080;
      description = "TCP port for the dashboard.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Open {option}`port` in the firewall.";
    };

    mode = lib.mkOption {
      type = lib.types.enum [ "mock" "real" ];
      default = "real";
      description = ''
        TX backend. `real` spawns the actual rpitx binaries on GPIO4; `mock`
        runs a harmless stand-in process (no RF) for testing the service.
      '';
    };

    freqAllowlist = lib.mkOption {
      type = lib.types.str;
      default = "430000000-440000000";
      example = "430000000-440000000,902000000-928000000";
      description = ''
        Comma-separated allowed frequency ranges in Hz (`low-high,low-high`).
        Any transmit request outside these ranges is rejected. Set this to what
        your licence/region actually permits.
      '';
    };

    maxTxSeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 60;
      description = "Hard cap in seconds; a watchdog auto-kills any longer TX.";
    };
  };

  config = lib.mkIf cfg.enable {
    # sox + csdr are the modulation stages of the NBFM pipeline; PATH includes
    # BIN_DIR (rpitx) plus these so `nbfm` resolves every stage at runtime.
    environment.systemPackages = [ pkgs.rpitx cfg.package pkgs.sox pkgs.csdr ];

    systemd.services.rpitx-dashboard = {
      description = "rpitx web control dashboard";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      serviceConfig = {
        # rpitx maps /dev/mem and drives Broadcom DMA/clock peripherals, which
        # requires root. The dashboard spawns those tools, so it runs as root
        # too. Keep it on a trusted network (and behind the firewall/token).
        ExecStart = "${lib.getExe cfg.package} --host ${cfg.host} --port ${toString cfg.port}";
        Restart = "on-failure";
        RestartSec = 3;
        StateDirectory = "rpitx-dashboard";
        RuntimeDirectory = "rpitx-dashboard";
        # Kill the whole control group on stop/restart. rpitx children run in
        # their own session (to allow group-kill on a normal stop); this ensures
        # a hard stop/crash-restart still tears down any transmitting child so
        # the carrier can never be left keyed.
        KillMode = "control-group";
        Environment = [
          "RPITX_MODE=${cfg.mode}"
          "BIN_DIR=${pkgs.rpitx}/bin"
          "FREQ_ALLOWLIST=${cfg.freqAllowlist}"
          "MAX_TX_SECONDS=${toString cfg.maxTxSeconds}"
          # StateDirectory=rpitx-dashboard resolves to /var/lib/rpitx-dashboard.
          "UPLOAD_DIR=/var/lib/rpitx-dashboard/uploads"
          # Host-global single-transmitter lock (RuntimeDirectory -> /run).
          "TX_LOCK_FILE=/run/rpitx-dashboard/tx.lock"
        ];
      };
    };

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}
