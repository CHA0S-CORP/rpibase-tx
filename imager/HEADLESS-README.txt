rpibase-tx — headless setup from this boot partition
=====================================================

Everything here is read on every boot by the rpitx-headless service.
SSH is always enabled. Default login: pi / raspberry (change it!).
The dashboard is on http://<pi>:8080.

OPTION A — Raspberry Pi Imager
------------------------------
Use Imager's "OS customisation" (hostname, username/password, SSH key,
Wi-Fi, timezone). Imager writes firstrun.sh here; it is applied on first
boot and renamed firstrun.sh.applied.

  Imager 1.x: works out of the box with the local .img.zst file.
  Imager 2.x: local images are not customisable unless a manifest says so.
              Run it with the os_list.json shipped next to the release:
                rpi-imager --repo https://github.com/CHA0S-CORP/rpibase-tx/releases/download/<TAG>/os_list.json
              then pick "rpibase-tx" from the list.

Notes: the username you enter becomes an additional sudo user (pi stays);
keyboard layout is ignored (no console needed).

OPTION B — plain files (Raspberry Pi OS conventions)
---------------------------------------------------
ssh                  optional, accepted (sshd is always on)
userconf.txt         user:password   password may be plain text or a crypt
                     hash (openssl passwd -6). Renamed *.applied after use.
authorized_keys      SSH public keys for user pi
wpa_supplicant.conf  Wi-Fi, standard wpa_supplicant syntax, e.g.
                       country=US
                       network={
                         ssid="MyNet"
                         psk="secret"
                       }
hostname             one line
network.txt          static IP instead of DHCP:
                       INTERFACE=eth0          (default eth0; wlan0 works)
                       ADDRESS=192.168.1.50/24
                       GATEWAY=192.168.1.1
                       DNS=192.168.1.1 1.1.1.1
*.network            raw systemd-networkd units for anything fancier
                     (VLANs, bonds, multiple addresses). They are copied to
                     /etc/systemd/network/50-boot-<name> and win over the
                     built-in DHCP defaults.

wpa_supplicant.conf, authorized_keys, hostname, network.txt and *.network
are re-applied on every boot: edit them here to change the config later.
