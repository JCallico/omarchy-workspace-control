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

from common import hyprctl_json, load_config, load_state, log, write_state


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
        "workspace_id": (c.get("workspace") or {}).get("id"),
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

    if only_names is None:
        # --all sees every workspace Hyprland currently knows about (live
        # or not, since an empty-but-persistent one still has a name), so
        # a *custom* name outside that set is orphaned -- most commonly a
        # workspace that got renamed after being saved under its old name.
        # Zombie entries don't just clutter the state file: on restore they
        # relaunch apps under a workspace name that no longer means
        # anything to Hyprland, on top of whatever's saved under the
        # workspace's current (correct) name.
        #
        # Plain numeric names ("3", "4") and special-workspace names
        # ("special:...") are NEVER pruned here even when currently absent:
        # a non-persistent, currently-empty workspace simply doesn't appear
        # in `hyprctl workspaces` at all, which looks identical to "renamed
        # away" but isn't -- it's still a perfectly valid restore target,
        # and deleting its saved layout just because nobody's on it right
        # now would silently throw away exactly the data this plugin exists
        # to keep.
        known_names = {w.get("name") for w in hyprctl_json("workspaces")} | set(by_name)

        def is_orphaned(name):
            if name in known_names:
                return False
            if name.lstrip("-").isdigit():
                return False
            if name.startswith("special:"):
                return False
            return True

        stale = [name for name in workspaces if is_orphaned(name)]
        for name in stale:
            del workspaces[name]
        if stale:
            log("save", f"pruned stale workspace entries: {stale}")

    write_state(state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-name", action="append", default=None)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    snapshot(None if args.all or not args.workspace_name else set(args.workspace_name))


if __name__ == "__main__":
    main()
