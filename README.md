# Session Restore

An Omarchy bar plugin that remembers exactly which apps are open on each
Hyprland workspace — window class, position, size, floating/pinned/
fullscreen state, and (for terminals) the working directory — and puts
them back after a reboot or logout.

Bar icon: a floppy-disk glyph, left side of the bar by default. Left-click
opens the config screen; right-click triggers an immediate full save.

## How it works

- **Watching.** A background process listens to Hyprland's own event
  socket and, a couple of seconds after you open, close, move, float,
  pin, or fullscreen a window on a workspace, re-saves *only that
  workspace*. Every other workspace's saved layout is left untouched.
  Nothing runs while you're not changing your layout.
- **The one gap events don't cover.** Hyprland does not emit an IPC event
  for interactive window drags/resizes, so a floating window's position
  can drift without the watcher knowing. A periodic full rescan (default
  every 15 minutes, configurable) exists purely to catch that.
- **Restoring.** Once per login, anything missing from the last saved
  layout gets relaunched onto its saved workspace. This is
  heuristic, not exact: it matches "already open" by window class + count
  rather than individual window identity, and it reconstructs each app's
  launch command from `/proc/<pid>/cmdline`, so an app launched through a
  wrapper that doesn't preserve useful argv (some Electron/Flatpak/Snap
  apps) may come back without its in-app state (e.g. browser tabs) — just
  the app itself, on the right workspace. Tiled window split ratios are
  left to Hyprland's normal layout, not reproduced exactly; floating
  window position/size/pinned/fullscreen are restored exactly.

## Requirements

- Omarchy's Hyprland fork with the `hl`/`hl.dsp.*` Lua dispatcher API
  (this plugin does **not** use upstream Hyprland's plain-text
  `hyprctl dispatch <disp> <args>` syntax — verified against Omarchy's
  Hyprland 0.56.2 build; if a future Omarchy/Hyprland release changes
  this API again, `scripts/restore.py` and `scripts/watch.py` are the
  only two files that call it).
- `python3` (stdlib only — no pip dependencies) and `jq`-free `hyprctl -j`
  JSON output, both standard on Omarchy.

## Install

```
git clone <this-repo> ~/.config/omarchy/plugins/jcallico.session-restore
omarchy plugin enable jcallico.session-restore left
```

(Or use `omarchy plugin add <git-url>` once published.) The plugin
hot-reloads automatically; no shell restart is required after install.

## Remove

```
omarchy plugin disable jcallico.session-restore
rm -rf ~/.config/omarchy/plugins/jcallico.session-restore
rm -rf ~/.local/state/omarchy-session
```

## Configuration

All settings are in the plugin's popup panel (click the bar icon):

| Setting | Default | Notes |
|---|---|---|
| Watch for layout changes | on | Turns the background watcher on/off |
| Restore on login | on | Relaunch missing apps once per login |
| Save debounce (seconds) | 2 | Quiet period after the last change before saving |
| Safety-net rescan (minutes) | 15 | Full rescan interval for drag/resize drift |
| Excluded app classes | (none) | Comma-separated window classes to never save/restore |

State lives outside the plugin folder, under
`~/.local/state/omarchy-session/` (`session.json` for the saved layout,
`config.json` mirroring the settings above for the Python backend to
read) — deliberately outside `~/.config/omarchy/plugins/`, since Omarchy
hot-reloads plugin code on any write under that directory and frequent
state writes there would cause spurious reloads.

## Known limitations

- Window-identity matching on restore is by class + count, not exact
  window, so two windows of the same app aren't individually tracked.
- No browser tab / in-app state restore — only the app and its window
  geometry.
- Depends on Omarchy's non-upstream Hyprland dispatcher API; a fork or
  major Hyprland version bump could change it again.
