# NixOS home-manager module for wine-sni-bridge
#
# Usage in your home-manager config:
#   imports = [ ./path/to/wine-sni-bridge/nix/module.nix ];
#   services.wine-sni-bridge.enable = true;
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
  };

  config = lib.mkIf cfg.enable {
    systemd.user.services.wine-sni-bridge = {
      Unit = {
        Description = "Wine SNI Bridge - X11 tray to StatusNotifierItem";
      };
      Service = {
        ExecStart = "${bridgePython}/bin/python3 ${cfg.script}";
        Restart = "on-failure";
        RestartSec = 5;
      };
      Install = {
        WantedBy = [ "default.target" ];
      };
    };
  };
}
