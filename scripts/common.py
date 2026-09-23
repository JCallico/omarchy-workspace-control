"""Shared helpers for the Session Restore plugin's backend scripts.

All runtime state lives under ~/.local/state/omarchy-session/, deliberately
outside the plugin folder: Omarchy hot-reloads plugin code on any write
under ~/.config/omarchy/plugins/, so writing frequent state/session files
there would cause spurious shell reloads.
"""
import json
import os
import subprocess
from pathlib import Path

STATE_DIR = Path.home() / ".local/state/omarchy-session"
STATE_FILE = STATE_DIR / "session.json"
CONFIG_FILE = STATE_DIR / "config.json"

DEFAULT_CONFIG = {
    "debounceSec": 2,
    "fallbackIntervalMin": 15,
    "excludeClasses": [],
}


def hyprctl_json(*args):
    out = subprocess.run(["hyprctl", "-j", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def hyprctl_repl(code):
    """Run one Lua expression through Hyprland's IPC (Omarchy's Hyprland
    fork exposes an `hl`/`hl.dsp` Lua dispatcher API in place of upstream's
    plain-text `hyprctl dispatch <disp> <args>`; verified live against this
    machine's Hyprland 0.56.2 build)."""
    subprocess.run(["hyprctl", "repl", code], capture_output=True, text=True)


def lua_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"workspaces": {}}


def write_state(state):
    import tempfile
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
