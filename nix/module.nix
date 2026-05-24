# NixOS home-manager module for wine-sni-bridge
#
# Usage in your home-manager config:
#   imports = [ ./path/to/wine-sni-bridge/nix/module.nix ];
#   services.wine-sni-bridge.enable = true;
#
# Options:
#   services.wine-sni-bridge.enable     — enable the systemd user service
#   services.wine-sni-bridge.byteOrder  — "native" (default) or "network".
#     See wine-sni-bridge.py module docstring for why "native" is correct
#     for every known Qt/Cairo-based SNI host.
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.wine-sni-bridge;
  bridgePython = pkgs.python3.withPackages (ps: [
    ps.xlib
    ps.dbus-python
    ps.pygobject3
  ]);
in {
  options.services.wine-sni-bridge = {
    enable = lib.mkEnableOption "Wine SNI Bridge - X11 tray to StatusNotifierItem for Wayland";

    script = lib.mkOption {
      type = lib.types.path;
      default = ../wine-sni-bridge.py;
      description = "Path to the wine-sni-bridge.py script";
    };

    byteOrder = lib.mkOption {
      type = lib.types.enum ["native" "network"];
      default = "native";
      description = ''
        IconPixmap packing byte order. "native" (default) matches the
        in-memory layout Qt QImage::Format_ARGB32 and Cairo ARGB32 read on
        little-endian hosts — colors land correctly in every known SNI
        host. "network" follows the DBus SNI spec literally (big-endian
        ARGB); use only if a host requires it.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.user.services.wine-sni-bridge = {
      Unit = {
        Description = "Wine SNI Bridge - X11 tray to StatusNotifierItem";
      };
      Service = {
        ExecStart = "${bridgePython}/bin/python3 ${cfg.script} --byte-order ${cfg.byteOrder}";
        Restart = "on-failure";
        RestartSec = 5;
        # Belt-and-suspenders: the bridge itself fails fast on dead X11,
        # but if a different leak ever creeps in, cap it instead of
        # letting it eat gigabytes of RAM unattended.
        MemoryMax = "256M";
      };
      Install = {
        WantedBy = ["default.target"];
      };
    };
  };
}
