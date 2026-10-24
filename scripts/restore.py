#!/usr/bin/env python3
"""Rebuild the saved Hyprland session: every window back on its workspace
at its exact saved position and size, tiled or floating.

Invoked once per login by Panel.qml (guarded by a marker file under
$XDG_RUNTIME_DIR so a shell reload mid-session doesn't re-run it), and by
the panel's "Restore now" button. --dry-run prints the plan without
touching anything.

How exact tiled placement works (dwindle layout): a dwindle workspace is a
binary split tree, and the saved window rectangles are its leaves, so the
tree can be recovered by repeatedly finding a straight cut that separates
the rectangles into two groups. The tree is then rebuilt one window at a
time: open the first leaf on the empty workspace, and for every split,
focus the window occupying it, `preselect` the split direction and open
(or move in) the first window of the other half -- before subdividing
either half, since dwindle only ever splits a single window. Once every
window exists, exact sizes are applied; resizing a tiled window moves the
splits around it, so this recreates the saved ratios.

Windows that are already open (anything started before restore runs, or
everything when "Restore now" is pressed mid-session) are reused rather
than relaunched: they're parked on a hidden staging workspace first so
every workspace starts empty, then moved back into their slot.

Uses Omarchy's Hyprland-fork Lua dispatcher API (hl.dsp.*), verified live
against Hyprland 0.56.2 -- NOT upstream's plain-text `hyprctl dispatch`
syntax, which this build rejects.

Known limits:
- An app that opens extra windows by itself on launch (e.g. an editor
  restoring its own previous windows) adds windows the tree doesn't know
  about, wherever focus happens to be.
- Windows on special (scratchpad) workspaces are reopened there, but not
  repositioned.
- In-app state (browser tabs, editor files) is up to each app; only the
  app, its launch arguments and its window geometry are restored.
"""
import shlex
import shutil
import sys
import time
import traceback
from pathlib import Path

from common import (OPERATION_LOCK, RECOVERY_FILE, RESTORE_LOCK, STATE_DIR, STATE_FILE,
                    file_lock, hyprctl_json, hyprctl_repl, log,
                    lua_str, load_state, wait_for_hyprctl)

HYPRCTL_READY_TIMEOUT_SECS = 30
WINDOW_MAP_TIMEOUT_SECS = 30
CLASS_MISMATCH_GRACE_SECS = 4
SPLIT_TOLERANCE_PX = 40
SIZE_PASSES = 3
STAGING_WORKSPACE = "special:workspace-control-staging"

DRY_RUN = "--dry-run" in sys.argv


def dispatch(code):
    if DRY_RUN:
        print("[dry-run]", code)
        return
    hyprctl_repl(code)
    time.sleep(0.05)


def sel(addr):
    return lua_str(f"address:{addr}")


# ---- launch commands --------------------------------------------------

def launch_argv(w):
    argv = [a for a in (w.get("cmdline") or []) if a]
    # Chromium and Electron apps (Chromium, VS Code, T3 Code, ...) rewrite
    # their own /proc/<pid>/cmdline into ONE space-joined string. Quoted as
    # a single shell word, that names a file that doesn't exist and the
    # launch silently fails -- which is why those apps never came back.
    if len(argv) == 1 and " " in argv[0] and not Path(argv[0]).exists():
        argv = argv[0].split()
    # A plain Chromium browser window shares its process (and so its
    # cmdline) with any --app window opened by the same Chromium instance;
    # relaunching it verbatim would open another app window instead.
    cls = w.get("class") or ""
    if any(a.startswith("--app=") for a in argv) and not cls.startswith("chrome-"):
        argv = [a for a in argv if not a.startswith("--app=")]
    return argv


