import QtQuick
import QtQuick.Controls.Basic

Window {
    id: root
    objectName: "globalStatusBarWindow"
    required property QtObject bridge
    readonly property real uiScale: Math.max(.85, Math.min(1.2, screen.width / 1280))
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
    transientParent: null
    color: "transparent"
    x: screen.virtualX + (screen.width - width) / 2
    y: screen.virtualY
    width: Math.min(screen.width, Math.max(560 * uiScale, buttons.implicitWidth + 48 * uiScale))
    height: Math.ceil(panel.visible ? panel.anchorY + panel.height + 32 : bar.height + 160 * uiScale)
    readonly property bool presenting: !bridge.suspended && bridge.visible
    readonly property real dragReveal: !bridge.suspended && bridge.dragProgress > 0 ? bridge.dragProgress : 0
    property real reveal: presenting ? 1 : 0
    property real pullOffset: 0
    property real handleStretch: 0
    property real hintReveal: 1
    visible: !bridge.suspended && (presenting || reveal > 0 || dragReveal > 0)
    property bool snapReveal: false
    Behavior on reveal {
        enabled: !root.bridge.suspended && root.bridge.dragProgress === 0 && !root.snapReveal
        NumberAnimation { objectName: "statusRevealAnimation"; duration: root.presenting ? 260 : 200; easing.type: root.presenting ? Easing.OutBack : Easing.InCubic; easing.overshoot: 1.15 }
    }
    Behavior on pullOffset {
        enabled: !barDrag.active && root.presenting && !root.bridge.exiting
        NumberAnimation { duration: 240; easing.type: Easing.OutBack; easing.overshoot: 1.4 }
    }
    Behavior on handleStretch {
        enabled: !barDrag.active && root.bridge.dragProgress === 0
        NumberAnimation { duration: 300; easing.type: Easing.OutBack; easing.overshoot: 2.2 }
    }
    NumberAnimation {
        id: statusHintAnim
        target: root; property: "hintReveal"
        from: 0; to: 1; duration: 200
        easing.type: Easing.OutBack; easing.overshoot: 1.2
    }
    property string clock: Qt.formatTime(new Date(), "hh:mm")
    property int selectedIndex: 0
    onSelectedIndexChanged: statusHintAnim.restart()
    readonly property var statusKeys: ["clock", "wifi", "bluetooth", "battery", "brightness", "volume", "downloads", "settings"]
    readonly property int itemCount: statusKeys.length + 1
    readonly property string selectedLabel: {
        if (selectedIndex === statusKeys.length) return qsTranslate("NGS", "Close panel")
        const key = statusKeys[selectedIndex]
        const state = bridge.systemState
        if (key === "clock") return qsTranslate("NGS", "Date and time · ") + Qt.formatDate(new Date(), "d MMMM")
        if (key === "wifi") return state.wifi ? "Wi‑Fi · " + state.wifi : qsTranslate("NGS", "Wi-Fi · not connected")
        if (key === "bluetooth") return state.bluetooth ? "Bluetooth · " + state.bluetooth : "Bluetooth"
        if (key === "battery") return !state.has_battery ? qsTranslate("NGS", "Plugged in") : qsTranslate("NGS", "Battery · ") + (state.battery >= 0 ? state.battery + "%" : "?") + (state.charging ? qsTranslate("NGS", " (charging)") : "")
        if (key === "brightness") return qsTranslate("NGS", "Screen brightness")
        if (key === "volume") return state.muted ? qsTranslate("NGS", "Muted") : qsTranslate("NGS", "Volume · ") + (state.volume >= 0 ? state.volume + "%" : "?")
        if (key === "downloads") return qsTranslate("NGS", "Downloads")
        if (key === "settings") return qsTranslate("NGS", "NGS settings")
        return ""
    }
    Connections {
        target: root.bridge
        function onAction(action) { root.handleAction(action) }
        function onEdgeDragChanged() {
            const dp = root.bridge.dragProgress
            if (dp > 0) {
                root.reveal = dp <= 1 ? dp : 1 + Math.min(0.18, (dp - 1) * 0.25)
                root.handleStretch = dp > 1 ? Math.min(44 * root.uiScale, (dp - 1) * 52 * root.uiScale) : 0
            } else {
                root.handleStretch = 0
                if (root.presenting) root.reveal = 1
                else if (root.reveal > 0) root.reveal = 0
            }
        }
    }
    Shortcut { sequence: "Escape"; enabled: root.presenting; onActivated: root.handleAction("back") }
    Timer {
        id: autoHideTimer
        objectName: "overlayAutoHideTimer"
        interval: 4000
        running: root.presenting && !panel.visible && !barDrag.active
        repeat: false
        onTriggered: if (root.presenting && !panel.visible && !barDrag.active) root.bridge.hide()
    }
    Timer { objectName: "overlaySystemStatusTimer"; interval: 30000; running: root.visible; repeat: true; onTriggered: root.bridge.refreshSystemStatus() }
    Timer {
        objectName: "overlayClockMinuteTimer"
        interval: 60000 - (Date.now() % 60000)
        running: root.visible; repeat: true; triggeredOnStart: true
        onTriggered: {
            root.clock = Qt.formatTime(new Date(), "hh:mm")
            interval = 60000 - (Date.now() % 60000)
        }
    }
        onVisibleChanged: {
        if (visible) { selectedIndex = 0; pullOffset = 0; handleStretch = 0; autoHideTimer.restart() }
        else {
            autoHideTimer.stop()
            panel.close()
            pullOffset = 0
            handleStretch = 0
            if (bridge.exiting) Qt.callLater(function() { if (!root.visible && root.bridge.exiting) root.bridge.finishHide() })
        }
    }
    onPresentingChanged: {
        if (presenting) { pullOffset = 0; handleStretch = 0; autoHideTimer.restart(); if (bridge.dragProgress === 0) reveal = 1 }
        else {
            autoHideTimer.stop()
            panel.close()
            const span = bar.height + 12 * root.uiScale
            if (pullOffset < 0 && span > 0) {
                snapReveal = true
                reveal = Math.max(0, Math.min(1, reveal + pullOffset / span))
                pullOffset = 0
                snapReveal = false
                if (reveal === 0 && bridge.exiting) Qt.callLater(function() { if (root.bridge.exiting) root.bridge.finishHide() })
            } else {
                pullOffset = 0
            }
            handleStretch = 0
            reveal = 0
        }
    }
    function handleAction(action) {
        if (!presenting) return
        autoHideTimer.restart()
        if (panel.visible) panel.handleAction(action)
        else if (action === "back" || action === "menu") bridge.hide()
        else if (action === "left" || action === "right") selectedIndex = (selectedIndex + (action === "right" ? 1 : itemCount - 1)) % itemCount
        else if (action === "activate") {
            if (selectedIndex === statusKeys.length) bridge.hide()
            else showPanel(statusKeys[selectedIndex])
        }
    }
    function showPanel(key) {
        autoHideTimer.restart()
        if (key === "downloads" || key === "settings") root.bridge.triggerLauncherAction(key)
        else panel.showPanel(key)
    }
    MouseArea { anchors.fill: parent; enabled: root.presenting; onClicked: root.bridge.hide() }
    FluidSurface {
        id: bar
        objectName: "globalStatusBar"
        anchors.horizontalCenter: parent.horizontalCenter
        readonly property real effectiveReveal: root.dragReveal > 0
            ? (root.dragReveal <= 1 ? root.dragReveal : 1 + Math.min(0.18, (root.dragReveal - 1) * 0.25))
            : root.reveal
        y: -height + (height + 12 * root.uiScale) * effectiveReveal + root.pullOffset
        enabled: root.presenting
        width: Math.min(root.width - 24, buttons.implicitWidth + 24 * root.uiScale)
        height: 88 * root.uiScale
        radius: 24 * root.uiScale
        color: uiTheme.themeColors.glass
        glass: true
        Row {
            id: buttons
            anchors.centerIn: parent
            spacing: 4 * root.uiScale
            Repeater {
                model: root.statusKeys
                FluidButton {
                    required property string modelData
                    required property int index
                    objectName: "globalStatus_" + modelData
                    width: Math.max(48, (modelData === "clock" || modelData === "battery" ? 76 : 56) * root.uiScale)
                    height: Math.max(48, 56 * root.uiScale)
                    highlighted: root.selectedIndex === index
                    Accessible.name: ({clock:qsTranslate("NGS", "Date and time"),wifi:"Wi-Fi",bluetooth:"Bluetooth",battery:qsTranslate("NGS", "Power"),brightness:qsTranslate("NGS", "Brightness"),volume:qsTranslate("NGS", "Volume"),downloads:qsTranslate("NGS", "Downloads"),settings:qsTranslate("NGS", "Settings")})[modelData]
                    contentItem: Item {
                        Row {
                            anchors.centerIn: parent; spacing: 5 * root.uiScale
                            StatusIcon { kind: modelData; state: root.bridge.systemState; width: visible ? 23 * root.uiScale : 0; height: 23 * root.uiScale; visible: modelData !== "clock" && modelData !== "downloads" && modelData !== "settings"; anchors.verticalCenter: parent.verticalCenter }
                            Item {
                                visible: modelData === "downloads"; width: visible ? 23 * root.uiScale : 0; height: 23 * root.uiScale; anchors.verticalCenter: parent.verticalCenter
                                Item {
                                    anchors.centerIn: parent; width: 24; height: 24; scale: parent.width / 24
                                    Rectangle { x: 11; y: 1; width: 2; height: 14; radius: 1; color: uiTheme.themeColors.text }
                                    Rectangle { x: 5; y: 11; width: 9; height: 2; rotation: 45; color: uiTheme.themeColors.text }
                                    Rectangle { x: 10; y: 11; width: 9; height: 2; rotation: -45; color: uiTheme.themeColors.text }
                                    Rectangle { x: 2; y: 22; width: 20; height: 2; radius: 1; color: uiTheme.themeColors.text }
                                }
                            }
                            Item {
                                visible: modelData === "settings"; width: visible ? 23 * root.uiScale : 0; height: 23 * root.uiScale; anchors.verticalCenter: parent.verticalCenter
                                KeyboardGlyph {
                                    keyName: "@settings"; ink: uiTheme.themeColors.text; width: parent.width; height: parent.height
                                }
                            }
                            Text { anchors.verticalCenter: parent.verticalCenter; visible: modelData === "clock" || modelData === "battery"; text: modelData === "clock" ? root.clock : !root.bridge.systemState.has_battery ? "AC" : root.bridge.systemState.battery >= 0 ? root.bridge.systemState.battery + "%" : "?"; font.pixelSize: 15 * root.uiScale; color: uiTheme.themeColors.text }
                        }
                    }
                    onClicked: { root.selectedIndex = index; root.showPanel(modelData) }
                }
            }
            FluidButton {
                objectName: "globalStatusClose"
                text: "×"
                Accessible.name: qsTranslate("NGS", "Close panel")
                width: Math.max(48, 56 * root.uiScale)
                height: Math.max(48, 56 * root.uiScale)
                highlighted: root.selectedIndex === root.statusKeys.length
                contentItem: Item {
                    Item {
                        anchors.centerIn: parent
                        width: 24 * root.uiScale; height: 24 * root.uiScale
                        Rectangle { anchors.centerIn: parent; width: 20 * root.uiScale; height: 2.4 * root.uiScale; radius: height / 2; rotation: 45; color: uiTheme.themeColors.text }
                        Rectangle { anchors.centerIn: parent; width: 20 * root.uiScale; height: 2.4 * root.uiScale; radius: height / 2; rotation: -45; color: uiTheme.themeColors.text }
                    }
                }
                onClicked: { root.selectedIndex = root.statusKeys.length; root.bridge.hide() }
            }
        }
        Rectangle {
            objectName: "statusBarHandle"
            anchors.horizontalCenter: parent.horizontalCenter
            y: parent.height - 10 * root.uiScale + root.handleStretch
            width: Math.max(46 * root.uiScale, (72 - Math.min(24, Math.abs(root.handleStretch) * 0.45)) * root.uiScale)
            height: (4 + Math.min(3, Math.max(0, root.handleStretch) * 0.08)) * root.uiScale
            radius: height / 2
            color: root.bridge.keyboard.palette.panel_text
            opacity: 0.78
        }
        DragHandler {
            id: barDrag
            objectName: "statusBarDrag"
            target: null
            dragThreshold: 3
            minimumPointCount: 1; maximumPointCount: 1
            xAxis.enabled: false
            onActiveTranslationChanged: if (active) {
                const dy = activeTranslation.y
                if (dy < 0) {
                    root.pullOffset = Math.max(-(bar.height + 16 * root.uiScale), dy)
                    root.handleStretch = Math.max(-6 * root.uiScale, dy * 0.12)
                } else {
                    root.pullOffset = Math.min(18 * root.uiScale, dy * 0.25)
                    root.handleStretch = Math.min(44 * root.uiScale, dy * 0.65)
                }
            }
            onActiveChanged: if (!active) {
                const dismiss = root.pullOffset < -22 * root.uiScale
                root.handleStretch = 0
                if (dismiss) root.bridge.hide()
                else root.pullOffset = 0
            }
        }
    }
    FluidSurface {
        id: statusSelectionBadge
        objectName: "statusSelectionBadge"
        anchors.horizontalCenter: bar.horizontalCenter
        y: bar.y + bar.height + Math.max(0, root.handleStretch) + 8 * root.uiScale + (1 - root.hintReveal) * -6 * root.uiScale
        visible: root.presenting && !panel.visible && root.reveal > 0.6
        opacity: Math.max(0, Math.min(1, root.reveal)) * root.hintReveal
        scale: 0.94 + 0.06 * root.hintReveal
        width: statusSelectionText.implicitWidth + 32 * root.uiScale
        height: 32 * root.uiScale
        radius: height / 2
        color: uiTheme.themeColors.glass
        glass: true
        Behavior on width { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
        Row {
            anchors.centerIn: parent
            spacing: 7 * root.uiScale
            Rectangle {
                width: 6 * root.uiScale; height: 6 * root.uiScale; radius: width / 2
                color: uiTheme.themeColors.accent
                anchors.verticalCenter: parent.verticalCenter
            }
            Text {
                id: statusSelectionText
                objectName: "statusSelectionText"
                text: root.selectedLabel
                textFormat: Text.PlainText
                font.pixelSize: 13 * root.uiScale
                font.weight: Font.Medium
                color: uiTheme.themeColors.text
                anchors.verticalCenter: parent.verticalCenter
            }
        }
    }
    SystemPanel {
        id: panel
        bridge: root.bridge; uiScale: root.uiScale
        anchorX: root.width / 2; anchorY: bar.height + 24 * root.uiScale
        viewportHeight: root.screen.height * .75
    }
}
