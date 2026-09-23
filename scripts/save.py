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

from common import hyprctl_json, load_config, load_state, write_state


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


def window_record(c):
    return {
        "class": c.get("class"),
        "title": c.get("title"),
        "floating": c.get("floating"),
        "fullscreen": bool(c.get("fullscreen")),
        "pinned": c.get("pinned"),
        "at": c.get("at"),
        "size": c.get("size"),
        "cmdline": read_cmdline(c.get("pid", 0)),
        "cwd": read_cwd(c.get("pid", 0)),
    }


def snapshot(only_names):
    exclude = set(load_config().get("excludeClasses") or [])
    clients = [c for c in hyprctl_json("clients") if c.get("pid", 0) > 0]

    by_name = {}
    for c in clients:
        cls = c.get("class")
        if not cls or cls in exclude:
            continue
        name = (c.get("workspace") or {}).get("name")
        if not name or not read_cmdline(c["pid"]):
            continue
        by_name.setdefault(name, []).append(window_record(c))

    state = load_state()
    workspaces = state.setdefault("workspaces", {})

    target_names = only_names if only_names is not None else set(by_name)
    for name in target_names:
        workspaces[name] = {"windows": by_name.get(name, [])}

    write_state(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-name", action="append", default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    snapshot(None if args.all or not args.workspace_name else set(args.workspace_name))


if __name__ == "__main__":
    main()