def strip_working_directory(argv):
    """Drop a terminal's own start-directory flag so the `cd` to the
    directory its shell was last in takes effect instead."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("--working-directory", "--directory"):
            skip = True
            continue
        if a.startswith(("--working-directory=", "--directory=")):
            continue
        out.append(a)
    return out


def shell_command(w):
    argv = launch_argv(w)
    if not argv:
        return None
    cwd = w.get("cwd")
    if w.get("terminal_cwd"):
        cwd = w["terminal_cwd"]
        argv = strip_working_directory(argv)
    inner = "exec " + " ".join(shlex.quote(a) for a in argv)
    if cwd and Path(cwd).is_dir():
        inner = f"cd {shlex.quote(cwd)} && {inner}"
    # uwsm-app gives each app its own systemd scope (as Omarchy's own
    # launchers do) so its output lands in the journal, but it starts the
    # scope in / -- hence the inner `cd`.
    if shutil.which("uwsm-app"):
        return f"uwsm-app -- sh -c {shlex.quote(inner)}"
    return f"sh -c {shlex.quote(inner)}"


# ---- split tree ---------------------------------------------------------

class Leaf:
    def __init__(self, w):
        self.w = w
        x, y = w["at"]
        width, height = w["size"]
        self.x, self.y, self.r, self.b = x, y, x + width, y + height


class Split:
    def __init__(self, direction, a, b):
        self.direction = direction  # "r": b right of a, "d": b below a
        self.a, self.b = a, b


def build_tree(leaves):
    if len(leaves) == 1:
        return leaves[0]

    left, top = min(l.x for l in leaves), min(l.y for l in leaves)
    right, bottom = max(l.r for l in leaves), max(l.b for l in leaves)
    side_by_side_first = (right - left) >= (bottom - top)

    def cuts(start, end):
        for c in sorted({end(l) for l in leaves}):
            a = [l for l in leaves if end(l) <= c + SPLIT_TOLERANCE_PX]
            b = [l for l in leaves if l not in a]
            if a and b and all(start(l) >= c - SPLIT_TOLERANCE_PX for l in b):
                return a, b
        return None

    vertical = cuts(lambda l: l.x, lambda l: l.r)
    horizontal = cuts(lambda l: l.y, lambda l: l.b)
    order = [("r", vertical), ("d", horizontal)]
    if not side_by_side_first:
        order.reverse()
    for direction, found in order:
        if found:
            a, b = found
            return Split(direction, build_tree(a), build_tree(b))

    # Not a guillotine partition (shouldn't happen with dwindle): chain the
    # rest so every window still opens, just not in its exact slot.
    log("restore", f"no clean split for {[l.w['class'] for l in leaves]}; chaining")
    ordered = sorted(leaves, key=lambda l: (l.y, l.x))
    return Split("r", ordered[0], build_tree(ordered[1:]))


def first_leaf(node):
    while isinstance(node, Split):
        node = node.a
    return node


def prune(node, leaf):
    if node is leaf:
        return None
    if isinstance(node, Leaf):
        return node
    a, b = prune(node.a, leaf), prune(node.b, leaf)
    if a is None:
        return b
    if b is None:
        return a
    node.a, node.b = a, b
    return node


def describe(node, depth=0):
    pad = "  " * depth
    if isinstance(node, Leaf):
        return f"{pad}{node.w['class']} at {node.w['at']} size {node.w['size']}\n"
    return f"{pad}split {node.direction}\n" + describe(node.a, depth + 1) + describe(node.b, depth + 1)


# ---- opening windows --------------------------------------------------

class Restorer:
    def __init__(self, existing):
        # Already-open windows that match a saved one, by class.
        self.pool = {}
        for c in existing:
            self.pool.setdefault(c.get("class"), []).append(c)
        self.existing = {c["address"]: c for c in existing}
        self.parked = set()
        self.placed = set()
        self.opened = {}  # id(saved window record) -> address

    def normalize(self, addr, floating):
        dispatch(f'hl.dispatch(hl.dsp.window.fullscreen_state({{ internal = 0, client = 0, '
                 f'action = "set", window = {sel(addr)} }}))')
        current = next((c for c in hyprctl_json("clients") if c["address"] == addr), {})
        # Hyprland rejects even "off" for tiled/unpinned fullscreen windows.
        if current.get("pinned"):
            dispatch(f'hl.dispatch(hl.dsp.window.pin({{ action = "off", window = {sel(addr)} }}))')
        action = "on" if floating else "off"
        dispatch(f'hl.dispatch(hl.dsp.window.float({{ action = "{action}", window = {sel(addr)} }}))')

    def recover_staging(self):
        # Include claimed windows too: moving one out may itself have failed.
        # Attempt every window even if IPC or an individual dispatch fails.
        failed = False
        try:
            live = {c["address"]: c for c in hyprctl_json("clients")}
        except Exception as exc:
            log("restore", f"cannot query staging; returning all parked windows: {exc}")
            live = None
        for addr in self.parked:
            c = self.existing[addr]
            if live is not None:
                current = live.get(addr)
                if not current:
                    continue
                if addr in self.placed and (current.get("workspace") or {}).get("name") != STAGING_WORKSPACE:
                    continue
            try:
                dispatch(f"hl.dispatch(hl.dsp.window.move({{ workspace = {lua_str(c['_orig_ws'] or '1')}, "
                         f"follow = false, window = {sel(addr)} }}))")
                self.normalize(addr, bool(c.get("floating")))
                if c.get("pinned"):
                    dispatch(f'hl.dispatch(hl.dsp.window.pin({{ action = "on", window = {sel(addr)} }}))')
                if c.get("fullscreen"):
                    mode = "maximized" if c["fullscreen"] == 1 else "fullscreen"
                    dispatch(f'hl.dispatch(hl.dsp.window.fullscreen({{ mode = "{mode}", action = "set", window = {sel(addr)} }}))')
            except Exception as exc:
                failed = True
                log("restore", f"could not recover staged window {addr}: {exc}")
        return not failed

    def claim_existing(self, w, ws_name):
        candidates = self.pool.get(w.get("class")) or []
        if not candidates:
            return None
        # Same title first (a terminal's title carries its cwd), then same
        # original workspace, so two windows of one app don't swap places.
        for pick in (lambda c: c.get("title") == w.get("title"),
                     lambda c: c.get("_orig_ws") == ws_name,
                     lambda c: True):
            for c in candidates:
                if pick(c):
                    candidates.remove(c)
                    return c["address"]
        return None

    def wait_for_new_window(self, before, cls):
        deadline = time.monotonic() + WINDOW_MAP_TIMEOUT_SECS
        other_since = None
        while time.monotonic() < deadline:
            new = [c for c in hyprctl_json("clients") if c["address"] not in before]
            for c in new:
                if c.get("class") == cls:
                    return c["address"]
            # Some apps report a different class than when they were saved
            # (e.g. after an update); accept a lone new window after a grace
            # period rather than giving up on it.
            if new:
                other_since = other_since or time.monotonic()
                if time.monotonic() - other_since > CLASS_MISMATCH_GRACE_SECS:
                    return new[0]["address"]
            time.sleep(0.25)
        return None

    def open_window(self, w, ws_name, anchor, direction):
        """Open saved window `w` on workspace `ws_name`, splitting `anchor`
        in `direction` (or onto the empty workspace if anchor is None).
        Returns the window's address, or None if it never appeared."""
        addr = self.claim_existing(w, ws_name)
        if addr:
            self.normalize(addr, bool(w.get("floating")))
        if anchor:
            dispatch(f"hl.dispatch(hl.dsp.focus({{ window = {sel(anchor)} }}))")
            dispatch(f'hl.dispatch(hl.dsp.layout("preselect {direction}"))')
        else:
            dispatch(f"hl.dispatch(hl.dsp.focus({{ workspace = {lua_str(ws_name)} }}))")

        if addr:
            dispatch(f"hl.dispatch(hl.dsp.window.move({{ workspace = {lua_str(ws_name)}, "
                     f"follow = false, window = {sel(addr)} }}))")
            self.placed.add(addr)
            log("restore", f"  reused open {w['class']} on {ws_name}")
        else:
            cmd = shell_command(w)
            if not cmd:
                log("restore", f"  no launch command saved for {w['class']}; skipped")
                return None
            if DRY_RUN:
                print(f"[dry-run] launch on {ws_name}: {cmd}")
                return f"dry-{id(w)}"
            before = {c["address"] for c in hyprctl_json("clients")}
            dispatch(f"hl.dispatch(hl.dsp.exec_cmd({lua_str(cmd)}))")
            addr = self.wait_for_new_window(before, w.get("class"))
            if not addr:
                log("restore", f"  FAILED: {w['class']} never opened a window within "
                    f"{WINDOW_MAP_TIMEOUT_SECS}s; command was: {cmd} "
                    f"(its output is in `journalctl --user -n 50`)")
                return None
            log("restore", f"  launched {w['class']} on {ws_name}")

        self.opened[id(w)] = addr
        return addr

    def open_subtree_rep(self, node, ws_name, anchor, direction):
        """Open the first window of `node` that actually succeeds, splitting
        `anchor`; returns (address, node with any failed leaves removed)."""
        while node is not None:
            leaf = first_leaf(node)
            addr = self.open_window(leaf.w, ws_name, anchor, direction)
            if addr:
                return addr, node
            node = prune(node, leaf)
        return None, None

    def realize(self, node, ws_name, addr):
        """`addr` occupies node's whole region and is its first leaf."""
        if isinstance(node, Leaf):
            return
        addr_b, node_b = self.open_subtree_rep(node.b, ws_name, addr, node.direction)
        self.realize(node.a, ws_name, addr)
        if addr_b:
            self.realize(node_b, ws_name, addr_b)


