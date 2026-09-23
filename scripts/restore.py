#!/usr/bin/env python3
"""Relaunch windows missing from the current session back onto their saved
workspace, and best-effort reposition floating ones to their saved
position/size/pinned/fullscreen state.

Invoked once per login by Panel.qml (guarded by a marker file under
$XDG_RUNTIME_DIR so a plugin hot-reload mid-session doesn't re-run it).
--dry-run is provided for manual testing without spawning apps.

Uses Omarchy's Hyprland-fork Lua dispatcher API (hl.dsp.*), verified live
against this machine's Hyprland 0.56.2 build -- NOT upstream Hyprland's
plain-text `hyprctl dispatch <disp> <args>` syntax, which this build
rejects outright.

Limitations (heuristic by nature):
- Matches "was this app already open" by window class + count, not exact
  window identity, so two windows of the same app aren't individually
  tracked.
- Reconstructs launch commands from /proc/<pid>/cmdline, so apps launched
  via a wrapper that doesn't preserve useful argv (some Electron/Flatpak/
  Snap apps) may not relaunch with the same in-app state (e.g. browser
  tabs) -- just the app itself, on the right workspace.
- Restores floating window position/size/pinned/fullscreen; tiled window
  split ratios are left to Hyprland's normal layout, not reproduced exactly.
"""
import shlex
import sys
import time
from pathlib import Path

from common import hyprctl_json, hyprctl_repl, lua_str, load_state

BOOT_SETTLE_SECS = 3
POST_LAUNCH_SETTLE_SECS = 2.0
LAUNCH_STAGGER_SECS = 0.4

DRY_RUN = "--dry-run" in sys.argv


def dispatch(code):
    if DRY_RUN:
        print("[dry-run]", code)
        return
    hyprctl_repl(code)


def main():
    state = load_state()
    if not state.get("workspaces"):
        print("No saved session found; nothing to restore.")
        return

    time.sleep(0 if DRY_RUN else BOOT_SETTLE_SECS)

    saved = []
    for ws_name, ws in state["workspaces"].items():
        for w in ws.get("windows", []):
            if w.get("class"):
                saved.append({**w, "workspace_name": ws_name})

    current = hyprctl_json("clients")
    remaining = {}
    for c in current:
        cls = c.get("class")
        if cls:
            remaining[cls] = remaining.get(cls, 0) + 1
    before_addresses = {c.get("address") for c in current}

    to_launch = []
    for w in saved:
        cls = w["class"]
        if remaining.get(cls, 0) > 0:
            remaining[cls] -= 1
            continue
        to_launch.append(w)

    if not to_launch:
        print("All saved windows already open; nothing to relaunch.")
        return

    for w in to_launch:
        cmd = w.get("cmdline") or []
        if not cmd:
            continue
        cmd_str = " ".join(shlex.quote(a) for a in cmd)
        cwd = w.get("cwd")
        if cwd and Path(cwd).is_dir():
            cmd_str = f"cd {shlex.quote(cwd)} && {cmd_str}"
        dispatch(f"hl.dispatch(hl.dsp.exec_cmd({lua_str(cmd_str)}))")
        time.sleep(LAUNCH_STAGGER_SECS)

    if DRY_RUN:
        return

    time.sleep(POST_LAUNCH_SETTLE_SECS)
    after = hyprctl_json("clients")
    new_by_class = {}
    for c in after:
        if c.get("address") not in before_addresses:
            new_by_class.setdefault(c.get("class"), []).append(c)

    for w in to_launch:
        candidates = new_by_class.get(w["class"])
        if not candidates:
            continue
        target = candidates.pop(0)
        addr = target["address"]
        sel = f"address:{addr}"

        ws_name = w.get("workspace_name")
        if ws_name and (target.get("workspace") or {}).get("name") != ws_name:
            dispatch(
                f'hl.dispatch(hl.dsp.window.move({{ workspace = {lua_str(ws_name)}, '
                f'follow = false, window = {lua_str(sel)} }}))'
            )

        want_floating = bool(w.get("floating"))
        if want_floating != bool(target.get("floating")):
            action = "on" if want_floating else "off"
            dispatch(
                f'hl.dispatch(hl.dsp.window.float({{ action = {lua_str(action)}, '
                f'window = {lua_str(sel)} }}))'
            )

        if want_floating:
            size, at = w.get("size"), w.get("at")
            if size:
                dispatch(
                    f'hl.dispatch(hl.dsp.window.resize({{ x = {size[0]}, y = {size[1]}, '
                    f'relative = false, window = {lua_str(sel)} }}))'
                )
            if at:
                dispatch(
                    f'hl.dispatch(hl.dsp.window.move({{ x = {at[0]}, y = {at[1]}, '
                    f'relative = false, window = {lua_str(sel)} }}))'
                )

        if w.get("pinned") and not target.get("pinned"):
            dispatch(f'hl.dispatch(hl.dsp.window.pin({{ window = {lua_str(sel)} }}))')

        if w.get("fullscreen") and not target.get("fullscreen"):
            dispatch(
                f'hl.dispatch(hl.dsp.window.fullscreen({{ mode = "fullscreen", '
                f'window = {lua_str(sel)} }}))'
            )


if __name__ == "__main__":
    main()
