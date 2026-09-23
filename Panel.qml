import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Bar icon + config screen for the Session Restore plugin. This file is
// both the bar-slot widget and the popup panel (same combined-entry-point
// pattern as omarchy.tailscale / omarchy.dropbox): BarIconButton draws the
// bar glyph, KeyboardPanel hosts the popup content below it.
//
// The actual save/watch/restore logic lives in scripts/*.py, invoked here
// as child processes. Backend design notes (why a workspace-scoped,
// event-driven watcher instead of a timer; the Lua hl.dsp.* dispatcher API
// this Hyprland/Omarchy build actually requires) are in scripts/watch.py
// and scripts/restore.py's docstrings.
Panel {
  id: root
  moduleName: "jcallico.session-restore"
  ipcTarget: "jcallico.session-restore"
  // manageIpc left at its default (true): the base Panel's own IpcHandler
  // (open/close/show/hide/toggle) is all this widget needs, unlike
  // tailscale/dropbox which disable it to install a richer custom one.

  readonly property string pythonBin: "/usr/bin/python3"
  readonly property string scriptsPath: String(Qt.resolvedUrl("scripts")).replace("file://", "")
  readonly property string stateDir: Quickshell.env("HOME") + "/.local/state/omarchy-session"
  readonly property string sessionFile: stateDir + "/session.json"
  readonly property string configFile: stateDir + "/config.json"
  readonly property string restoreMarker: (Quickshell.env("XDG_RUNTIME_DIR") || ("/run/user/" + Quickshell.env("UID"))) + "/omarchy-session-restore.marker"

  readonly property bool watchEnabled: setting("watchEnabled", true)
  readonly property bool restoreOnLogin: setting("restoreOnLogin", true)
  readonly property int debounceSec: setting("debounceSec", 2)
  readonly property int fallbackIntervalMin: setting("fallbackIntervalMin", 15)
  readonly property string excludeClasses: setting("excludeClasses", "")

  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family

  property string watchStatus: "stopped"
  property string lastActionStatus: ""

  property var sessionSummary: []
  property string lastSavedText: "never"

  function updateSetting(key, value) {
    var entry = { id: root.moduleName }
    for (var k in root.settings) if (k !== "id") entry[k] = root.settings[k]
    entry[key] = value
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
    writeConfigFile()
  }

  function writeConfigFile() {
    var cfg = {
      debounceSec: root.debounceSec,
      fallbackIntervalMin: root.fallbackIntervalMin,
      excludeClasses: root.excludeClasses.split(",").map(function (s) { return s.trim() }).filter(function (s) { return s.length > 0 })
    }
    Quickshell.execDetached([
      root.pythonBin, "-c",
      "import sys, json, os\np = sys.argv[1]\nos.makedirs(os.path.dirname(p), exist_ok=True)\nopen(p, 'w').write(sys.argv[2])",
      root.configFile, JSON.stringify(cfg)
    ])
  }

  function saveNow() {
    lastActionStatus = "Saving..."
    saveNowProcess.command = [root.pythonBin, "-B", root.scriptsPath + "/save.py", "--all"]
    saveNowProcess.running = true
  }

  function restoreNow() {
    lastActionStatus = "Restoring..."
    restoreNowProcess.command = [root.pythonBin, "-B", root.scriptsPath + "/restore.py"]
    restoreNowProcess.running = true
  }

  function clearSession() {
    Quickshell.execDetached(["rm", "-f", root.sessionFile])
    lastActionStatus = "Cleared saved session"
  }

  function runRestoreOnLoginOnce() {
    // Shell-level guard so a plugin hot-reload mid-session (which re-runs
    // Component.onCompleted) never re-triggers restore -- only a real
    // reboot/login clears $XDG_RUNTIME_DIR.
    var cmd = "MARKER=" + shellQuote(root.restoreMarker) + "; " +
      "[ -e \"$MARKER\" ] || { touch \"$MARKER\"; " +
      shellQuote(root.pythonBin) + " -B " + shellQuote(root.scriptsPath + "/restore.py") + "; }"
    Quickshell.execDetached(["sh", "-c", cmd])
  }

  function shellQuote(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'"
  }

  function refreshSummary(text) {
    try {
      var data = JSON.parse(String(text || "{}"))
      var workspaces = data.workspaces || {}
      var rows = []
      for (var name in workspaces) {
        rows.push({ name: name, count: (workspaces[name].windows || []).length })
      }
      rows.sort(function (a, b) { return a.name.localeCompare(b.name) })
      sessionSummary = rows
    } catch (e) {
      sessionSummary = []
    }
  }

  Component.onCompleted: {
    writeConfigFile()
    if (root.restoreOnLogin) runRestoreOnLoginOnce()
  }

  // ---- backend processes ---------------------------------------------
  Process {
    id: watchProcess
    command: [root.pythonBin, "-B", root.scriptsPath + "/watch.py"]
    running: root.watchEnabled
    onStarted: root.watchStatus = "watching"
    onExited: function (exitCode) {
      root.watchStatus = exitCode === 0 ? "stopped" : "error (exit " + exitCode + ")"
    }
  }

  Process {
    id: saveNowProcess
    running: false
    onExited: root.lastActionStatus = "Saved"
  }

  Process {
    id: restoreNowProcess
    running: false
    onExited: root.lastActionStatus = "Restore complete"
  }

  FileView {
    path: root.sessionFile
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      root.refreshSummary(text())
      root.lastSavedText = Qt.formatDateTime(new Date(), "HH:mm:ss")
    }
    onLoadFailed: root.sessionSummary = []
  }

  // ---- bar icon --------------------------------------------------------
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    tooltipText: "Session Restore — " + root.watchStatus
    onPressed: function (buttonCode) {
      if (buttonCode === Qt.RightButton) root.saveNow()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(520))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
    }

    Flickable {
      anchors.fill: parent
      contentHeight: column.implicitHeight
      clip: true

      Column {
        id: column
        width: parent.width
        spacing: Style.space(10)

        PanelHero {
          width: parent.width
          title: "Session Restore"
          meta: root.watchStatus === "watching"
            ? "Watching for layout changes"
            : root.watchStatus
          foreground: root.contentForeground
          fontFamily: root.contentFontFamily
        }

        Text {
          width: parent.width
          text: root.sessionSummary.length === 0
            ? "No saved layout yet."
            : root.sessionSummary.map(function (r) { return r.name + ": " + r.count + " window" + (r.count === 1 ? "" : "s") }).join("  ·  ")
          wrapMode: Text.WordWrap
          color: Qt.darker(root.contentForeground, 1.3)
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.caption
        }

        PanelSeparator { foreground: root.contentForeground }

        PanelSectionHeader {
          text: "Automation"
          foreground: root.contentForeground
          fontFamily: root.contentFontFamily
        }

        Toggle {
          width: parent.width
          label: "Watch for layout changes"
          description: "Re-save a workspace a couple of seconds after you open, close, move, float, pin, or fullscreen a window on it."
          foreground: root.contentForeground
          accent: Color.accent
          fontFamily: root.contentFontFamily
          checked: root.watchEnabled
          onClicked: root.updateSetting("watchEnabled", !root.watchEnabled)
        }

        Toggle {
          width: parent.width
          label: "Restore on login"
          description: "Relaunch anything missing from the last saved layout, on its saved workspace, once per login."
          foreground: root.contentForeground
          accent: Color.accent
          fontFamily: root.contentFontFamily
          checked: root.restoreOnLogin
          onClicked: root.updateSetting("restoreOnLogin", !root.restoreOnLogin)
        }

        NumberField {
          label: "Save debounce (seconds)"
          from: 1
          to: 30
          value: root.debounceSec
          foreground: root.contentForeground
          accent: Color.accent
          fontFamily: root.contentFontFamily
          onModified: function (v) { root.updateSetting("debounceSec", v) }
        }

        NumberField {
          label: "Safety-net rescan (minutes)"
          from: 1
          to: 120
          value: root.fallbackIntervalMin
          foreground: root.contentForeground
          accent: Color.accent
          fontFamily: root.contentFontFamily
          onModified: function (v) { root.updateSetting("fallbackIntervalMin", v) }
        }

        Text {
          width: parent.width
          text: "Excluded app classes (comma-separated)"
          color: root.contentForeground
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.caption
        }

        TextField {
          id: excludeField
          width: parent.width
          text: root.excludeClasses
          placeholderText: "e.g. wofi, omarchy-launcher"
          foreground: root.contentForeground
          accent: Color.accent
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.body
          onEditingFinished: root.updateSetting("excludeClasses", text)
        }

        PanelSeparator { foreground: root.contentForeground }

        PanelSectionHeader {
          text: "Actions"
          foreground: root.contentForeground
          fontFamily: root.contentFontFamily
        }

        Row {
          width: parent.width
          spacing: Style.space(8)

          Button {
            text: "Save now"
            tooltipText: "Snapshot every workspace's current layout"
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            onClicked: root.saveNow()
          }

          Button {
            text: "Restore now"
            tooltipText: "Relaunch anything missing from the saved layout"
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            onClicked: root.restoreNow()
          }

          Button {
            text: "Clear saved session"
            tooltipText: "Delete the saved layout"
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            onClicked: root.clearSession()
          }
        }

        Text {
          width: parent.width
          visible: root.lastActionStatus !== ""
          text: root.lastActionStatus
          color: Qt.darker(root.contentForeground, 1.3)
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
