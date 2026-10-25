import QtQuick
import QtQuick.Controls.Basic
import "FocusNavigation.js" as Navigation

MotionPopup {
    id: root; objectName: "systemPanel"
    required property QtObject bridge
    property real uiScale: 1
    readonly property real unit: Math.max(.85, uiScale)
    readonly property QtObject controls: bridge.systemControls
    readonly property var state: controls.panel
    property string kind: "wifi"
    property int selectedField: 0
    property string networkId: ""
    property string unpairId: ""
    property bool confirmUnpair: false
    property real anchorX: -1
    property real anchorY: 72
    property real viewportHeight: parent.height
    property string pendingAction: "set"
    property var pendingValue: null
    property int outputCursor: 0
    property date clockTime: new Date()
    readonly property var outputs: state.outputs || []
    readonly property var sessions: state.sessions || []
    readonly property string radioState: (state.key === kind && state.radio_state) || bridge.systemState[kind] || "unknown"
    readonly property bool radio: kind === "wifi" || kind === "bluetooth"
    readonly property bool sliderMode: kind === "volume" || kind === "brightness"
    readonly property bool available: kind === "brightness" ? state.key === kind && state.brightness >= 0 && level.value >= 0
        : !state.error && (sliderMode ? level.value >= 0 && state.volume >= 0 : bridge.systemState[kind] !== "absent")
    readonly property int levelField: kind === "volume" ? -1 : 0
    width: Math.min(parent.width - 32, (kind === "volume" ? 520 : 460) * unit)
    height: Math.min(viewportHeight - Math.max(16, anchorY) - 16, (kind === "volume" ? Math.min(610, 370 + sessions.length * 80) : ({clock:240, brightness:310, battery:480, wifi:540, bluetooth:540})[kind]) * unit)
    x: Math.max(16, Math.min(parent.width - width - 16, anchorX < 0 ? parent.width - width - 24 : anchorX - width / 2))
    y: Math.max(16, Math.min(anchorY, parent.height - height - 16))
    padding: 18 * unit; modal: true; focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: FluidSurface { color: uiTheme.themeColors.glass; border.color: uiTheme.themeColors.border; radius: 26; elevation: 1; shadowBlur: 24 }
    Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim }
    function request(action, value = null, password = "") { controls.request(kind, action, value, password) }
    function refreshClock() {
        clockTick.stop()
        if (!visible || kind !== "clock") return
        clockTime = new Date()
        // The panel displays minutes. Wake at the next minute boundary, only while open.
        clockTick.interval = Math.max(1, 60000 - (clockTime.getTime() % 60000))
        clockTick.start()
    }
    onVisibleChanged: refreshClock()
    onKindChanged: refreshClock()
    function schedule(action, value) { pendingAction = action; pendingValue = value; if (!commit.running) commit.start() }
    function flush() { if (commit.running) { commit.stop(); request(pendingAction, pendingValue) } }
    function sessionRequest(index, action, value) {
        const item = sessions[index]
        if (item) schedule(action, {endpoint: state.endpoint, session: item.id, value: value})
    }
    function showPanel(key) {
        flush()
        password.focus = false; password.deselect()
        kind = key; selectedField = 0; networkId = ""; unpairId = ""; password.text = ""
        open(); request(radio && radioState !== "off" && radioState !== "absent" ? "scan" : "read")
    }
    function activate(index) {
        if (index === 2) { close(); return }
        if (kind === "brightness" && available && (index === -10 || index === -11 || index >= 20 && index <= 23)) {
            level.value = Math.max(0, Math.min(100, index === -10 ? level.value - 10 : index === -11 ? level.value + 10 : (index - 19) * 20))
            schedule("set", Math.round(level.value)); flush(); return
        }
        if (state.busy) return
        if (index === 1) { networkId = ""; password.text = ""; request(radio ? "scan" : "read"); return }
        if (index === 0) {
            if (radio && available) request("toggle", radioState !== "on")
            else if (kind === "volume" && available) request("mute", !state.muted)
            else if (kind === "battery" && state.energy_saver_available) request("energy_saver", !state.energy_saver)
            return
        }
        if (kind === "volume" && index === -2) { outputChoice.popup.open(); return }
        if (kind === "volume" && index >= 1000) {
            const i = Math.floor((index - 1000) / 2), item = sessions[i]
            if (item && index % 2 === 1) sessionRequest(i, "session_mute", !item.muted)
            return
        }
        const item = state.items[index - 3]
        if (!item) return
        if (kind === "wifi") {
            if (item.connected) request("disconnect", item.id)
            else if (item.secure && !item.saved) { networkId = item.id; password.forceActiveFocus(); bridge.keyboard.show() }
            else request("connect", item.id)
        } else if (kind === "bluetooth") {
            if (item.paired) { unpairId = item.id; confirmUnpair = false }
            else request("pair", item.id)
        }
        else if (kind === "battery") request("plan", item.id)
    }
    function connectNetwork() { if (networkId && password.text) { password.focus = false; password.deselect(); bridge.keyboard.hide(); request("connect", networkId, password.text); password.text = ""; networkId = "" } }
    onAboutToHide: { flush(); outputChoice.popup.close(); password.focus = false; password.deselect(); password.text = ""; bridge.keyboard.hide() }
    function handleAction(action) {
        if (outputChoice.popup.visible) {
            if (action === "back" || action === "menu") outputChoice.popup.close()
            else if (action === "up" || action === "down") {
                outputCursor = Math.max(0, Math.min(outputs.length - 1, outputCursor + (action === "down" ? 1 : -1)))
                outputChoice.popup.contentItem.positionViewAtIndex(outputCursor, ListView.Contain)
            }
            else if (action === "activate") { const item = outputs[outputCursor]; outputChoice.popup.close(); if (item) request("output", item.id) }
            return
        }
        if (action === "back" || action === "menu") { flush(); close(); return }
        if (unpairId) {
            if (action === "left" || action === "right") confirmUnpair = !confirmUnpair
            else if (action === "activate") { if (confirmUnpair) request("unpair", unpairId); unpairId = "" }
            return
        }
        if (networkId) { if (action === "activate") connectNetwork(); return }
        if (action === "refresh") { activate(1); return }
        if (sliderMode && selectedField === levelField && (action === "left" || action === "right") && available && (kind === "brightness" || !state.busy)) {
            level.value = Math.max(0, Math.min(100, level.value + (action === "right" ? 5 : -5))); schedule("set", Math.round(level.value)); return
        }
        if (kind === "brightness" && ["left", "right", "up", "down", "next_control", "previous_control"].indexOf(action) >= 0) {
            const ids = [-10, 0, -11, 20, 21, 22, 23, 2]
            const items = [brightnessMinus, level, brightnessPlus, presets.itemAt(0), presets.itemAt(1), presets.itemAt(2), presets.itemAt(3), panelBack]
            const current = Math.max(0, ids.indexOf(selectedField))
            selectedField = ids[action === "next_control" || action === "previous_control"
                ? (current + (action === "next_control" ? 1 : ids.length - 1)) % ids.length
                : Navigation.next(items, current, action, root.contentItem)]
            return
        }
        if (kind === "volume" && selectedField >= 1000 && selectedField % 2 === 0 && (action === "left" || action === "right") && !state.busy) {
            const index = (selectedField - 1000) / 2, row = mixer.itemAtIndex(index)
            if (row) { row.level.value = Math.max(0, Math.min(100, row.level.value + (action === "right" ? 5 : -5))); sessionRequest(index, "session_set", Math.round(row.level.value)) }
            return
        }
        if (action === "up" || action === "down") {
            let order = [0]
            if (kind === "volume") {
                order = [-2, -1, 0]
                for (let n = 0; n < sessions.length; n++) order.push(1000 + n * 2, 1001 + n * 2)
            } else for (let n = 0; n < state.items.length; n++) order.push(n + 3)
            if (radio) order.push(1)
            order.push(2)
            const i = order.indexOf(selectedField), delta = action === "down" ? 1 : -1
            if (selectedField === 2 && action === "down") return
            selectedField = order[Math.max(0, Math.min(order.length - 1, i + delta))]
        } else if (action === "left" && selectedField === 2 && radio) selectedField = 1
        else if (action === "right" && selectedField === 1) selectedField = 2
        else if (action === "activate") activate(selectedField)
        if (selectedField >= 1000) mixer.positionViewAtIndex(Math.floor((selectedField - 1000) / 2), ListView.Contain)
        else if (selectedField >= 3) devices.positionViewAtIndex(selectedField - 3, ListView.Contain)
    }
    Timer { id: commit; interval: 60; onTriggered: if (root.visible) root.request(root.pendingAction, root.pendingValue) }
    Timer { id: clockTick; objectName: "systemClockTick"; onTriggered: root.refreshClock() }
    Connections {
        target: root.controls
        function onChanged() {
            if (!root.state.busy && !level.pressed && !commit.running && root.state.key === root.kind) level.value = root.kind === "volume" ? root.state.volume : root.state.brightness
            if (!outputChoice.popup.visible) outputChoice.currentIndex = Math.max(0, root.outputs.findIndex(item => item.id === root.state.endpoint))
        }
    }
    Connections {
        target: outputChoice.popup
        function onAboutToShow() { root.outputCursor = outputChoice.currentIndex }
        function onClosed() { outputChoice.currentIndex = Math.max(0, root.outputs.findIndex(item => item.id === root.state.endpoint)) }
    }
    component PanelButton: FluidButton {
        property int field: -1
        highlighted: root.selectedField === field
        implicitHeight: Math.max(48, 44 * root.unit); font.pixelSize: 15 * root.unit
        focusPolicy: Qt.NoFocus
        onClicked: root.activate(field)
    }
    contentItem: Item {
        Column {
            id: topPanel; width: parent.width; spacing: 10 * root.unit
        Text { width: parent.width; text: ({wifi:"Wi-Fi", bluetooth:"Bluetooth", volume:qsTranslate("NGS", "Volume mixer"), brightness:qsTranslate("NGS", "Screen brightness"), battery:qsTranslate("NGS", "Battery and power"), clock:qsTranslate("NGS", "Date and time")})[root.kind]; color: uiTheme.themeColors.text; font.pixelSize: 23 * root.unit }
        Text {
            objectName: "systemPanelDescription"
            width: parent.width; visible: root.kind !== "brightness"; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 14 * root.unit
            text: root.kind === "battery" ? !root.bridge.systemState.has_battery ? qsTranslate("NGS", "Plugged in · no battery detected") : qsTranslate("NGS", "Charge: ") + root.bridge.systemState.battery + "% · " + (root.bridge.systemState.charging ? qsTranslate("NGS", "charging") : root.bridge.systemState.plugged ? qsTranslate("NGS", "plugged in") : qsTranslate("NGS", "on battery"))
                : root.kind === "clock" ? Qt.formatDateTime(root.clockTime, "dddd, d MMMM yyyy · hh:mm")
                : root.radio ? ({on:qsTranslate("NGS", "Enabled"), off:qsTranslate("NGS", "Disabled"), absent:qsTranslate("NGS", "No adapter detected"), unknown:qsTranslate("NGS", "Checking status")})[root.radioState]
                : root.kind === "brightness" ? qsTranslate("NGS", "Built-in display") : qsTranslate("NGS", "Windows default audio output · Left / Right — volume")
        }
        PanelButton { objectName: "systemToggle"; field: 0; width: parent.width; visible: root.radio; enabled: !root.state.busy && root.available; text: root.radioState === "on" ? qsTranslate("NGS", "Turn off") : qsTranslate("NGS", "Turn on") }
        PanelButton {
            objectName: "energySaverToggle"; field: 0; width: parent.width; visible: root.kind === "battery"
            enabled: !root.state.busy && !!root.state.energy_saver_available
            text: qsTranslate("NGS", "Energy saver · ") + (root.state.energy_saver === true ? qsTranslate("NGS", "on", "включена") : root.state.energy_saver === false ? qsTranslate("NGS", "off", "выключена") : qsTranslate("NGS", "unavailable"))
        }
        Text { width: parent.width; visible: root.kind === "battery" && !!root.state.energy_saver_message; text: root.state.energy_saver_message || ""; color: uiTheme.themeColors.muted; wrapMode: Text.WordWrap; font.pixelSize: 14 * root.unit }
        FluidComboBox {
            id: outputChoice; objectName: "audioOutput"; width: parent.width; visible: root.kind === "volume"
            enabled: !root.state.busy && root.outputs.length > 0; model: root.outputs; textRole: "name"
            implicitHeight: Math.max(48, 44 * root.unit); font.pixelSize: 14 * root.unit
            currentIndex: Math.max(0, root.outputs.findIndex(item => item.id === root.state.endpoint))
            displayText: root.outputs.length ? currentText : qsTranslate("NGS", "No audio outputs detected")
            background: FluidSurface { color: uiTheme.themeColors.button; border.color: root.selectedField === -2 ? uiTheme.themeColors.accent : uiTheme.themeColors.border }
            delegate: ItemDelegate {
                required property int index
                width: outputChoice.width - 12; height: 44 * root.unit; highlighted: root.outputCursor === index
                onHoveredChanged: if (hovered) root.outputCursor = index
                contentItem: Text { text: root.outputs[index].name; color: uiTheme.themeColors.text; font: outputChoice.font; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
                background: FluidSurface { color: parent.highlighted || parent.hovered ? uiTheme.themeColors.soft : uiTheme.themeColors.glass }
            }
            onActivated: index => root.request("output", root.outputs[index].id)
        }
        Row {
            width: parent.width; visible: root.sliderMode; spacing: 12 * root.unit
            PanelButton { id: brightnessMinus; objectName: "brightnessMinus"; field: -10; visible: root.kind === "brightness"; width: Math.max(48, 52 * root.unit); height: Math.max(48, 48 * root.unit); padding: 4 * root.unit; enabled: root.available; text: "−10"; Accessible.name: qsTranslate("NGS", "Decrease brightness by 10 percent") }
            Slider {
                id: level; objectName: "systemLevel"; width: parent.width - (root.kind === "volume" ? 154 * root.unit : 2 * Math.max(48, 52 * root.unit) + 90 * root.unit); height: Math.max(48, 48 * root.unit)
                from: 0; to: 100; stepSize: 1; enabled: root.available && (root.kind === "brightness" || !root.state.busy); focusPolicy: Qt.NoFocus
                onMoved: if (root.kind === "brightness" || !pressed) root.schedule("set", Math.round(value))
                onPressedChanged: { if (pressed) root.selectedField = root.levelField; else if (root.visible) { root.schedule("set", Math.round(value)); root.flush() } }
                background: Rectangle { x: level.leftPadding; y: (level.height - height) / 2; width: level.availableWidth; height: 8 * root.unit; radius: height / 2; color: uiTheme.themeColors.line; Rectangle { width: level.visualPosition * parent.width; height: parent.height; radius: parent.radius; color: uiTheme.themeColors.accent } }
                handle: Rectangle { width: 30 * root.unit; height: width; radius: width / 2; x: level.leftPadding + level.visualPosition * (level.availableWidth - width); y: (level.height - height) / 2; color: uiTheme.themeColors.text; border.width: 3; border.color: root.selectedField === root.levelField ? uiTheme.themeColors.accent : uiTheme.themeColors.border }
            }
            PanelButton { id: brightnessPlus; objectName: "brightnessPlus"; field: -11; visible: root.kind === "brightness"; width: Math.max(48, 52 * root.unit); height: Math.max(48, 48 * root.unit); padding: 4 * root.unit; enabled: root.available; text: "+10"; Accessible.name: qsTranslate("NGS", "Increase brightness by 10 percent") }
            Text { objectName: "systemLevelPercent"; width: 54 * root.unit; text: root.available ? Math.round(level.value) + "%" : "—"; color: uiTheme.themeColors.text; font.pixelSize: 17 * root.unit; anchors.verticalCenter: parent.verticalCenter }
            PanelButton { objectName: "masterMute"; field: 0; visible: root.kind === "volume"; width: 76 * root.unit; enabled: !root.state.busy && root.available; text: root.state.muted ? qsTranslate("NGS", "On") : qsTranslate("NGS", "Mute"); Accessible.name: root.state.muted ? qsTranslate("NGS", "Unmute master volume") : qsTranslate("NGS", "Mute master volume") }
        }
        Row {
            width: parent.width; spacing: 8 * root.unit; visible: root.kind === "brightness"
            Repeater {
                id: presets; model: [20, 40, 60, 80]
                PanelButton {
                    required property int index; required property int modelData
                    objectName: "brightnessPreset-" + modelData; field: 20 + index
                    width: (parent.width - 24 * root.unit) / 4; height: Math.max(48, 48 * root.unit)
                    enabled: root.available; text: modelData + "%"; Accessible.name: qsTranslate("NGS", "Brightness ") + modelData + qsTranslate("NGS", " percent")
                }
            }
        }
        }
        ListView {
            id: mixer; objectName: "audioSessions"; width: parent.width; y: topPanel.height + 10 * root.unit
            height: Math.max(0, bottomPanel.y - y - 10 * root.unit); visible: root.kind === "volume"; clip: true
            model: root.sessions; spacing: 8 * root.unit
            delegate: Item {
                required property var modelData; required property int index
                property alias level: sessionLevel
                width: mixer.width; height: 76 * root.unit
                Text { width: parent.width; text: modelData.name; elide: Text.ElideRight; color: uiTheme.themeColors.text; font.pixelSize: 15 * root.unit }
                Row {
                    anchors.bottom: parent.bottom; width: parent.width; spacing: 12 * root.unit
                    Slider {
                        id: sessionLevel; objectName: "audioSessionLevel-" + index; from: 0; to: 100; stepSize: 1; value: modelData.volume
                        width: parent.width - 154 * root.unit; height: 44 * root.unit; enabled: !root.state.busy; focusPolicy: Qt.NoFocus
                        palette.highlight: uiTheme.themeColors.accent
                        background: Rectangle { x: sessionLevel.leftPadding; y: (sessionLevel.height - height) / 2; width: sessionLevel.availableWidth; height: 6 * root.unit; radius: height / 2; color: uiTheme.themeColors.line; Rectangle { width: sessionLevel.visualPosition * parent.width; height: parent.height; radius: parent.radius; color: uiTheme.themeColors.accent } }
                        handle: Rectangle { width: 24 * root.unit; height: width; radius: width / 2; x: sessionLevel.leftPadding + sessionLevel.visualPosition * (sessionLevel.availableWidth - width); y: (sessionLevel.height - height) / 2; color: uiTheme.themeColors.text; border.width: 3; border.color: root.selectedField === 1000 + index * 2 ? uiTheme.themeColors.accent : uiTheme.themeColors.border }
                        onPressedChanged: { if (pressed) root.selectedField = 1000 + index * 2; else if (root.visible) root.sessionRequest(index, "session_set", Math.round(value)) }
                        onMoved: if (!pressed) root.sessionRequest(index, "session_set", Math.round(value))
                    }
                    Text { width: 54 * root.unit; text: Math.round(sessionLevel.value) + "%"; color: uiTheme.themeColors.text; font.pixelSize: 17 * root.unit; anchors.verticalCenter: parent.verticalCenter }
                    PanelButton { objectName: "audioSessionMute-" + index; field: 1001 + index * 2; width: 76 * root.unit; enabled: !root.state.busy; text: modelData.muted ? qsTranslate("NGS", "On") : qsTranslate("NGS", "Mute"); Accessible.name: (modelData.muted ? qsTranslate("NGS", "Unmute: ") : qsTranslate("NGS", "Mute: ")) + modelData.name }
                }
            }
            Text { anchors.centerIn: parent; width: parent.width; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter; visible: !root.sessions.length; text: root.state.busy ? qsTranslate("NGS", "Loading volume mixer…") : qsTranslate("NGS", "Applications appear here when they start playing audio"); color: uiTheme.themeColors.muted; font.pixelSize: 14 * root.unit }
            ScrollBar.vertical: ScrollBar {}
        }
        ListView {
            id: devices; objectName: "systemDevices"; width: parent.width
            y: topPanel.height + 14 * root.unit
            height: Math.max(0, bottomPanel.y - y - 14 * root.unit)
            visible: root.radio || root.kind === "battery"; clip: true; spacing: 8 * root.unit; model: root.state.items
            pressDelay: 120
            onMovementStarted: { password.focus = false; password.deselect(); root.bridge.keyboard.hide(); forceActiveFocus() }
            delegate: Item {
                id: deviceRow
                required property var modelData; required property int index
                width: devices.width; height: 64 * root.unit
                Accessible.role: Accessible.ListItem; Accessible.name: deviceLabel.text
                Text {
                    id: deviceLabel
                    Accessible.ignored: true
                    x: 8 * root.unit; width: parent.width - 76 * root.unit; height: parent.height
                    verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight; textFormat: Text.PlainText
                    text: deviceRow.modelData.name + (deviceRow.modelData.connected ? root.kind === "battery" ? qsTranslate("NGS", " · selected", " · выбрана") : qsTranslate("NGS", " · connected") : root.kind === "wifi" ? " · " + deviceRow.modelData.quality + "%" : deviceRow.modelData.paired ? qsTranslate("NGS", " · paired") : "")
                    color: uiTheme.themeColors.text; font.pixelSize: 15 * root.unit
                }
                PanelButton {
                    objectName: "systemDeviceAction-" + deviceRow.index; field: deviceRow.index + 3
                    anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                    width: Math.max(48, 52 * root.unit); height: Math.max(48, 52 * root.unit); enabled: !root.state.busy
                    text: root.kind === "bluetooth" ? deviceRow.modelData.paired ? "×" : "+" : root.kind === "battery" ? deviceRow.modelData.connected ? "✓" : "○" : deviceRow.modelData.connected ? "×" : "+"
                    Accessible.name: (root.kind === "bluetooth" ? deviceRow.modelData.paired ? qsTranslate("NGS", "Unpair: ") : qsTranslate("NGS", "Pair: ") : root.kind === "wifi" ? deviceRow.modelData.connected ? qsTranslate("NGS", "Disconnect: ") : qsTranslate("NGS", "Connect: ") : qsTranslate("NGS", "Select: ")) + deviceRow.modelData.name
                }
            }
            Text { anchors.centerIn: parent; visible: !root.state.items.length; text: root.state.busy ? qsTranslate("NGS", "Scanning for devices…") : qsTranslate("NGS", "No connections available"); color: uiTheme.themeColors.muted; font.pixelSize: 16 * root.unit }
            ScrollBar.vertical: ScrollBar {}
        }
        Column {
            id: bottomPanel; width: parent.width; anchors.bottom: parent.bottom; spacing: 14 * root.unit
        Row {
            width: parent.width; spacing: 10 * root.unit; visible: !!root.unpairId
            FluidButton { text: qsTranslate("NGS", "Cancel", "Отмена"); width: (parent.width - parent.spacing) / 2; highlighted: !root.confirmUnpair; onClicked: root.unpairId = "" }
            FluidButton { text: qsTranslate("NGS", "Unpair"); width: (parent.width - parent.spacing) / 2; highlighted: root.confirmUnpair; onClicked: { root.request("unpair", root.unpairId); root.unpairId = "" } }
        }
        Row {
            width: parent.width; spacing: 10 * root.unit; visible: !!root.networkId
            TextField { id: password; objectName: "wifiPassword"; width: parent.width - 155 * root.unit; height: 50 * root.unit; echoMode: TextInput.Password; maximumLength: 63; placeholderText: qsTranslate("NGS", "Wi-Fi password"); color: uiTheme.themeColors.text; font.pixelSize: 18 * root.unit; onAccepted: root.connectNetwork(); background: FluidSurface { color: uiTheme.themeColors.base; border.color: uiTheme.themeColors.accent } }
            FluidButton { text: qsTranslate("NGS", "Connect"); width: 145 * root.unit; height: 50 * root.unit; onClicked: root.connectNetwork() }
        }
        Row {
            width: parent.width; height: (root.kind === "brightness" ? 32 : 48) * root.unit; spacing: 10 * root.unit
            visible: root.state.busy || !!root.state.error || !!root.state.message
            ActivityIndicator {
                objectName: "systemPanelActivity"; width: 30 * root.unit; height: width
                anchors.verticalCenter: parent.verticalCenter
                visible: root.visible && !!root.state.busy; running: visible
                palette.highlight: uiTheme.themeColors.accent
            }
            Text {
                objectName: "systemPanelStatus"; width: parent.width - (root.state.busy ? 40 * root.unit : 0); height: parent.height
                wrapMode: Text.WordWrap; elide: Text.ElideRight; maximumLineCount: 2; verticalAlignment: Text.AlignVCenter
                text: root.state.busy ? root.radio && root.state.action === "scan"
                    ? (root.kind === "wifi" ? qsTranslate("NGS", "Scanning for Wi-Fi networks…") : qsTranslate("NGS", "Scanning for Bluetooth devices…")) + qsTranslate("NGS", " Found: ") + root.state.items.length
                    : qsTranslate("NGS", "Waiting for Windows…") : root.state.error || root.state.message || ""
                color: root.state.error ? uiTheme.themeColors.warning : uiTheme.themeColors.muted; font.pixelSize: 15 * root.unit
            }
        }
        Row {
            width: parent.width; spacing: 12 * root.unit
            PanelButton { field: 1; visible: root.radio; text: root.kind === "bluetooth" ? qsTranslate("NGS", "Scan for devices") : qsTranslate("NGS", "Scan for networks"); width: (parent.width - parent.spacing) / 2; enabled: !root.state.busy }
            PanelButton { id: panelBack; field: 2; text: qsTranslate("NGS", "Back · ") + root.bridge.inputHints.back; width: root.radio ? (parent.width - parent.spacing) / 2 : parent.width }
        }
        }
    }
}
