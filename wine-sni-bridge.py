#!/usr/bin/env python3
"""
Wine SNI Bridge - X11 System Tray to StatusNotifierItem bridge for Wayland.
Replaces xembedsniproxy without creating focus-stealing unmanaged X11 windows.

Byte order note
---------------
The DBus StatusNotifierItem spec describes IconPixmap as "ARGB32 in network
byte order" (big-endian A,R,G,B bytes). In practice, every known SNI host
(waybar, Quickshell, KDE Plasma, fcitx5) constructs images via Qt's
QImage::Format_ARGB32 or Cairo's ARGB32 directly from the DBus byte array,
and those formats read memory in native byte order — which on x86_64 LE is
B,G,R,A. So native-endian packing is what hosts actually render correctly.
Use --byte-order=network only if you encounter a host that truly reads the
spec literally and shows color-swapped icons with the default.
"""

import argparse
import os
import sys
import struct
import signal
import time

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib
from Xlib import X, display, Xatom, protocol, error
from Xlib.error import ConnectionClosedError

SNI_WATCHER_BUS = "org.kde.StatusNotifierWatcher"
SNI_WATCHER_PATH = "/StatusNotifierWatcher"
SNI_ITEM_IFACE = "org.kde.StatusNotifierItem"

SYSTEM_TRAY_REQUEST_DOCK = 0
XEMBED_EMBEDDED_NOTIFY = 0
XEMBED_VERSION = 0


def log(msg):
    print(f"[bridge] {msg}", flush=True)


class SNIItem(dbus.service.Object):
    """Persistent SNI item that can be reused across dock/undock cycles."""

    def __init__(self, bus_name, path, bridge):
        self._bridge = bridge
        self._icon_xid = 0
        self._icon_data = []
        self._title = "Wine App"
        self._active = False
        dbus.service.Object.__init__(self, bus_name, path)

    def bind(self, icon_xid):
        """Bind to a new tray icon window."""
        self._icon_xid = icon_xid
        self._icon_data = []
        self._title = "Wine App"
        self._active = True
        self.NewStatus("Active")

    def unbind(self):
        """Unbind from current icon, go passive."""
        self._active = False
        self._icon_xid = 0
        self.NewStatus("Passive")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        if iface != SNI_ITEM_IFACE:
            return ""
        return self._props().get(prop, "")

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self._props() if iface == SNI_ITEM_IFACE else {}

    def _props(self):
        return {
            "Category": "ApplicationStatus",
            "Id": f"wine-tray-{self._icon_xid}",
            "Title": self._title,
            "Status": "Active" if self._active else "Passive",
            "IconName": "",
            "IconPixmap": dbus.Array(self._icon_data, signature="(iiay)"),
            "OverlayIconName": "",
            "OverlayIconPixmap": dbus.Array([], signature="(iiay)"),
            "AttentionIconName": "",
            "AttentionIconPixmap": dbus.Array([], signature="(iiay)"),
            "AttentionMovieName": "",
            "ToolTip": dbus.Struct(
                ("", dbus.Array([], signature="(iiay)"), self._title, ""),
                signature="sa(iiay)ss"),
            "ItemIsMenu": dbus.Boolean(False),
            "Menu": dbus.ObjectPath("/NO_MENU"),
            "WindowId": dbus.Int32(self._icon_xid),
        }

    @dbus.service.method(SNI_ITEM_IFACE, in_signature="ii")
    def Activate(self, x, y):
        if self._active:
            self._bridge.send_click(self._icon_xid, 1)

    @dbus.service.method(SNI_ITEM_IFACE, in_signature="ii")
    def SecondaryActivate(self, x, y):
        if self._active:
            self._bridge.send_click(self._icon_xid, 2)

    @dbus.service.method(SNI_ITEM_IFACE, in_signature="ii")
    def ContextMenu(self, x, y):
        if self._active:
            self._bridge.send_click(self._icon_xid, 3)

    @dbus.service.method(SNI_ITEM_IFACE, in_signature="is")
    def Scroll(self, delta, orientation):
        pass

    @dbus.service.signal(SNI_ITEM_IFACE)
    def NewIcon(self):
        pass

    @dbus.service.signal(SNI_ITEM_IFACE)
    def NewTitle(self):
        pass

    @dbus.service.signal(SNI_ITEM_IFACE)
    def NewStatus(self, status):
        pass

    def update_icon(self, icon_data):
        if icon_data and icon_data != self._icon_data:
            self._icon_data = icon_data
            self.NewIcon()


