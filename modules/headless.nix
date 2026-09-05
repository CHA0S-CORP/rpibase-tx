{ config, lib, pkgs, ... }:

# Headless first-boot provisioning from files on the FAT boot partition.
#
# Two ways in, both read from /boot/firmware (the SD card's FAT partition, the
# one you can edit from any laptop):
#
#  1. Raspberry Pi Imager "OS customisation" (hostname, user/password, SSH keys,
#     Wi-Fi, timezone). Imager writes `firstrun.sh` for images with
#     init_format=systemd — that is what Imager 1.x assumes for any local .img,
#     and what the manifest we publish declares for Imager 2.x. The script
#     probes for /usr/lib/raspberrypi-sys-mods/imager_custom and
#     /usr/lib/userconf-pi/userconf; we ship NixOS-aware shims at those paths
#     so the stock script runs unmodified.
#
#  2. Plain files, Raspberry Pi OS style:
#       ssh                  accepted (sshd is always on)
#       userconf.txt         user:crypted-password  (openssl passwd -6)
#       authorized_keys      SSH public keys for the default user
#       wpa_supplicant.conf  raw wpa_supplicant config (Wi-Fi)
#       hostname             one line
#       network.txt          INTERFACE/ADDRESS/GATEWAY/DNS for a static IP
#       *.network            raw systemd-networkd units (any custom network setup)
#
# One-shot files (firstrun.sh, userconf.txt) are renamed *.applied after use.
# The rest are re-applied on every boot, so editing them on the card is the
# way to change Wi-Fi/network later.
let
  cfg = config.services.rpitx-headless;
  fw = cfg.firmwareDir;
  state = "/var/lib/rpitx-headless";
  wpaConf = "/etc/wpa_supplicant/imperative.conf";
  netDir = "/etc/systemd/network";
  netPrefix = "50-boot-";

  # /usr/lib/raspberrypi-sys-mods/imager_custom — the subset Imager's
  # firstrun.sh calls. Signatures follow RPi-Distro/raspberrypi-sys-mods.
  imagerCustom = pkgs.writeShellApplication {
    name = "imager_custom";
    runtimeInputs = with pkgs; [ coreutils gnused util-linux nettools getent ];
    text = ''
      STATE=${state}
      mkdir -p "$STATE"
      first_user() { getent passwd 1000 | cut -d: -f1; }
      first_home() { getent passwd 1000 | cut -d: -f6; }

      cmd=''${1:-}; shift || true
      case "$cmd" in
        set_hostname)
          echo "$1" > "$STATE/hostname"
          hostname "$1"
          ;;
        enable_ssh)
          # enable_ssh [-k|--key-only] [-p|--pass-auth] [-d|--disabled] [KEY...]
          # sshd is always enabled on this image; only the keys matter here.
          while [ $# -gt 0 ] && [ "''${1#-}" != "$1" ]; do shift; done
          if [ $# -gt 0 ]; then
            u=$(first_user); h=$(first_home)
            install -o "$u" -g users -m 700 -d "$h/.ssh"
            printf '%s\n' "$@" > "$h/.ssh/authorized_keys"
            chown "$u" "$h/.ssh/authorized_keys"; chmod 600 "$h/.ssh/authorized_keys"
          fi
          ;;
        set_wlan)
          # set_wlan [-h|--hidden] [-p|--plain] SSID [PASS [COUNTRY]]
          hidden=0; plain=0
          while [ $# -gt 0 ] && [ "''${1#-}" != "$1" ]; do
            case "$1" in -h|--hidden) hidden=1 ;; -p|--plain) plain=1 ;; esac; shift
          done
          ssid=$1; pass=''${2:-}; country=''${3:-}
          {
            [ -n "$country" ] && echo "country=$country"
            echo "ctrl_interface=DIR=/run/wpa_supplicant GROUP=wheel"
            echo "update_config=1"
            echo "network={"
            [ "$hidden" = 1 ] && echo "  scan_ssid=1"
            printf '  ssid="%s"\n' "$ssid"
            if [ -z "$pass" ]; then
              echo "  key_mgmt=NONE"
            else
              echo "  key_mgmt=WPA-PSK SAE"
              # Imager hands over a pre-derived 64-hex PSK (unquoted in wpa
              # syntax); -p / anything else is a passphrase (quoted).
              if [ "$plain" = 0 ] && [ "''${#pass}" = 64 ]; then
                echo "  psk=$pass"
              else
                printf '  psk="%s"\n' "$pass"
              fi
              echo "  ieee80211w=1"
            fi
            echo "}"
          } > "$STATE/wpa.conf"
          install -m 600 "$STATE/wpa.conf" ${wpaConf}
          rfkill unblock wifi || true
          ;;
        set_timezone)
          if [ -e "/etc/zoneinfo/$1" ]; then ln -sfn "/etc/zoneinfo/$1" /etc/localtime; fi
          ;;
        set_keymap)
          echo "imager_custom: set_keymap ignored (headless image)" >&2
          ;;
        *)
          echo "imager_custom: unsupported command '$cmd'" >&2
          ;;
      esac
    '';
  };

  # /usr/lib/userconf-pi/userconf [FIRSTUSER] NEWNAME CRYPTED_PASS
  # Users are mutable on this image (NixOS mutableUsers), so chpasswd persists.
  # The declared default user is never renamed (NixOS would recreate it at the
  # next activation); a different requested name becomes an additional wheel
  # user carrying over any SSH keys already installed by enable_ssh.
  userconf = pkgs.writeShellApplication {
    name = "userconf";
    runtimeInputs = with pkgs; [ coreutils shadow getent ];
    text = ''
      if [ $# -eq 3 ]; then first=$1; shift; else first=$(getent passwd 1000 | cut -d: -f1); fi
      new=$1; pass=''${2:-}
      if ! getent passwd "$new" >/dev/null; then
        useradd -m -G wheel -s /run/current-system/sw/bin/bash "$new"
        fh=$(getent passwd "$first" | cut -d: -f6); nh=$(getent passwd "$new" | cut -d: -f6)
        if [ -f "$fh/.ssh/authorized_keys" ]; then
          install -o "$new" -g users -m 700 -d "$nh/.ssh"
          install -o "$new" -g users -m 600 "$fh/.ssh/authorized_keys" "$nh/.ssh/authorized_keys"
        fi
      fi
      if [ -n "$pass" ]; then echo "$new:$pass" | chpasswd -e; fi
    '';
  };

  apply = pkgs.writeShellApplication {
    name = "rpitx-headless-apply";
    runtimeInputs = with pkgs; [ coreutils gnused util-linux nettools getent mkpasswd bash imagerCustom userconf ];
    text = ''
      FW=${fw}
      STATE=${state}
      mkdir -p "$STATE" /etc/wpa_supplicant ${netDir}

      if ! mountpoint -q "$FW"; then mount "$FW" 2>/dev/null || true; fi
      if ! mountpoint -q "$FW"; then
        echo "headless: $FW not mounted, nothing to apply"
      else
        # --- Raspberry Pi Imager: firstrun.sh (systemd init_format) ----------
        if [ -f "$FW/firstrun.sh" ]; then
          echo "headless: running Raspberry Pi Imager firstrun.sh"
          bash "$FW/firstrun.sh" || echo "headless: firstrun.sh exited $?"
          [ -f "$FW/firstrun.sh" ] && mv -f "$FW/firstrun.sh" "$FW/firstrun.sh.applied"
        fi

        # --- Plain files --------------------------------------------------------
        [ -f "$FW/ssh" ] && echo "headless: 'ssh' present (sshd is always enabled)"

        if [ -f "$FW/userconf.txt" ]; then
          line=$(head -n1 "$FW/userconf.txt" | tr -d '\r')
          u=''${line%%:*}; p=''${line#*:}
          if [ -n "$u" ] && [ -n "$p" ]; then
            case "$p" in
              '$'*) userconf "$u" "$p" ;;
              *)    userconf "$u" "$(printf '%s' "$p" | mkpasswd -m sha-512 -s)" ;;
            esac
            echo "headless: applied userconf.txt for $u"
          fi
          mv -f "$FW/userconf.txt" "$FW/userconf.txt.applied"
        fi

        if [ -f "$FW/wpa_supplicant.conf" ]; then
          tr -d '\r' < "$FW/wpa_supplicant.conf" > "$STATE/wpa.conf"
          install -m 600 "$STATE/wpa.conf" ${wpaConf}
          rfkill unblock wifi || true
          echo "headless: installed wpa_supplicant.conf"
        fi

        if [ -f "$FW/authorized_keys" ]; then
          h=$(getent passwd ${cfg.user} | cut -d: -f6)
          install -o ${cfg.user} -g users -m 700 -d "$h/.ssh"
          tr -d '\r' < "$FW/authorized_keys" > "$h/.ssh/authorized_keys"
          chown ${cfg.user} "$h/.ssh/authorized_keys"; chmod 600 "$h/.ssh/authorized_keys"
          echo "headless: installed authorized_keys for ${cfg.user}"
        fi

        if [ -f "$FW/hostname" ]; then
          head -n1 "$FW/hostname" | tr -d ' \t\r' > "$STATE/hostname"
        fi

        # Custom network config: raw networkd units and/or a simple static IP.
        rm -f ${netDir}/${netPrefix}*.network
        for f in "$FW"/*.network; do
          [ -f "$f" ] || continue
          dest="${netDir}/${netPrefix}$(basename "$f")"
          tr -d '\r' < "$f" > "$dest"
          echo "headless: installed $(basename "$f")"
        done
        if [ -f "$FW/network.txt" ]; then
          INTERFACE=eth0; ADDRESS=""; GATEWAY=""; DNS=""
          while IFS='=' read -r k v; do
            k=$(echo "$k" | tr -d ' \t\r'); v=$(echo "$v" | sed 's/^[ \t]*//;s/[ \t\r]*$//')
            case "$k" in
              INTERFACE) INTERFACE=$v ;; ADDRESS) ADDRESS=$v ;;
              GATEWAY) GATEWAY=$v ;; DNS) DNS=$v ;;
            esac
          done < "$FW/network.txt"
          if [ -n "$ADDRESS" ]; then
            {
              echo "[Match]"; echo "Name=$INTERFACE"; echo
              echo "[Network]"
              for a in $ADDRESS; do echo "Address=$a"; done
              [ -n "$GATEWAY" ] && echo "Gateway=$GATEWAY"
              for d in $DNS; do echo "DNS=$d"; done
            } > "${netDir}/${netPrefix}static.network"
            echo "headless: static $ADDRESS on $INTERFACE from network.txt"
          fi
        fi
      fi

      # Persisted hostname (from either path) is re-applied each boot; NixOS
      # owns /etc/hostname, so it lives in state and is set transiently.
      if [ -s "$STATE/hostname" ]; then hostname "$(cat "$STATE/hostname")"; fi
    '';
  };
