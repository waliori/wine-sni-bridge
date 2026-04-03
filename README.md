# wine-sni-bridge

A lightweight daemon that bridges Wine/Windows system tray icons to Wayland's StatusNotifierItem protocol. Drop-in replacement for `xembedsniproxy` that doesn't steal keyboard focus.

## The Problem

On Wayland, Wine applications that use the system tray (minimize-to-tray games, background apps) rely on the X11 XEmbed protocol. The standard solution, `xembedsniproxy` from KDE Plasma, converts these to the modern StatusNotifierItem (SNI) protocol for status bars like Waybar.

**But xembedsniproxy has a critical flaw on wlroots-based compositors**: it creates `override_redirect` (unmanaged) X11 windows that steal keyboard focus. Once focus is grabbed, you can't type, use shortcuts, or interact with any window until you open and close something like `rofi` to reset the focus chain. This is a [known issue](https://github.com/swaywm/sway/issues/7130) across Sway, Hyprland, dwl, and other wlroots compositors.

There is no upstream fix. Wine's [native SNI support](https://gitlab.winehq.org/wine/wine/-/merge_requests/2808) has been pending since 2023 and hasn't been merged.

**wine-sni-bridge** solves this by:
- Using a **managed** X11 window (not `override_redirect`) that compositors can control
- Setting `_NET_WM_WINDOW_TYPE_UTILITY` so it's excluded from alt-tab and overview
- Never requesting keyboard focus
- Letting compositor window rules hide it completely

## How It Works

```
Wine App (minimize to tray)
    |
    v
X11 XEmbed Protocol (Shell_NotifyIcon)
    |
    v
wine-sni-bridge.py (claims _NET_SYSTEM_TRAY_S0)
    |  - Embeds icon windows in an offscreen container
    |  - Extracts icon pixels via CopyArea
    |  - Crops and scales to 48x48
    |  - Makes black background transparent
    |
    v
DBus StatusNotifierItem (per-icon bus connection)
    |
    v
Waybar / Any SNI-compatible status bar
```

## Features

- **No focus stealing** - managed utility window, compositor-controlled
- **Multi-app support** - each Wine app gets its own SNI item (separate DBus connections)
- **Icon caching** - remembers icons per WM_CLASS, no white flash on re-minimize
- **Click forwarding** - left/middle/right clicks in the status bar forwarded to Wine
- **Waybar reload survival** - watches for StatusNotifierWatcher restarts, re-registers automatically
- **Stale icon cleanup** - auto-undocks icons when X11 windows are destroyed
- **Systemd integration** - runs as a user service, auto-restarts on failure
- **Single file** - ~580 lines of Python, no build system needed

## Dependencies

