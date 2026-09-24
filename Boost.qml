import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Boost — cap the CPU's max boost clock from the bar. It can never raise a
// cap above what the CPU reports (an overclocked BIOS would report the
// overclock as its max), only lower or lift a previously-lowered cap up to
// that reported ceiling.
//
// The widget reads the live cap through the unprivileged boostctl.py monitor
// and applies changes through the separately installed root-owned boostset.py
// helper. The helper persists only a validated cap in a root-owned state file;
// the widget's shell.json entry is updated only after the helper succeeds.
//
// Left click: open the slider panel. Right click: cycle presets on the fly.
// Middle click: re-read the current cap from sysfs.
BarWidget {
  id: root

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Absolute path to this plugin's folder (trailing slash), resolved from the
  // QML file itself so the helpers are found wherever the plugin lives.
  readonly property string pluginDir: {
    var path = String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "")
    return path.charAt(path.length - 1) === "/" ? path : path + "/"
  }

  // The intended cap from settings (persisted); the bar readout instead shows
  // the sysfs truth (`state.max`) so out-of-band changes are always visible.
  property string maxGHz: String(root.effective("maxGHz", "3.0"))

  // User-configurable preset chips and right-click cycle steps, e.g.
  // "base,3.4,4.1,turbo". Empty (the default) = auto: evenly-spaced steps
  // between the reported base and max via adaptivePresetTokens(), so the
  // chips suit every CPU's actual range instead of a fixed 3.0/3.5/4.0.
  property string presets: String(root.effective("presets", ""))

  // Optional manual ceiling for the max boost, e.g. "4.7". Set when the
  // board/BIOS reports a higher figure than the CPU's rated max (PBO etc.) and
  // you want "MAX" to mean your own number. Blank = trust the CPU's sysfs.
  property string capMax: String(root.effective("capMax", ""))

  // Optional manual base clock, e.g. "3.8", for CPUs where sysfs can't report
  // it (acpi-cpufreq Ryzen without CPPC). Blank = auto-detect.
  property string baseGHz: String(root.effective("baseGHz", ""))

  // Set by right-click cycling: once the pending apply lands, fire a desktop
  // notification saying what the new max boost is.
  property bool notifyNext: false

  property string pendingApplyGHz: ""
  property real pendingGHz: -1
  property string pendingSettingsEntry: ""
  property bool persistInFlight: false

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
  // loaded; before then we never offer anything above the persisted value.
  // A manual `capMax` pins the ceiling below what the board reports when you
  // want MAX to mean your own number (e.g. a CPU rated 4.7 GHz that a PBO
  // BIOS reports as 5.0). Either way the widget can't ask for more than the
  // CPU reports, and boostctl.py enforces the same limit at sysfs.
  function cpuMax() {
    var ceiling = 100.0
    var over = Number(root.capMax)
    if (isFinite(over) && over >= 1.0) {
      var t = Number(root.state.turbo)
      if (isFinite(t) && t > 0) return Math.min(over, t, ceiling)
      return Math.min(over, ceiling)
    }
    var t = Number(root.state.turbo)
    if (isFinite(t) && t > 0) return Math.min(t, ceiling)
    var c = Number(root.maxGHz)
    if (isFinite(c) && c > 0) return Math.min(c, ceiling)
    return 3.0
  }

  // Base clock used by the BASE preset/cycle: manual override wins, then
  // whatever boostctl detected, then a sensible 2.6 last resort.
  function baseGHzValue() {
    var o = Number(root.baseGHz)
    if (isFinite(o) && o > 0) return Math.min(o, 100.0)
    var b = Number(root.state.base)
    if (isFinite(b) && b > 0) return Math.min(b, 100.0)
    return 2.6
  }

  // ---- presets ------------------------------------------------------

  // Default chips when no custom list is configured: BASE, evenly-spaced
  // steps up to MAX. Every CPU gets steps inside its own reported range, so
  // a 3.8–4.7 GHz chip gets 3.8/4.0/4.3/4.5/4.7 while an Intel 2.6–5.0 gets
  // 2.6/3.2/3.8/4.4/5.0 — never presets that sit below BASE.
  function adaptivePresetTokens() {
    var base = root.baseGHzValue()
    var max = root.cpuMax()
    var out = ["base"]
    if (max > base + 0.01) {
      var seen = { "base": 1, "turbo": 1 }
      for (var i = 1; i <= 3; i++) {
        var key = (Math.round((base + (max - base) * i / 4) * 10) / 10).toFixed(1)
        if (!seen[key]) { seen[key] = 1; out.push(key) }
      }
    }
    out.push("turbo")
    return out
  }

  // Shared validator: trims, maps MAX/boost/highest → turbo, clamps numbers to
  // the CPU ceiling, drops junk/duplicates, and never returns an empty set.
  function normalizeTokens(tokens) {
    var seen = {}
    var out = []
    for (var i = 0; i < tokens.length; i++) {
      var t = String(tokens[i]).trim().toLowerCase()
      if (!t) continue
      var key
      if (t === "base") key = "base"
      else if (t === "turbo" || t === "max" || t === "boost" || t === "highest") key = "turbo"
      else {
        var n = Number(t)
        if (!isFinite(n) || n <= 0) continue
        n = Math.max(1.0, Math.min(n, root.cpuMax()))
        n = Math.round(n * 10) / 10
        key = n.toFixed(1)
      }
      if (!seen[key]) { seen[key] = 1; out.push(key) }
    }
    // Degrade gracefully instead of guessing fixed 3.0/3.5/4.0 steps.
    return out.length ? out : root.adaptivePresetTokens()
  }

  function presetTokens() {
    // Empty/unset setting (the default) means "auto per-CPU steps".
    var raw = String(root.presets || "").trim()
    if (!raw) return root.adaptivePresetTokens()
    return root.normalizeTokens(raw.split(","))
  }

  function presetLabel(t) {
    if (t === "base") return "BASE"
    if (t === "turbo") return "MAX"
    return t
  }

  // Chip row model: {label, value} in the user's configured order.
  function presetChips() {
    var tokens = root.presetTokens()
    var chips = []
    for (var i = 0; i < tokens.length; i++) {
      chips.push({ "label": root.presetLabel(tokens[i]), "value": tokens[i] })
    }
    return chips
  }

  // Cycle steps: tokens resolved to real GHz at the moment of the click, so the
  // BASE/MAX targets always reflect the live board state.
  function presetValues() {
    var tokens = root.presetTokens()
    var vals = []
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i]
      if (t === "base") vals.push(root.baseGHzValue())
      else if (t === "turbo") vals.push(root.cpuMax())
      else vals.push(Number(t))
    }
    return vals
  }

  // Build the widget's full entry as the UNION of the runtime settings and the
  // bar layoutConfig, plus this change. layoutConfig can lag behind what the
  // user just typed (the shell rebuilds it on file-watch), so reading only one
  // of the two would silently drop sibling keys like capMax/baseGHz/presets on
  // the next single-key write.
  function mergedEntry(changes) {
    var live = {}
    // Start from the bar layoutConfig (the on-disk view)…
    var self = root.selfEntry()
    if (self instanceof Object) {
      for (var s in self) live[s] = self[s]
    }
    // …then overlay the runtime settings: if they disagree, memory is newer
    // (it was just written by an earlier apply/save and the shell hasn't
    // re-injected the file-backed view yet).
    var setting = root.settings
    if (setting instanceof Object) {
      for (var k in setting) live[k] = setting[k]
    }
    for (var c in changes) live[c] = changes[c]
    var entry = { "id": "davidjm.boost" }
    for (var e in live) if (e !== "id") entry[e] = live[e]
    return entry
  }

  // Push the unioned entry into both runtime settings and disk. Writing the
  // whole entry every time (rather than one changed key) makes each save
  // self-contained, so nothing can ever be dropped because a previous write
  // wasn't picked up yet.
  function applySettingsEntry(changes, notice) {
    var entry = root.mergedEntry(changes)
    root.settings = entry
    root.pendingSettingsEntry = JSON.stringify(entry)
    if (!root.persistInFlight) root.startPersistence()
    if (notice) root.keyNotice = notice
  }

  function startPersistence() {
    if (root.persistInFlight || !root.pendingSettingsEntry) return
    var payload = root.pendingSettingsEntry
    root.pendingSettingsEntry = ""
    root.persistInFlight = true
    persistProcess.command = ["/usr/bin/python3", root.pluginDir + "persist.py", payload]
    persistProcess.running = true
  }

  function savePresets(norm) {
    root.applySettingsEntry({ "presets": norm }, "Presets saved: " + norm)
  }

  function saveCapMax(norm) {
    root.applySettingsEntry({ "capMax": norm }, norm
      ? "Manual max set to " + norm + " GHz"
      : "Manual max cleared (MAX = CPU's own max)")
  }

  function saveBaseGHz(norm) {
    root.applySettingsEntry({ "baseGHz": norm }, norm
      ? "Base clock set to " + norm + " GHz"
      : "Base clock cleared (auto-detect)")
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
    stateFile.reload()
  }

  function parseState(text) {
    var source = String(text || "")
    if (!source || source.length > 1048576) return
    var json = {}
    try { json = JSON.parse(source) } catch (e) { return }
    if (!json || typeof json !== "object" || Array.isArray(json)) return
    var numeric = ["max", "turbo", "base", "temp"]
    for (var i = 0; i < numeric.length; i++) {
      var value = json[numeric[i]]
      if (value !== null && value !== undefined && (typeof value !== "number" || !isFinite(value))) return
    }
    if (json.max !== undefined) root.state = json
  }

  // ------------------------------------------------------------------- apply

  function applyValue(ghz) {
    var n = Number(ghz)
    if (!isFinite(n) || n <= 0) return false
    n = Math.min(n, root.cpuMax(), 100.0)
    n = Math.max(1.0, Math.round(n * 10) / 10)
    if (!isFinite(n) || n <= 0) return false
    root.dragGHz = -1
    if (applyProcess.running) {
      root.pendingGHz = n
      var queued = root.state || {}
      queued.max = n
      root.state = queued
      root.keyNotice = "Queued " + root.fmt(n) + " GHz…"
      return true
    }
    root.startApply(n)
    return true
  }

  function startApply(n) {
    root.pendingApplyGHz = n.toFixed(1)
    applyProcess.command = ["pkexec", "/usr/local/libexec/omarchy-boost/boostset.py", "set", root.pendingApplyGHz]
    applyProcess.running = true
    var fresh = root.state || {}
    fresh.max = n
    root.state = fresh
    root.keyNotice = "Applying " + root.fmt(n) + " GHz…"
  }

  function persistValue(ghz) {
    var cap = Number(ghz).toFixed(1)
    root.applySettingsEntry({ "maxGHz": cap })
  }

  // Right-click cycle: step up through the configured presets in order; after
  // the last usable one, wrap back to the first.
  function cycleQuick() {
    root.notifyNext = false
    var steps = root.presetValues()
    var cur = Number(root.state.max)
    if (!isFinite(cur)) cur = Number(root.maxGHz)
    var target = null
    for (var i = 0; i < steps.length; i++) {
      if (steps[i] > cur + 0.01) { target = steps[i]; break }
    }
    if (target === null) target = steps.length ? steps[0] : root.cpuMax()
    if (root.applyValue(target)) root.notifyNext = true
  }

  Process {
    id: applyProcess
    onExited: function(exitCode) {
      var applied = root.pendingApplyGHz
      root.pendingApplyGHz = ""
      if (exitCode === 0 && applied) {
        root.persistValue(applied)
        root.keyNotice = "Boost capped at " + root.fmt(applied) + " GHz."
        if (root.notifyNext) {
          root.notifyNext = false
          Quickshell.execDetached(["omarchy-notification-send", "Boost",
            "Max boost set to " + root.fmt(applied) + " GHz",
            "-g", "\uf0e7", "--app-name", "davidjm.boost", "-t", "4000"])
        }
      } else {
        root.keyNotice = "Apply failed — the root helper rejected the cap."
      }
      if (root.pendingGHz >= 0) {
        var next = root.pendingGHz
        root.pendingGHz = -1
        root.startApply(next)
      }
    }
  }

  Process {
    id: persistProcess
    onExited: function(exitCode) {
      root.persistInFlight = false
      if (exitCode !== 0) root.keyNotice = "Settings were not persisted."
      if (root.pendingSettingsEntry) root.startPersistence()
    }
  }

  readonly property string statePath: "/tmp/davidjm-boost-state.json"

  Process {
    id: monitorProc
    command: ["/usr/bin/python3", root.pluginDir + "boostctl.py", "monitor", "--state-file", root.statePath]
    running: true
    onExited: monitorRestart.restart()
  }
  Timer { id: monitorRestart; interval: 3000; onTriggered: monitorProc.running = true }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onTextChanged: root.parseState(stateFile.text())
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
    contentWidth: settingsPanel.fittedContentWidth(Style.space(360))
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
          text: (root.state && root.state.model && root.state.model !== "Unknown CPU"
              ? String(root.state.model) : "CPU")
          color: Color.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.bold: true
          wrapMode: Text.Wrap
          Layout.fillWidth: true
        }

        Text {
          text: (root.state && root.state.cores ? root.state.cores + " cores" : "")
            + (root.state && root.state.threads ? "/" + root.state.threads + " threads" : "")
          color: Qt.darker(Color.foreground, 1.15)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          Layout.alignment: Qt.AlignLeft
        }

        Text {
          text: "Base " + (root.baseGHz
                ? root.fmt(root.baseGHz)
                : (root.state && root.state.base ? root.fmt(root.state.base) : "–")) + " GHz"
            + "   ·   Max boost " + (root.state && root.state.turbo ? root.fmt(root.state.turbo) : "–") + " GHz"
          color: Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          font.bold: true
          Layout.alignment: Qt.AlignLeft
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

        // Scale labels: the left end is always 1.0, the right end always means
        // "the CPU's full turbo" (variable per CPU), so it reads MAX instead of
        // a hardcoded number like 5.0.
        RowLayout {
          Layout.topMargin: Style.space(2)
          Layout.fillWidth: true
          spacing: 0

          Text {
            text: "1.0"
            Layout.fillWidth: true
            Layout.rightMargin: Style.space(4)
            horizontalAlignment: Text.AlignLeft
            color: Qt.darker(Color.foreground, 1.15)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
          Text {
            text: root.fmt((1.0 + root.cpuMax()) / 2)
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignHCenter
            color: Qt.darker(Color.foreground, 1.15)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
          Text {
            text: "MAX"
            Layout.fillWidth: true
            Layout.leftMargin: Style.space(4)
            horizontalAlignment: Text.AlignRight
            color: Qt.darker(Color.foreground, 1.15)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
          }
        }

        Text {
          text: (root.state && root.state.temp !== undefined && root.state.temp !== null
            ? "Package " + root.fmt(root.state.temp) + "°C" : "Package –°C")
          color: Qt.darker(Color.foreground, 1.15)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          Layout.alignment: Qt.AlignLeft
        }

        RowLayout {
          spacing: Style.space(6)
          Layout.topMargin: Style.space(6)

          Repeater {
            model: root.presetChips()
            delegate: chip
          }
        }

        Text {
          text: "Presets (comma-separated: BASE, GHz values, MAX)"
          color: Qt.darker(Color.foreground, 1.15)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignLeft
          Layout.topMargin: Style.space(4)
        }

        TextField {
          id: presetField
          text: root.presetTokens().join(",")
          accent: Color.accent
          foreground: Color.foreground
          Layout.fillWidth: true
          onAccepted: {
            var norm = root.normalizeTokens(presetField.text.split(",")).join(",")
            root.savePresets(norm)
            presetField.text = norm
          }
        }

        Text {
          text: "Manual max (GHz, blank = from CPU)"
          color: Qt.darker(Color.foreground, 1.15)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignLeft
          Layout.topMargin: Style.space(4)
        }

        TextField {
          id: capMaxField
          text: root.capMax
          placeholderText: "e.g. 4.7"
          accent: Color.accent
          foreground: Color.foreground
          Layout.fillWidth: true
          onAccepted: {
            var t = capMaxField.text.trim()
            var n = Number(t)
            var norm = (t === "" || !isFinite(n) || n < 1.0)
              ? "" : (Math.round(n * 10) / 10).toFixed(1)
            root.saveCapMax(norm)
            capMaxField.text = norm
          }
        }

        Text {
          text: "Base clock (GHz, blank = from CPU)"
          color: Qt.darker(Color.foreground, 1.15)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
          Layout.fillWidth: true
          Layout.alignment: Qt.AlignLeft
          Layout.topMargin: Style.space(4)
        }

        TextField {
          id: baseField
          text: root.baseGHz
          placeholderText: "e.g. 3.8"
          accent: Color.accent
          foreground: Color.foreground
          Layout.fillWidth: true
          onAccepted: {
            var t = baseField.text.trim()
            var n = Number(t)
            var norm = (t === "" || !isFinite(n) || n < 1.0)
              ? "" : (Math.round(n * 10) / 10).toFixed(1)
            root.saveBaseGHz(norm)
            baseField.text = norm
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
          if (modelData.value === "base") v = root.baseGHzValue()
          else if (modelData.value === "turbo") v = root.cpuMax()
          else v = Number(modelData.value)
          root.applyValue(v)
        }
      }
    }
  }
}