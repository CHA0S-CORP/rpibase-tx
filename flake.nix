{
  description = "NixOS SD image for a Raspberry Pi 3B running rpitx + a web control dashboard";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    nixos-hardware.url = "github:NixOS/nixos-hardware";
  };

  outputs = { self, nixpkgs, nixos-hardware, ... }:
    let
      system = "aarch64-linux";

      # Overlay exposing our two packages on any nixpkgs instance.
      overlay = final: prev: {
        rpitx = final.callPackage ./pkgs/rpitx.nix { };
        rpitx-dashboard = final.callPackage ./pkgs/rpitx-dashboard.nix { };
      };

      pkgs = import nixpkgs {
        inherit system;
        overlays = [ overlay ];
      };
    in
    {
      overlays.default = overlay;

      packages.${system} = {
        inherit (pkgs) rpitx rpitx-dashboard;
        default = pkgs.rpitx-dashboard;
        # `nix build .#sdImage`
        sdImage = self.nixosConfigurations.rpitx-pi3.config.system.build.sdImage;
      };

      nixosConfigurations.rpitx-pi3 = nixpkgs.lib.nixosSystem {
        inherit system;
        modules = [
          nixos-hardware.nixosModules.raspberry-pi-3
          "${nixpkgs}/nixos/modules/installer/sd-card/sd-image-aarch64.nix"
          ./modules/rpitx.nix
          ({ config, lib, pkgs, ... }: {
            nixpkgs.overlays = [ overlay ];

            networking.hostName = "rpitx";

            # rpitx needs a stable clock: pin the GPU to 250 MHz and disable
            # dynamic frequency scaling, mirroring upstream install.sh. These
            # lines are appended after the stock sections, so scope them [all].
            sdImage.populateFirmwareCommands = lib.mkAfter ''
              printf '\n[all]\n# rpitx: stable clock for the DMA RF carrier generator\ngpu_freq=250\nforce_turbo=1\n' >> firmware/config.txt
            '';

            services.rpitx-dashboard = {
              enable = true;
              port = 8080;
              openFirewall = true;
              mode = "real"; # "mock" = no RF, for bring-up testing
              # EDIT to your licence/region before transmitting:
              freqAllowlist = "430000000-440000000";
              maxTxSeconds = 60;
            };

            # --- Access -----------------------------------------------------
            services.openssh.enable = true;
            users.users.pi = {
              isNormalUser = true;
              extraGroups = [ "wheel" ];
              # CHANGE THIS or set hashedPassword / openssh authorized keys.
              initialPassword = "raspberry";
            };
            security.sudo.wheelNeedsPassword = false;

            # Wi-Fi (optional): fill in and uncomment.
            # networking.wireless = {
            #   enable = true;
            #   networks."YOUR_SSID".psk = "YOUR_PASSWORD";
            # };

            # Shrink the image a bit; drop if you want docs/manpages.
            documentation.enable = false;

            system.stateVersion = "24.11";
          })
        ];
      };
    };
}
