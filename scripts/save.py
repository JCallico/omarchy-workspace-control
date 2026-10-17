#!/usr/bin/env python3
"""Snapshot Hyprland window layout, one workspace at a time.

  save.py --workspace-name NAME [--workspace-name NAME2 ...]
      Re-snapshot only the given workspace(s), leaving every other
      workspace's recorded layout untouched. This is what watch.py calls
      after a debounced layout-change event on that workspace.
  save.py --all
      Re-snapshot every workspace that currently has at least one window.
      Used as watch.py's periodic drift-correction safety net (interactive
      window drags/resizes don't emit Hyprland IPC events, so nothing else
      would ever catch them) and for the one-shot logout/shutdown capture.

Not meant to be run by hand, though it's safe to: it only reads
hyprctl/proc state and writes the state file.
"""
import argparse
import os
from pathlib import Path

from common import hyprctl_json, load_config, load_state, log, restore_in_progress, write_state


def read_cmdline(pid):
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [p.decode(errors="replace") for p in raw.split(b"\0") if p]


def read_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


TERMINALS = {"foot", "alacritty", "kitty", "ghostty", "wezterm", "wezterm-gui",
             "konsole", "gnome-terminal", "xterm"}


def terminal_cwd(pid, argv):
    """For a terminal emulator, the directory that matters is the one the
    shell (or program) inside it is sitting in -- the terminal process
    itself usually stays wherever it was launched (often /)."""
    if not argv or Path(argv[0].split()[0]).name not in TERMINALS:
        return None
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return None
    for child in children:
        cwd = read_cwd(child)
        if cwd:
            return cwd
    return None


def window_record(c):
    argv = read_cmdline(c.get("pid", 0))
    return {
        "class": c.get("class"),
        "title": c.get("title"),
        "workspace_id": (c.get("workspace") or {}).get("id"),
        "floating": c.get("floating"),
        "fullscreen": bool(c.get("fullscreen")),
        "pinned": c.get("pinned"),
        "at": c.get("at"),
        "size": c.get("size"),
        "cmdline": argv,
        "cwd": read_cwd(c.get("pid", 0)),
        "terminal_cwd": terminal_cwd(c.get("pid", 0), argv),
    }


def snapshot(only_names):
    if restore_in_progress():
        return
    exclude = set(load_config().get("excludeClasses") or [])
    clients = [c for c in hyprctl_json("clients") if c.get("pid", 0) > 0]

    by_name = {}
    for c in clients:
        cls = c.get("class")
        if not cls or cls in exclude:
            continue
        name = (c.get("workspace") or {}).get("name")
        if not name or name == "special:workspace-control-staging" or not read_cmdline(c["pid"]):
            continue
        by_name.setdefault(name, []).append(window_record(c))

    state = load_state()
    workspaces = state.setdefault("workspaces", {})

    active = (hyprctl_json("activeworkspace") or {}).get("name")
    if active and not active.startswith("special:"):
        state["active_workspace"] = active

    if only_names is None:
        # A full snapshot mirrors the live session exactly: workspaces that
        # are now empty, or whose windows were closed while nothing was
        # watching, or that only exist under an old name, all drop out --
        # restoring them would resurrect windows that are gone. This is
        # safe because --all never runs during the login window: watch.py
        # holds off until that login's restore has finished, and every
        # restore keeps the layout it restored from in
        # session.pre-restore.json.
        dropped = sorted(set(workspaces) - set(by_name))
        state["workspaces"] = {name: {"windows": wins} for name, wins in by_name.items()}
        if dropped:
            log("save", f"dropped workspaces no longer open: {dropped}")
    else:
        for name in only_names:
            if by_name.get(name):
                workspaces[name] = {"windows": by_name[name]}
            else:
                workspaces.pop(name, None)

    write_state(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-name", action="append", default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    snapshot(None if args.all or not args.workspace_name else set(args.workspace_name))


if __name__ == "__main__":
    main()