def is_special(ws_name):
    return ws_name.startswith("special:")


def run():
    state = load_state()
    workspaces = {n: ws.get("windows", []) for n, ws in (state.get("workspaces") or {}).items()}
    workspaces = {n: [w for w in ws if w.get("class")] for n, ws in workspaces.items()}
    workspaces = {n: ws for n, ws in workspaces.items() if ws}
    if not workspaces:
        log("restore", "no saved session found; nothing to restore")
        print("No saved session found; nothing to restore.")
        return True

    if not DRY_RUN and not wait_for_hyprctl(HYPRCTL_READY_TIMEOUT_SECS):
        log("restore", f"hyprctl never became ready within {HYPRCTL_READY_TIMEOUT_SECS}s; aborting")
        return False

    saved_classes = {w["class"] for ws in workspaces.values() for w in ws}
    existing = []
    for c in hyprctl_json("clients"):
        if c.get("class") in saved_classes:
            c["_orig_ws"] = (c.get("workspace") or {}).get("name")
            existing.append(c)

    total = sum(len(ws) for ws in workspaces.values())
    log("restore", f"restoring {total} window(s) on {len(workspaces)} workspace(s); "
        f"{len(existing)} matching window(s) already open will be reused")

    restorer = Restorer(existing)
    success = False
    recovered = True
    try:
        # Park already-open windows so every workspace starts empty and the
        # split tree can be rebuilt from scratch.
        for c in existing:
            restorer.parked.add(c["address"])
            # Unpin before moving so pinned windows can actually enter staging.
            if c.get("pinned"):
                dispatch(f'hl.dispatch(hl.dsp.window.fullscreen_state({{ internal = 0, client = 0, action = "set", window = {sel(c["address"])} }}))')
                dispatch(f'hl.dispatch(hl.dsp.window.pin({{ action = "off", window = {sel(c["address"])} }}))')
            dispatch(f"hl.dispatch(hl.dsp.window.move({{ workspace = {lua_str(STAGING_WORKSPACE)}, "
                     f"follow = false, window = {sel(c['address'])} }}))")

        for ws_name, windows in workspaces.items():
            tiled = [w for w in windows if not w.get("floating") and not is_special(ws_name)]
            floating = [w for w in windows if w not in tiled]
            log("restore", f"workspace {ws_name}: {len(tiled)} tiled, {len(floating)} floating")

            if tiled:
                tree = build_tree([Leaf(w) for w in tiled])
                if DRY_RUN:
                    print(f"workspace {ws_name} tree:\n{describe(tree)}", end="")
                addr, tree = restorer.open_subtree_rep(tree, ws_name, None, None)
                if addr:
                    restorer.realize(tree, ws_name, addr)

            for w in floating:
                addr = restorer.open_window(w, ws_name, None, None)
                if not addr:
                    continue
                dispatch(f'hl.dispatch(hl.dsp.window.float({{ action = "on", window = {sel(addr)} }}))')
                size, at = w.get("size"), w.get("at")
                if size:
                    dispatch(f"hl.dispatch(hl.dsp.window.resize({{ x = {size[0]}, y = {size[1]}, "
                             f"relative = false, window = {sel(addr)} }}))")
                if at:
                    dispatch(f"hl.dispatch(hl.dsp.window.move({{ x = {at[0]}, y = {at[1]}, "
                             f"relative = false, window = {sel(addr)} }}))")

        if DRY_RUN:
            return True

        # Exact tiled sizes. Each resize shifts the splits around one window,
        # which moves its neighbours, so a few passes are needed to settle.
        tiled_windows = [(ws_name, w) for ws_name, ws in workspaces.items() for w in ws
                         if not w.get("floating") and not is_special(ws_name)
                         and id(w) in restorer.opened]
        for _ in range(SIZE_PASSES):
            for _ws, w in tiled_windows:
                addr = restorer.opened[id(w)]
                dispatch(f"hl.dispatch(hl.dsp.window.resize({{ x = {w['size'][0]}, y = {w['size'][1]}, "
                         f"relative = false, window = {sel(addr)} }}))")
            time.sleep(0.2)

        for ws, ws_windows in workspaces.items():
            for w in ws_windows:
                addr = restorer.opened.get(id(w))
                if not addr:
                    continue
                if w.get("pinned"):
                    dispatch(f'hl.dispatch(hl.dsp.window.pin({{ action = "on", window = {sel(addr)} }}))')
                if w.get("fullscreen"):
                    mode = "maximized" if type(w["fullscreen"]) is int and w["fullscreen"] == 1 else "fullscreen"
                    dispatch(f'hl.dispatch(hl.dsp.window.fullscreen({{ mode = "{mode}", action = "set", window = {sel(addr)} }}))')

        active = state.get("active_workspace") or next(iter(workspaces))
        dispatch(f"hl.dispatch(hl.dsp.focus({{ workspace = {lua_str(active)} }}))")

        # Report how close each window landed to where it was saved.
        live = {c["address"]: c for c in hyprctl_json("clients")}
        off = []
        for ws_name, ws in workspaces.items():
            for w in ws:
                c = live.get(restorer.opened.get(id(w)))
                if not c:
                    continue
                dx = abs(c["at"][0] - w["at"][0]) + abs(c["at"][1] - w["at"][1])
                ds = abs(c["size"][0] - w["size"][0]) + abs(c["size"][1] - w["size"][1])
                if dx + ds > 4:
                    off.append(f"{w['class']}@{ws_name} off by {dx}px pos/{ds}px size")
        restored = sum(
            1 for ws_name, ws in workspaces.items() for w in ws
            if (live.get(restorer.opened.get(id(w)), {}).get("workspace") or {}).get("name") == ws_name
        )
        log("restore", f"done: {restored}/{total} window(s) restored" +
            (f"; geometry mismatches: {off}" if off else "; all at their saved geometry"))

        success = restored == total
    finally:
        if not DRY_RUN:
            recovered = restorer.recover_staging()
    return success and recovered


def main():
    log("restore", "starting" + (" (dry-run)" if DRY_RUN else ""))
    if DRY_RUN:
        return run()
    # The restore lock rejects concurrent restores; the operation lock also
    # excludes saves, including ones that began just before restoration.
    with file_lock(RESTORE_LOCK, blocking=False) as acquired:
        if not acquired:
            log("restore", "another restore is already running")
            return False
        with file_lock(OPERATION_LOCK):
            try:
                # Keep the original backup across failed/interrupted retries.
                if not RECOVERY_FILE.exists():
                    if STATE_FILE.exists():
                        shutil.copy2(STATE_FILE, STATE_DIR / "session.pre-restore.json")
                    RECOVERY_FILE.touch()
                success = run()
                if success:
                    RECOVERY_FILE.unlink(missing_ok=True)
                else:
                    log("restore", "incomplete; automatic saves paused until restore succeeds or Save now replaces the layout")
                return success
            except Exception:
                log("restore", "CRASHED; automatic saves paused:\n" + traceback.format_exc())
                raise


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
