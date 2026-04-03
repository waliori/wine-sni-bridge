# Contributing

Contributions are welcome! Here's how you can help:

## Compositor Configs

The dwl/MangoWC config is "battle-tested". Hyprland and Sway configs are based on docs but untested. If you use a different compositor and have working rules, please submit a PR adding your config to `examples/`.

## Bug Reports

When filing issues, please include:
- Your compositor (Sway, Hyprland, dwl, etc.) and version
- Waybar version
- Wine version
- Output of `journalctl --user -u wine-sni-bridge --no-pager -n 50`
- Steps to reproduce

## Testing

Test your changes with:
1. A Steam/Proton game that minimizes to tray (e.g. Black Desert Online)
2. Multiple Wine apps simultaneously
3. Waybar reload (`pkill waybar && waybar &`)
4. Compositor reload
