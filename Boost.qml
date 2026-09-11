import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Boost — cap or lift the CPU's max boost clock from the bar.
//
// The widget reads the live cap straight from sysfs (readable by anyone, no
// elevation needed) and applies changes through boostctl.py, the one program
// a polkit rule lets the user run passwordlessly via pkexec. The chosen cap is
// persisted into this widget's shell.json entry AND a small maxboost file next
// to this QML; the cpu-cap-boost.service systemd oneshot re-applies it at boot.
//
// Left click: open the slider panel. Right click: cycle presets on the fly.
// Middle click: re-read the current cap from sysfs.
BarWidget {
  id: root

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Absolute path to this plugin's folder (trailing slash), resolved from the
  // QML file itself so the helpers are found wherever the plugin lives.
  readonly property string pluginDir: String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")

  // The intended cap from settings (persisted); the bar readout instead shows
  // the sysfs truth (`state.max`) so out-of-band changes are always visible.
  property string maxGHz: String(root.effective("maxGHz", "3.0"))

  // Live board state, parsed from `boostctl.py get`.
  property var state: ({})

  property bool settingsOpen: false
  property string keyNotice: ""
  property real dragGHz: -1

  // ------------------------------------------------------------------ helpers

  function selfEntry() {
    var lc = root.bar ? root.bar.layoutConfig : null
    if (!lc) return null
    for (var r = 0; r < 3; r++) {
      var region = ["left", "center", "right"][r]
      var list = lc[region]
      if (!list) continue
      for (var i = 0; i < list.length; i++) {
        if (list[i] && (list[i].id || "") === "davidjm.boost") return list[i]
      }
    }
    return null
  }

  function effective(name, fallback) {
    var v = root.setting(name, undefined)
    if (v !== undefined && v !== null) return v
    var entry = root.selfEntry()
    var e = entry ? entry[name] : undefined
    return (e === undefined || e === null) ? fallback : e
  }

  function fmt(value) {
    var n = Number(value)
    return isFinite(n) ? n.toFixed(1) : "–"
  }

  // Highest allowed cap: the CPU's reported full-turbo once the state has
  // loaded; before then we never offer anything above the persisted value,
  // so the slider can never be dragged past what the CPU reports.
  function cpuMax() {
    var t = Number(root.state.turbo)
    if (isFinite(t) && t > 0) return t
    var c = Number(root.maxGHz)
    if (isFinite(c) && c > 0) return c
    return 3.0
  }

  function toggleSettings() {
    root.settingsOpen = !root.settingsOpen
    if (root.settingsOpen) root.keyNotice = ""
  }
  function closeSettings() { root.settingsOpen = false }
  // KeyboardPanel funnels outside-click dismissal and popout handoff through
  // `owner`; both call these.
  function close() { root.closeSettings() }
  function closeForPopoutSwitch() { root.closeSettings() }

  // The shell live-patches settings after persist.py; a released slider then
  // stops previewing and the bound (now-current) value takes over.
  onSettingsChanged: root.dragGHz = -1

  readonly property string displayMax:
    (root.state && root.state.max !== undefined && root.state.max !== null)
      ? root.fmt(root.state.max) : root.fmt(root.maxGHz)

  // --------------------------------------------------------------- state sync

  function refreshState() {
    if (stateProcess.running) return
    stateProcess.command = ["python3", root.pluginDir + "boostctl.py", "get"]
    stateProcess.running = true
  }

  function parseState(text) {
    var json = {}
    try { json = JSON.parse(text) } catch (e) { json = {} }
    if (json.max !== undefined) root.state = json
  }

  // ------------------------------------------------------------------- apply

  function applyValue(ghz) {
    var n = Number(ghz)
    if (!isFinite(n)) return
    var turbo = Number(root.state.turbo)
    if (isFinite(turbo) && turbo > 0) n = Math.min(n, turbo)
    n = Math.max(1.0, Math.round(n * 10) / 10)
    root.dragGHz = -1
    if (applyProcess.running) return
    applyProcess.command = ["pkexec", root.pluginDir + "boostctl.py", "set", n.toFixed(1)]
    applyProcess.running = true
    root.persistValue(n)
    // Optimistically mirror the new cap into the readout before the poll lands.
    var fresh = root.state || {}
    fresh.max = n
    root.state = fresh
    root.keyNotice = "Applying " + root.fmt(n) + " GHz…"
  }

  function persistValue(ghz) {
    var cap = Number(ghz).toFixed(1)
    // Push the value into this widget's own settings immediately so the button
    // and slider reflect it without waiting for the shell's shell.json watch.
    var live = root.selfEntry() || root.settings || {}
    if (!(live instanceof Object)) live = {}
    var entry = { "id": "davidjm.boost" }
    for (var k in live) if (k !== "id") entry[k] = live[k]
    entry["maxGHz"] = cap
    root.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function") {
      root.bar.shell.updateEntryInline("davidjm.boost", entry)
    }
    // Persist to disk for the boot service and the settings editor.
    persistProcess.command = ["python3", root.pluginDir + "persist.py", JSON.stringify({ "maxGHz": cap })]
    persistProcess.running = true
  }

  // Right-click cycle: BASE → 3.0 → 3.5 → 4.0 → MAX → BASE.
  function cycleQuick() {
    var base = Number(root.state.base)
    if (!isFinite(base) || base <= 0) base = 2.6
    var turbo = root.cpuMax()
    var steps = [base, 3.0, 3.5, 4.0, turbo]
    var cur = Number(root.state.max)
    if (!isFinite(cur)) cur = Number(root.maxGHz)
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] > cur + 0.01) { root.applyValue(steps[i]); return }
    }
    root.applyValue(base)
  }

  Timer { id: poller; interval: 2000; repeat: true; running: true; onTriggered: root.refreshState() }
  Timer { id: pendingPoll; interval: 500; repeat: false; onTriggered: root.refreshState() }

  Process {
    id: applyProcess
    onExited: function(exitCode) {
      if (exitCode === 0) {
        root.keyNotice = "Boost capped at " + root.fmt(root.state.max) + " GHz."
        pendingPoll.restart()
      } else {
        root.keyNotice = "Apply failed — is the polkit rule installed?"
      }
    }
  }

  Process { id: persistProcess }

  Process {
    id: stateProcess
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parseState(text)
    }
  }

  Component.onCompleted: root.refreshState()

  // ------------------------------------------------------------------- colors

  property color base: bar ? bar.barForeground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // --------------------------------------------------------------------- bar

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // The label is the button's `text` — WidgetButton only paints/clicks when
    // text is non-empty (`hasVisualContent`), so the glyph + cap go there.
    text: "\uF0E7  " + root.displayMax
    tooltipText: (root.state && root.state.model ? root.state.model + " · " : "")
      + "Max boost " + root.displayMax + " GHz"
      + ((root.state && root.state.temp !== undefined && root.state.temp !== null)
        ? " · " + root.fmt(root.state.temp) + "°C" : "")
      + " — left adjust · right presets · middle refresh"
    horizontalMargin: 6
    active: root.state && root.state.max !== undefined && Number(root.state.max) < Number(root.state.turbo)

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton) root.cycleQuick()
      else if (mouseButton === Qt.MiddleButton) root.refreshState()
      else root.toggleSettings()
    }
  }

  // --------------------------------------------------------- adjustment panel

  KeyboardPanel {
    id: settingsPanel
    anchorItem: button
    bar: root.bar
    owner: root
    open: root.settingsOpen
    focusTarget: capSlider
    contentWidth: settingsPanel.fittedContentWidth(Style.space(300))
    contentHeight: settingsPanel.fittedContentHeight(form.implicitHeight)

    PanelKeyCatcher {
      anchors.fill: parent
      onCloseRequested: root.closeSettings()

      ColumnLayout {
        id: form
        anchors.fill: parent
        spacing: Style.space(8)

        Text {
          text: "MAX BOOST"
          color: Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: 2
        }

        Text {
          text: "CURRENT CAP  " + (root.dragGHz >= 0
            ? root.fmt(root.dragGHz)
            : (root.state && root.state.max !== undefined ? root.fmt(root.state.max) : root.fmt(root.maxGHz))) + " GHz"
          color: Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          Layout.alignment: Qt.AlignLeft
        }

        Text {
          text: (root.state && root.state.model && root.state.model !== "Unknown CPU"
              ? String(root.state.model) : "CPU")
            + (root.state && root.state.cores ? " · " + root.state.cores + " cores" : "")
            + (root.state && root.state.threads ? "/" + root.state.threads + " threads" : "")
          color: Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          Layout.alignment: Qt.AlignLeft
        }

        PanelSlider {
          id: capSlider
          bar: root.bar
          value: {
            var cur = root.dragGHz >= 0 ? root.dragGHz
              : (root.state && root.state.max !== undefined
                  ? Number(root.state.max)
                  : (isFinite(Number(root.maxGHz)) ? Number(root.maxGHz) : 3.0))
            return Math.min(cur, root.cpuMax())
          }
          minimum: 1.0
          maximum: root.cpuMax()
          step: 0.1
          tickCount: 5
          Layout.fillWidth: true
          Layout.topMargin: Style.space(2)
          onMoved: root.dragGHz = value
          onReleased: root.applyValue(value)
        }

        Text {
          text: (root.state && root.state.temp !== undefined && root.state.temp !== null
            ? "Package " + root.fmt(root.state.temp) + "°C" : "Package –°C")
            + "  ·  full turbo " + (root.state && root.state.turbo
              ? root.fmt(root.state.turbo) : "–") + " GHz"
          color: Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          Layout.alignment: Qt.AlignLeft
        }

        RowLayout {
          spacing: Style.space(6)
          Layout.topMargin: Style.space(6)

          Repeater {
            model: [
              { "label": "BASE", "value": "base" },
              { "label": "3.0", "value": 3.0 },
              { "label": "3.5", "value": 3.5 },
              { "label": "4.0", "value": 4.0 },
              { "label": "MAX", "value": "turbo" }
            ]
            delegate: chip
          }
        }

        Text {
          text: root.keyNotice
          visible: root.keyNotice !== ""
          color: Color.popups.text
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
          Layout.fillWidth: true
        }
      }
    }
  }

  // Preset chip: a small labelled button that applies its value immediately.
  Component {
    id: chip

    Rectangle {
      required property var modelData
      property bool hot: false
      implicitWidth: Math.max(Style.space(44), chipText.implicitWidth + Style.space(16))
      implicitHeight: Style.space(26)
      radius: Style.cornerRadius
      color: hot ? Qt.rgba(root.base.r, root.base.g, root.base.b, 0.16)
                 : Qt.rgba(root.base.r, root.base.g, root.base.b, 0.07)
      border.color: hot ? Qt.rgba(root.base.r, root.base.g, root.base.b, 0.5)
                        : Qt.rgba(root.base.r, root.base.g, root.base.b, 0.22)
      border.width: 1

      Text {
        id: chipText
        anchors.centerIn: parent
        text: modelData.label
        color: root.base
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }

      MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onEntered: hot = true
        onExited: hot = false
        onClicked: {
          var v
          if (modelData.value === "base") v = Number(root.state.base) || 2.6
          else if (modelData.value === "turbo") v = root.cpuMax()
          else v = Number(modelData.value)
          root.applyValue(v)
        }
      }
    }
  }
}