#!/usr/bin/env python3
"""Event-driven trigger for save.py.

Listens to Hyprland's IPC event socket (.socket2.sock) and, on a
layout-affecting event (window opened/closed/moved-workspace/floated/
pinned/fullscreened), debounces per workspace and re-snapshots only the
workspace(s) involved -- a change on one workspace never touches another,
and nothing runs while the desktop is idle.

Event line formats below were captured live against this machine's
Hyprland 0.56.2 (Omarchy fork) -- see the plugin repo's dev notes. Title-
change and focus-change events (windowtitle*, activewindow*) are
deliberately NOT tracked: they fire on nearly every keystroke/tab-switch
and would defeat the point of being event-driven rather than polling.

Interactive window drags/resizes emit no IPC event at all on this
Hyprland build, so a --all rescan runs on a long timer (fallbackIntervalMin
from config.json, read fresh each cycle) purely to catch that drift; it is
not the primary save mechanism.

Spawned and kept running by Panel.qml (Process bound to the "watchEnabled"
setting) -- not meant to be run by hand, though `python3 watch.py` is safe
to run standalone for debugging (Ctrl-C to stop).
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from common import hyprctl_json, load_config

SCRIPT_DIR = Path(__file__).resolve().parent
SAVE_SCRIPT = SCRIPT_DIR / "save.py"
RECONNECT_DELAY_SECS = 3


def sock_path():
    sig = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return f"{runtime}/hypr/{sig}/.socket2.sock"


def run_save(*args):
    # -B: never write __pycache__ under the plugin folder -- Omarchy
    # hot-reloads plugin code on any write there, and a .pyc would trigger
    # a spurious reload on every save.
    subprocess.run(["/usr/bin/python3", "-B", str(SAVE_SCRIPT), *args])


class Debouncer:
    def __init__(self):
        self.timers = {}
        self.lock = threading.Lock()

    def trigger(self, workspace_name):
        if not workspace_name:
            return
        debounce_secs = load_config().get("debounceSec", 2)
        with self.lock:
            existing = self.timers.get(workspace_name)
            if existing:
                existing.cancel()
            t = threading.Timer(debounce_secs, self._fire, args=(workspace_name,))
            self.timers[workspace_name] = t
            t.start()

    def _fire(self, workspace_name):
        run_save("--workspace-name", workspace_name)
        with self.lock:
            self.timers.pop(workspace_name, None)


def active_window_workspace():
    try:
        aw = hyprctl_json("activewindow")
        return (aw.get("workspace") or {}).get("name")
    except Exception:
        return None


def fallback_loop(stop_event):
    # Long-interval safety net for geometry drift that no IPC event covers
    # (see module docstring). Re-reads the interval each cycle so a settings
    # change takes effect on the next tick without restarting this process.
    while not stop_event.is_set():
        interval_min = load_config().get("fallbackIntervalMin", 15)
        if stop_event.wait(max(60, interval_min * 60)):
            return
        run_save("--all")


def handle_line(line, addr_ws, debouncer):
    if ">>" not in line:
        return
    event, _, payload = line.partition(">>")
    parts = payload.split(",")

    if event == "openwindow" and len(parts) >= 2:
        addr, ws_name = "0x" + parts[0], parts[1]
        addr_ws[addr] = ws_name
        debouncer.trigger(ws_name)
    elif event == "closewindow" and len(parts) >= 1:
        addr = "0x" + parts[0]
        debouncer.trigger(addr_ws.pop(addr, None))
    elif event == "movewindowv2" and len(parts) >= 3:
        addr = "0x" + parts[0]
        new_ws_name = parts[2]
        old_ws_name = addr_ws.get(addr)
        addr_ws[addr] = new_ws_name
        debouncer.trigger(old_ws_name)
        debouncer.trigger(new_ws_name)
    elif event in ("changefloatingmode", "pin") and len(parts) >= 1:
        addr = "0x" + parts[0]
        debouncer.trigger(addr_ws.get(addr))
    elif event == "fullscreen":
        debouncer.trigger(active_window_workspace())


def run():
    debouncer = Debouncer()
    addr_ws = {
        c["address"]: (c.get("workspace") or {}).get("name")
        for c in hyprctl_json("clients")
    }

    stop_event = threading.Event()
    fallback_thread = threading.Thread(target=fallback_loop, args=(stop_event,), daemon=True)
    fallback_thread.start()

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock_path())
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("Hyprland event socket closed")
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                handle_line(line.decode(errors="replace"), addr_ws, debouncer)
    finally:
        stop_event.set()


def main():
    while True:
        try:
            run()
        except Exception as e:
            print(f"session-restore watch: {e}; reconnecting in {RECONNECT_DELAY_SECS}s", file=sys.stderr)
            time.sleep(RECONNECT_DELAY_SECS)


if __name__ == "__main__":
    main()
