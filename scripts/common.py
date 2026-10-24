"""Shared helpers for Workspace Control's backend scripts.

All runtime state lives under ~/.local/state/omarchy-workspace-control/,
deliberately outside the plugin folder: Omarchy hot-reloads plugin code on
any write under ~/.config/omarchy/plugins/, so writing frequent state/
session files there would cause spurious shell reloads.
"""
import datetime
import fcntl
from contextlib import contextmanager
import json
import os
import subprocess
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("WORKSPACE_CONTROL_STATE_DIR")
                 or Path.home() / ".local/state/omarchy-workspace-control")
STATE_FILE = STATE_DIR / "session.json"
CONFIG_FILE = STATE_DIR / "config.json"
LOG_FILE = STATE_DIR / "workspace-control.log"
LOG_MAX_BYTES = 512 * 1024
RESTORE_LOCK = STATE_DIR / "restore.lock"
OPERATION_LOCK = STATE_DIR / "operation.lock"
RECOVERY_FILE = STATE_DIR / "restore.pending"
# Created by Panel.qml after this login's restore exits. Lives in the
# runtime dir, so it exists exactly from that point until logout/reboot.
RESTORE_MARKER = (Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
                  / "omarchy-workspace-control-restore.marker")


@contextmanager
def file_lock(path, blocking=True):
    """Kernel-owned lock: never unlink it, which would split lock ownership.
    The descriptor closes on process exit, including crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def restore_in_progress():
    with file_lock(RESTORE_LOCK, blocking=False) as acquired:
        return not acquired


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
    # hyprctl can exit zero for Lua/dispatcher failures. Require an explicit
    # success sentinel from the dispatcher result as well as process success.
    wrapped = ("local result = " + code + "; "
               "if type(result) ~= 'table' or not result.ok then "
               "error(type(result) == 'table' and result.error or 'invalid dispatcher result') end; "
               "return 'workspace-control-ok'")
    result = subprocess.run(["hyprctl", "repl", wrapped], capture_output=True,
                            text=True, check=True)
    if result.stdout.strip() != "workspace-control-ok":
        raise RuntimeError(f"Hyprland dispatcher failed: {result.stdout.strip()} {result.stderr.strip()}")


def lua_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def log(tag, message):
    """Append one timestamped line to the shared log, truncating it first
    if it's grown past LOG_MAX_BYTES. This is the only record of what
    restore.py/watch.py/save.py did on a given run -- Panel.qml invokes
    restore.py at login via Quickshell.execDetached, which discards
    stdout/stderr entirely, so without this a boot-time failure is
    completely unobservable after the fact."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            lines = LOG_FILE.read_text(errors="replace").splitlines()[-2000:]
            LOG_FILE.write_text("\n".join(lines) + "\n")
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] {tag}: {message}\n")
    except OSError:
        pass


def wait_for_hyprctl(timeout_secs=30, interval_secs=1):
    """Poll until `hyprctl -j clients` actually responds, instead of a
    blind fixed sleep. A cold boot right after a crash (fsck, cold caches,
    more services starting at once) can take longer than any fixed delay
    we'd want to hardcode; querying before Hyprland's IPC socket exists
    raises, which previously meant restore.py died silently before it
    ever reached its own logic. Returns True once ready, False on timeout.
    """
    deadline = time.monotonic() + timeout_secs
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            hyprctl_json("clients")
            return True
        except Exception:
            time.sleep(interval_secs)
    return False


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