class WineSNIBridge:
    def __init__(self, byte_order="native"):
        # struct pack format: "<I" = little-endian (native on x86_64),
        # ">I" = big-endian (DBus SNI spec literal).
        self._pack_fmt = "<I" if byte_order == "native" else ">I"
        self._dead = False
        self._display = display.Display()
        self._display.set_error_handler(lambda *a: None)
        self._screen = self._display.screen()
        self._root = self._screen.root
        self._atoms = {}
        self._tray_window = None
        self._bus = None
        self._loop = None
        self._icon_cache = {}  # cache icon data per app (keyed by WM_NAME or class)

        # SNI slot pool: reusable slots to avoid DBus path conflicts
        self._slots = []       # list of {sni, bus_name, dbus_name, xid, icon_ready}
        self._slot_counter = 0

        # Active icon tracking
        self._active_icons = {}  # xid -> slot_index

        for name in ["_NET_SYSTEM_TRAY_S0", "_NET_SYSTEM_TRAY_OPCODE",
                      "_NET_SYSTEM_TRAY_ORIENTATION", "MANAGER",
                      "_XEMBED", "_XEMBED_INFO", "_NET_WM_ICON",
                      "WM_NAME", "_NET_WM_NAME", "UTF8_STRING"]:
            self._atoms[name] = self._display.intern_atom(name)

    def _fatal(self, reason):
        # The X11 connection is unrecoverable from inside the same Display
        # object — pending_events() / queued errors keep allocating inside
        # python-xlib forever. Bail out and let systemd Restart= bring us
        # back with a fresh display.
        if self._dead:
            return False
        self._dead = True
        log(f"X11 connection lost ({reason}); exiting for systemd restart")
        if self._loop is not None:
            self._loop.quit()
        else:
            sys.exit(1)
        return False

    def _init_dbus(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SessionBus()
        for _ in range(20):
            try:
                self._bus.get_object(SNI_WATCHER_BUS, SNI_WATCHER_PATH)
                log("StatusNotifierWatcher found")
                return True
            except dbus.DBusException:
                time.sleep(0.5)
        log("StatusNotifierWatcher not available")
        return False

    def _get_or_create_slot(self):
        """Create a fresh SNI slot with its own DBus connection.
        Each slot needs a separate connection so multiple /StatusNotifierItem
        objects can coexist (dbus-python only allows one per path per connection)."""
        self._slot_counter += 1
        bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-{self._slot_counter}"

        # Get the session bus address and create an independent connection
        bus_addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        slot_bus = dbus.bus.BusConnection(bus_addr)

        dbus_name = dbus.service.BusName(bus_name, slot_bus, do_not_queue=True)
        sni = SNIItem(dbus_name, "/StatusNotifierItem", self)

        slot = {"sni": sni, "bus_name": bus_name, "dbus_name": dbus_name,
                "slot_bus": slot_bus,
                "xid": None, "window": None, "icon_ready": False}

        # Find an empty position or append
        for i, s in enumerate(self._slots):
            if s is None:
                self._slots[i] = slot
                return i
        self._slots.append(slot)
        return len(self._slots) - 1

    def _claim_tray(self):
        sel_atom = self._atoms["_NET_SYSTEM_TRAY_S0"]

        self._tray_window = self._root.create_window(
            0, 0, 1, 1, 0,
            self._screen.root_depth, X.InputOutput, X.CopyFromParent,
            event_mask=(X.StructureNotifyMask | X.SubstructureNotifyMask |
                       X.PropertyChangeMask | X.ExposureMask),
            override_redirect=False
        )
        self._tray_window.set_wm_name("wine-sni-bridge")
        self._tray_window.set_wm_class("wine-sni-bridge", "wine-sni-bridge")
        self._tray_window.change_property(
            self._display.intern_atom("_NET_WM_WINDOW_TYPE"),
            Xatom.ATOM, 32,
            [self._display.intern_atom("_NET_WM_WINDOW_TYPE_UTILITY")])
        self._tray_window.change_property(
            self._display.intern_atom("_NET_WM_STATE"),
            Xatom.ATOM, 32,
            [self._display.intern_atom("_NET_WM_STATE_SKIP_TASKBAR"),
             self._display.intern_atom("_NET_WM_STATE_SKIP_PAGER")])
        self._tray_window.configure(x=-9999, y=-9999)
        self._tray_window.map()
        self._tray_window.change_property(
            self._atoms["_NET_SYSTEM_TRAY_ORIENTATION"],
            Xatom.CARDINAL, 32, [0])

        self._tray_window.set_selection_owner(sel_atom, X.CurrentTime)
        self._display.sync()

        owner = self._display.get_selection_owner(sel_atom)
        if not owner or owner.id != self._tray_window.id:
            log("Failed to claim tray selection")
            return False

        ev = protocol.event.ClientMessage(
            window=self._root,
            client_type=self._atoms["MANAGER"],
            data=(32, [X.CurrentTime, sel_atom, self._tray_window.id, 0, 0]))
        self._root.send_event(ev, event_mask=X.StructureNotifyMask)
        self._display.flush()

        log(f"Claimed tray selection")
        return True

    def _get_icon_key(self, icon_win):
        """Get a cache key for the icon window based on its WM_CLASS."""
        try:
            cls = icon_win.get_wm_class()
            if cls:
                return cls[1] or cls[0] or "unknown"
        except Exception:
            pass
        return "unknown"

    def _dock_icon(self, icon_xid):
        if icon_xid in self._active_icons:
            return

        try:
            icon_win = self._display.create_resource_object("window", icon_xid)

            # Resize tray window
            n = len(self._active_icons) + 1
            self._tray_window.configure(width=max(1, n * 32), height=32)

            x_offset = len(self._active_icons) * 32
            icon_win.reparent(self._tray_window, x_offset, 0)
            icon_win.configure(width=32, height=32)
            icon_win.map()
            icon_win.change_attributes(
                event_mask=(X.StructureNotifyMask | X.PropertyChangeMask |
                           X.ExposureMask))

            ev = protocol.event.ClientMessage(
                window=icon_win,
                client_type=self._atoms["_XEMBED"],
                data=(32, [X.CurrentTime, XEMBED_EMBEDDED_NOTIFY, 0,
                           self._tray_window.id, XEMBED_VERSION]))
            icon_win.send_event(ev)
            self._display.flush()

            # Get or reuse an SNI slot
            slot_idx = self._get_or_create_slot()
            slot = self._slots[slot_idx]
            slot["xid"] = icon_xid
            slot["window"] = icon_win
            slot["icon_ready"] = False
            slot["icon_key"] = self._get_icon_key(icon_win)
            slot["sni"].bind(icon_xid)

            # Reuse cached icon for this app so it's not white on re-dock
            cached = self._icon_cache.get(slot["icon_key"])
            if cached:
                slot["sni"].update_icon(cached)

            self._active_icons[icon_xid] = slot_idx

            # Register with watcher
            for attempt in range(10):
                try:
                    watcher = self._bus.get_object(SNI_WATCHER_BUS, SNI_WATCHER_PATH)
                    # Register with bus_name so watcher knows where to find us
                    # Watcher will query bus_name at /StatusNotifierItem by default,
                    # but we use a unique path, so register the full path
                    dbus.Interface(watcher, SNI_WATCHER_BUS).RegisterStatusNotifierItem(
                        slot["bus_name"])
                    break
                except dbus.DBusException:
                    if attempt < 9:
                        time.sleep(0.5)

            log(f"Docked icon {icon_xid} -> slot {slot_idx} ({slot['bus_name']})")

            # Start icon extraction retries
            GLib.timeout_add(500, self._extract_icon_retry, icon_xid)

        except Exception as e:
            log(f"Dock error {icon_xid}: {e}")

    def _undock_icon(self, icon_xid):
        if icon_xid not in self._active_icons:
            return

        slot_idx = self._active_icons.pop(icon_xid)
        slot = self._slots[slot_idx]

        # Fully remove from DBus so waybar drops the icon
        try:
            slot["sni"].remove_from_connection()
        except Exception:
            pass
        try:
            del slot["dbus_name"]
        except Exception:
            pass
        # Close the slot's bus connection
        try:
            slot["slot_bus"].close()
        except Exception:
            pass

        # Remove slot entirely so a fresh one is created next time
        self._slots[slot_idx] = None

        # Resize tray window. Wrapped because a dead X server raises here
        # straight into the GLib idle callback, which is what flooded the
        # journal and leaked python-xlib internal queues for days.
        n = len(self._active_icons)
        try:
            self._tray_window.configure(width=max(1, n * 32) if n else 1, height=32)
            for i, (xid, si) in enumerate(self._active_icons.items()):
                try:
                    self._slots[si]["window"].configure(x=i * 32, y=0)
                except Exception:
                    pass
            self._display.flush()
        except (ConnectionClosedError, IOError, OSError) as e:
            return self._fatal(f"_undock_icon: {e!r}")
        except Exception:
            pass
        log(f"Undocked icon {icon_xid} ({n} remaining)")

    def _extract_icon(self, icon_xid):
        if self._dead or icon_xid not in self._active_icons:
            return False

        slot = self._slots[self._active_icons[icon_xid]]
        if slot["icon_ready"]:
            return False

        icon_win = slot["window"]
        sni = slot["sni"]

        try:
            # Try _NET_WM_ICON first
            icon_prop = icon_win.get_full_property(
                self._atoms["_NET_WM_ICON"], Xatom.CARDINAL)
            if icon_prop and icon_prop.value is not None and len(icon_prop.value) >= 3:
                icons = self._parse_net_wm_icon(icon_prop.value)
                if icons:
                    sni.update_icon(icons)
                    self._icon_cache[slot.get("icon_key", "unknown")] = icons
                    slot["icon_ready"] = True
                    log(f"Icon {icon_xid}: via _NET_WM_ICON")
                    return False

            # CopyArea method
            self._display.sync()
            geom = icon_win.get_geometry()
            w, h = geom.width, geom.height
            if w > 0 and h > 0:
                pix = icon_win.create_pixmap(w, h, geom.depth)
                gc = icon_win.create_gc()
                try:
                    pix.copy_area(gc, icon_win, 0, 0, w, h, 0, 0)
                    self._display.sync()
                    img = pix.get_image(0, 0, w, h, X.ZPixmap, 0xFFFFFFFF)
                    if img and img.data:
                        bpp = len(img.data) // (w * h) if w * h else 4
                        colored = sum(1 for i in range(0, len(img.data), bpp)
                                      if any(img.data[i+j] > 10
                                             for j in range(min(3, bpp))))
                        if colored > (w * h * 0.05):
                            icon_data = self._raw_to_argb(img.data, w, h, geom.depth)
                            if icon_data:
                                sni.update_icon(icon_data)
                                self._icon_cache[slot.get("icon_key", "unknown")] = icon_data
                                slot["icon_ready"] = True
                                log(f"Icon {icon_xid}: extracted ({w}x{h}, {colored}px)")
                                return False
                        return True  # retry - not drawn yet
                finally:
                    gc.free()
                    pix.free()
        except (ConnectionClosedError, IOError, OSError) as e:
            return self._fatal(f"_extract_icon: {e!r}")
        except Exception as e:
            log(f"Icon error {icon_xid}, undocking: {e}")
            GLib.idle_add(self._undock_icon, icon_xid)
        return False

    def _extract_icon_retry(self, icon_xid):
        if icon_xid not in self._active_icons:
            return False
        slot = self._slots[self._active_icons[icon_xid]]
        if slot["icon_ready"]:
            return False
        retries = slot.get("retries", 0)
        if retries >= 30:
            return False
        slot["retries"] = retries + 1
        return self._extract_icon(icon_xid)

    def _parse_net_wm_icon(self, data):
        offset = 0
        best = None
        best_size = 0
        while offset + 2 < len(data):
            w, h = int(data[offset]), int(data[offset + 1])
            offset += 2
            if w <= 0 or h <= 0 or offset + w * h > len(data):
                break
            pixels = data[offset:offset + w * h]
            offset += w * h
            if w * h > best_size and w <= 256:
                best_size = w * h
                best = (w, h, b"".join(
                    struct.pack(self._pack_fmt, int(p) & 0xFFFFFFFF) for p in pixels))
        if best:
            w, h, argb = best
            return [dbus.Struct((dbus.Int32(w), dbus.Int32(h),
                                 dbus.ByteArray(argb)), signature="iiay")]
        return []

    def _raw_to_argb(self, raw, w, h, depth):
        try:
            pixels = []
            for i in range(0, min(len(raw), w * h * 4), 4):
                if i + 3 > len(raw):
                    break
                b, g, r = raw[i], raw[i+1], raw[i+2]
                a = raw[i+3] if depth == 32 else 255
                if r + g + b < 30:
                    a = 0
                pixels.append(struct.pack(self._pack_fmt, (a << 24) | (r << 16) | (g << 8) | b))
            argb = b"".join(pixels)
            if argb and w > 0 and h > 0:
                cropped = self._crop_argb(argb, w, h)
                if cropped:
                    cw, ch, cdata = cropped
                    scaled = self._scale_argb(cdata, cw, ch, 48, 48)
                    return [dbus.Struct((dbus.Int32(48), dbus.Int32(48),
                                        dbus.ByteArray(scaled)), signature="iiay")]
        except Exception:
            pass
        return None

    def _crop_argb(self, argb, w, h):
        min_x, min_y, max_x, max_y = w, h, 0, 0
        for y in range(h):
            for x in range(w):
                if argb[(y * w + x) * 4] > 0:
                    min_x, min_y = min(min_x, x), min(min_y, y)
                    max_x, max_y = max(max_x, x), max(max_y, y)
        if max_x < min_x:
            return None
        min_x, min_y = max(0, min_x - 1), max(0, min_y - 1)
        max_x, max_y = min(w - 1, max_x + 1), min(h - 1, max_y + 1)
        cw, ch = max_x - min_x + 1, max_y - min_y + 1
        out = bytearray(cw * ch * 4)
        for y in range(ch):
            s = ((min_y + y) * w + min_x) * 4
            d = y * cw * 4
            out[d:d + cw * 4] = argb[s:s + cw * 4]
        return (cw, ch, bytes(out))

    def _scale_argb(self, argb, sw, sh, dw, dh):
        out = bytearray(dw * dh * 4)
        for y in range(dh):
            for x in range(dw):
                s = ((y * sh // dh) * sw + (x * sw // dw)) * 4
                d = (y * dw + x) * 4
                out[d:d+4] = argb[s:s+4]
        return bytes(out)

    def send_click(self, icon_xid, button):
        if self._dead or icon_xid not in self._active_icons:
            return
        slot = self._slots[self._active_icons[icon_xid]]
        icon_win = slot["window"]
        try:
            geom = icon_win.get_geometry()
            cx, cy = geom.width // 2, geom.height // 2
            for EvType in [protocol.event.ButtonPress, protocol.event.ButtonRelease]:
                mask = X.ButtonPressMask if EvType == protocol.event.ButtonPress else X.ButtonReleaseMask
                state = 0 if EvType == protocol.event.ButtonPress else (1 << (7 + button))
                icon_win.send_event(EvType(
                    time=X.CurrentTime, root=self._root, window=icon_win,
                    child=X.NONE, root_x=0, root_y=0, event_x=cx, event_y=cy,
                    state=state, detail=button, same_screen=True), event_mask=mask)
            self._display.flush()
        except (ConnectionClosedError, IOError, OSError) as e:
            self._fatal(f"send_click: {e!r}")
        except Exception as e:
            log(f"Click error {icon_xid}, undocking stale icon: {e}")
            GLib.idle_add(self._undock_icon, icon_xid)

    def _process_x11(self):
        if self._dead:
            return False
        try:
            while self._display.pending_events():
                ev = self._display.next_event()
                if ev.type == X.ClientMessage:
                    if (hasattr(ev, 'client_type') and
                            ev.client_type == self._atoms["_NET_SYSTEM_TRAY_OPCODE"] and
                            ev.data[1][1] == SYSTEM_TRAY_REQUEST_DOCK):
                        xid = ev.data[1][2]
                        if xid:
                            GLib.idle_add(self._dock_icon, xid)
                elif ev.type == X.DestroyNotify:
                    xid = getattr(ev, 'window', None)
                    if xid and xid.id in self._active_icons:
                        GLib.idle_add(self._undock_icon, xid.id)
                elif ev.type == X.UnmapNotify:
                    xid = getattr(ev, 'window', None)
                    if xid and xid.id in self._active_icons:
                        GLib.idle_add(self._undock_icon, xid.id)
                elif ev.type == X.Expose:
                    xid = getattr(ev, 'window', None)
                    if xid and xid.id in self._active_icons:
                        GLib.timeout_add(100, self._extract_icon, xid.id)
        except (ConnectionClosedError, IOError, OSError) as e:
            return self._fatal(f"_process_x11: {e!r}")
        except Exception:
            pass
        return True

    def _re_register_all(self):
        if not self._active_icons:
            return False
        log(f"Re-registering {len(self._active_icons)} icons...")
        for xid, si in self._active_icons.items():
            slot = self._slots[si]
            try:
                watcher = self._bus.get_object(SNI_WATCHER_BUS, SNI_WATCHER_PATH)
                dbus.Interface(watcher, SNI_WATCHER_BUS).RegisterStatusNotifierItem(
                    slot["bus_name"])
                slot["sni"].NewIcon()
                log(f"Re-registered {xid}")
            except Exception as e:
                log(f"Re-register failed {xid}: {e}")
        return False

    def run(self):
        log("Starting...")
        if not self._init_dbus():
            return 1
        if not self._claim_tray():
            return 1

        self._bus.watch_name_owner(SNI_WATCHER_BUS,
            lambda owner: GLib.timeout_add(1000, self._re_register_all)
                          if owner and self._active_icons else None)

        self._root.change_attributes(event_mask=X.StructureNotifyMask)
        self._loop = GLib.MainLoop()
        GLib.timeout_add(50, self._process_x11)

        signal.signal(signal.SIGINT, lambda *_: self._loop.quit())
        signal.signal(signal.SIGTERM, lambda *_: self._loop.quit())

        log("Ready")
        try:
            self._loop.run()
        except KeyboardInterrupt:
            pass

        for xid in list(self._active_icons.keys()):
            self._undock_icon(xid)
        try:
            self._display.close()
        except Exception:
            pass
        if self._dead:
            log("Stopped (X11 dead — exiting nonzero so systemd restarts).")
            return 1
        log("Stopped.")
        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Wine SNI Bridge - X11 tray to StatusNotifierItem for Wayland")
    parser.add_argument(
        "--byte-order",
        choices=["native", "network"],
        default="native",
        help=(
            "IconPixmap packing byte order. 'native' (default) matches what "
            "Qt QImage::Format_ARGB32 and Cairo ARGB32 read on little-endian "
            "hosts — the colors render correctly in every known SNI host. "
            "'network' follows the DBus SNI spec literally (big-endian ARGB); "
            "use only if a host requires it."
        ),
    )
    args = parser.parse_args()
    sys.exit(WineSNIBridge(byte_order=args.byte_order).run())


if __name__ == "__main__":
    main()