- Python 3.10+
- [python-xlib](https://github.com/python-xlib/python-xlib) - X11 protocol
- [dbus-python](https://dbus.freedesktop.org/doc/dbus-python/) - DBus interface
- [PyGObject](https://pygobject.gnome.org/) - GLib main loop

### Install dependencies

**Arch Linux:**
```bash
pacman -S python-xlib python-dbus python-gobject
```

**Fedora:**
```bash
dnf install python3-xlib python3-dbus python3-gobject
```

**Debian/Ubuntu:**
```bash
apt install python3-xlib python3-dbus python3-gi
```

**NixOS:** See [NixOS Module](#nixos-module) below.

## Installation

```bash
# Clone
git clone https://github.com/waliori/wine-sni-bridge.git
cd wine-sni-bridge

# Install
cp wine-sni-bridge.py ~/.local/bin/
chmod +x ~/.local/bin/wine-sni-bridge.py

# Install systemd service
cp wine-sni-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wine-sni-bridge
```

## Compositor Setup (Required)

> **WARNING**: Without compositor rules, the bridge's X11 container window will be visible as a small floating window on your desktop. You **must** add window rules for your compositor.

The bridge window uses:
- **WM_CLASS**: `wine-sni-bridge`
- **Window type**: `_NET_WM_WINDOW_TYPE_UTILITY`

### Hyprland

Add to `hyprland.conf`:

```conf
windowrulev2 = float, class:wine-sni-bridge
windowrulev2 = nofocus, class:wine-sni-bridge
windowrulev2 = opacity 0.0 override 0.0 override, class:wine-sni-bridge
windowrulev2 = noborder, class:wine-sni-bridge
windowrulev2 = noshadow, class:wine-sni-bridge
windowrulev2 = noblur, class:wine-sni-bridge
windowrulev2 = noanimations, class:wine-sni-bridge
```

### Sway

Add to `config`:

```conf
for_window [app_id="wine-sni-bridge"] {
    floating enable
    move position -9999 -9999
    no_focus
    border none
    opacity 0
}
```

### dwl / MangoWC

Add to your config:

```conf
windowrule=nofocus:1,appid:wine-sni-bridge
windowrule=isfloating:1,appid:wine-sni-bridge
windowrule=isoverlay:1,appid:wine-sni-bridge
windowrule=focused_opacity:0.0,appid:wine-sni-bridge
windowrule=unfocused_opacity:0.0,appid:wine-sni-bridge
windowrule=isnoborder:1,appid:wine-sni-bridge
windowrule=isnoshadow:1,appid:wine-sni-bridge
windowrule=isnoanimation:1,appid:wine-sni-bridge
windowrule=noblur:1,appid:wine-sni-bridge
```

### Other wlroots compositors

The intent of the rules is:
1. **Float** the window (don't tile it)
2. **No focus** (never give it keyboard input)
3. **Zero opacity** (invisible)
4. **No decorations** (no border, shadow, blur, animations)

Adapt to your compositor's rule syntax using `app_id` / `class` = `wine-sni-bridge`.

## Waybar Configuration

Add `"tray"` to your modules and configure it:

```jsonc
{
    "modules-right": ["tray", ...],

    "tray": {
        "spacing": 10,
        "show-passive-items": true,
        "icon-size": 16
    },

    // Hide bridge from taskbar
    "wlr/taskbar": {
        "ignore-list": ["wine-sni-bridge"]
    }
}
```

## Wine Registry (Recommended)

Disable Wine's `WM_TAKE_FOCUS` protocol to prevent residual focus issues:

```bash
wine reg add 'HKEY_CURRENT_USER\Software\Wine\X11 Driver' /t REG_SZ /v UseTakeFocus /d N /f
```

Apply to each Wine prefix you use (including Proton prefixes if needed).

## NixOS Module

For NixOS with home-manager:

```nix
# In your home-manager config
imports = [ ./path/to/wine-sni-bridge/nix/module.nix ];

services.wine-sni-bridge.enable = true;
```

The module automatically creates the systemd service with the correct Python environment.

## Troubleshooting

**Icons not appearing in Waybar:**
- Check that StatusNotifierWatcher is running: `busctl --user list | grep StatusNotifier`
- Verify Waybar has the `tray` module configured
- Check bridge logs: `journalctl --user -u wine-sni-bridge -f`

**Bridge window visible on desktop:**
- Compositor rules not applied. Check your compositor config matches `wine-sni-bridge` (the WM_CLASS)
- Some compositors need a reload after adding rules

**White/blank icon on second minimize:**
- This is usually resolved by icon caching. If persistent, restart the bridge: `systemctl --user restart wine-sni-bridge`

**Black square on first minimize:**
- Known Wine behavior. The icon appears correctly after the first dock/undock cycle

**Bridge visible in compositor overview/expose:**
- The window sets `_NET_WM_WINDOW_TYPE_UTILITY` which most compositors exclude from overview
- If your compositor still shows it, add compositor-specific rules to exclude utility windows

**Focus still stolen after installing:**
- Make sure `xembedsniproxy` is **not** running: `pkill -f xembedsniproxy`
- Disable the systemd service if it exists: `systemctl --user disable --now xembed-sni-proxy`
- Apply the Wine registry tweak above

## Known Limitations

- First minimize of a Wine app may show a brief black square (icon not yet painted by Wine)
- Some compositors may show the utility window in overview/expose modes
- Only supports `_NET_SYSTEM_TRAY_S0` (primary screen)
- Icon extraction uses polling (50ms X11 event loop) - negligible CPU impact
- Wine apps in the default `~/.wine` prefix may show Wine's built-in tray bar alongside bridge icons (Proton/Steam games don't have this issue)

## Comparison with xembedsniproxy

| | xembedsniproxy | wine-sni-bridge |
|---|---|---|
| Focus stealing | Yes (unmanaged X11 windows) | No (managed utility window) |
| Black square artifacts | Yes | Minimal (first minimize only) |
| Multi-app support | Yes | Yes (separate DBus connections) |
| Icon caching | No | Yes (per WM_CLASS) |
| Click forwarding | Yes | Yes |
| Survives bar restart | No | Yes (auto re-registers) |
| Dependencies | KDE Frameworks | Python + 3 packages |
| Wayland native | Partial | Yes (pure DBus SNI) |

## How it was built

This project was created to solve the xembedsniproxy focus-stealing problem on [MangoWC](https://github.com/DreamMaoMao/mango) (a dwl-based Wayland compositor). After extensive research confirmed that no existing solution works, we built wine-sni-bridge from scratch as a purpose-built X11-to-SNI bridge that respects Wayland compositor focus management.

## License

MIT License. See [LICENSE](LICENSE).