in
{
  options.services.rpitx-headless = {
    enable = lib.mkEnableOption "headless provisioning from the FAT boot partition";

    firmwareDir = lib.mkOption {
      type = lib.types.str;
      default = "/boot/firmware";
      description = "Mount point of the SD card's FAT partition.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "pi";
      description = "Default user that receives authorized_keys / userconf.txt.";
    };
  };

  config = lib.mkIf cfg.enable {
    # The declared default user is uid 1000 so Imager's script (getent passwd
    # 1000) and the shims agree on who the "first user" is.
    users.users.${cfg.user}.uid = lib.mkDefault 1000;
    users.mutableUsers = true;
    services.openssh.enable = true;

    # Wi-Fi: wpa_supplicant with the imperative config file the shims write.
    networking.wireless = {
      enable = true;
      allowAuxiliaryImperativeNetworks = true;
    };

    # networkd so dropped-in *.network files (and network.txt) just work; the
    # NixOS-generated 99-*-dhcp units stay as the fallback for everything else.
    networking.useNetworkd = true;
    networking.useDHCP = lib.mkDefault true;
    systemd.network.enable = true;

    # Imager's firstrun.sh probes these exact paths.
    systemd.tmpfiles.rules = [
      "d /usr/lib/raspberrypi-sys-mods 0755 root root -"
      "L+ /usr/lib/raspberrypi-sys-mods/imager_custom - - - - ${imagerCustom}/bin/imager_custom"
      "d /usr/lib/userconf-pi 0755 root root -"
      "L+ /usr/lib/userconf-pi/userconf - - - - ${userconf}/bin/userconf"
    ];

    systemd.services.rpitx-headless = {
      description = "Apply headless setup files from the boot partition";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-pre.target" ];
      before = [
        "network-pre.target"
        "systemd-networkd.service"
        "wpa_supplicant.service"
        "sshd.service"
      ];
      after = [ "systemd-tmpfiles-setup.service" ];
      unitConfig.RequiresMountsFor = fw;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = lib.getExe apply;
      };
    };
  };
}
