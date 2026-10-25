import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Effects

Rectangle {
    id: root
    objectName: "playingBackground"
    required property QtObject bridge
    property real uiScale: 1
    property var launchCard: ({})
    property bool launching: false
    property real contentReveal: 1
    readonly property var state: bridge.runningState
    readonly property var hints: bridge.inputHints
    readonly property real unit: Math.max(.65, Math.min(uiScale, height / 680))
    property int selectedAction: 0
    property bool confirming: false
    property string confirmSession: ""
    property bool confirmReset: false
    readonly property bool frozen: bridge.gameFrozen
    readonly property int minimizeAction: frozen ? 6 : 4
    readonly property int resetAction: minimizeAction + 1
    readonly property string error: bridge.powerError || state.error
    readonly property var game: launching && !state.running ? launchCard : bridge.localGames.find(card => card.id === state.id)
        || bridge.games.find(card => card.id === state.id)
        || (launchCard.id === state.id || launching ? launchCard : {})
    color: uiTheme.themeColors.background
    clip: true
    MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }
    Image {
        anchors.fill: parent; source: root.game.backgroundImage || root.game.image || ""
        fillMode: Image.PreserveAspectCrop; sourceSize.width: 1600; asynchronous: true
        opacity: uiTheme.themeColors.light ? .10 : .16
    }

    function posterRectangle() {
        const poster = posterFrame
        const position = poster.mapToItem(root, 0, 0)
        return Qt.rect(position.x, position.y, poster.width, poster.height)
    }

    function duration(seconds) {
        const value = Math.max(0, Math.floor(seconds || 0))
        const h = Math.floor(value / 3600), m = Math.floor(value % 3600 / 60), s = value % 60
        return h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0")
    }
    function activate() {
        if (bridge.launchingGame) { bridge.cancelOperation(); return }
        if (state.closing || state.memory_busy) return
        if (!state.running && selectedAction !== 0) return
        if (confirming) {
            if (selectedAction === 1 && confirmSession === state.session_id) {
                if (confirmReset) bridge.resetGameStatistics(confirmSession)
                else bridge.forceCloseGame()
            }
            confirming = false; selectedAction = 0
        } else if (selectedAction === 0) {
            if (bridge.powerError && !frozen) bridge.dismissPowerError()
            else if (state.running) bridge.returnToGame(); else bridge.dismissPowerError()
        } else if (selectedAction === 1) bridge.toggleGameFreeze()
        else if (selectedAction === 2) bridge.minimizeGame()
        else if (selectedAction === 3 || selectedAction === resetAction) { confirmReset = selectedAction === resetAction; confirming = true; confirmSession = state.session_id; selectedAction = 0 }
        else if (selectedAction === minimizeAction) bridge.minimize()
        else if (selectedAction === 4) bridge.showGameLibrary()
        else if (selectedAction === 5) bridge.trimGameMemory()
    }
    function handleAction(action) {
        if (bridge.launchingGame) {
            if (action === "back" || action === "activate") bridge.cancelOperation()
            return
        }
        if (action === "back") { confirming = false; selectedAction = 0; return }
        if (action === "activate") { activate(); return }
        if (!confirming && (action === "toggle_installed" || action === "refresh")) {
            selectedAction = action === "toggle_installed" ? 1 : 2
            activate(); return
        }
        if (confirming) {
            if (action === "left" || action === "right") selectedAction = 1 - selectedAction
        } else if (action === "left") selectedAction = Math.max(0, selectedAction - selectedAction % 2)
        else if (action === "right") selectedAction = Math.min(resetAction, selectedAction + (selectedAction % 2 === 0 ? 1 : 0))
        else if (action === "up" && selectedAction >= 2) selectedAction -= 2
        else if (action === "down" && selectedAction < minimizeAction) selectedAction = Math.min(resetAction, selectedAction + 2)
        else if (action === "up" || action === "down") {
            const view = information.contentItem
            view.contentY = Math.max(0, Math.min(Math.max(0, view.contentHeight - view.height), view.contentY + (action === "down" ? 100 : -100) * unit))
        }
    }
    onVisibleChanged: { confirming = false; selectedAction = 0; if (visible) information.contentItem.contentY = 0 }
    onFrozenChanged: { confirming = false; selectedAction = 0 }
    function actionHint(index) {
        if (confirming) return index === 0 ? hints.back : selectedAction === index ? hints.activate : ""
        if (index === 1) return hints.toggle
        if (index === 2) return hints.refresh
        return selectedAction === index ? hints.activate : ""
    }
    Connections {
        target: root.bridge
        function onGameStarted() { root.confirming = false; root.selectedAction = 0 }
        function onGamePanelRequested() { if (root.visible) presentation.replay() }
    }

    ColumnLayout {
        anchors.fill: parent; anchors.margins: Math.max(16, 28 * root.unit)
        opacity: root.contentReveal
        spacing: 12 * root.unit
        Text {
            Layout.fillWidth: true
            text: root.state.closing ? qsTranslate("NGS", "Closing game…") : root.frozen ? qsTranslate("NGS", "Game frozen") : root.launching && !root.state.running ? qsTranslate("NGS", "Starting game…") : qsTranslate("NGS", "Now playing")
            color: uiTheme.themeColors.muted; font.pixelSize: Math.max(15, 18 * root.unit)
        }
        ScrollView {
            id: information; objectName: "runningInformation"
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 0
            clip: true; contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        Row {
            id: presentation; objectName: "runningPresentation"; width: information.availableWidth; spacing: 28 * root.unit
            property real reveal: 0
            function replay() { if (root.visible) entrance.restart() }
            opacity: Math.min(1, reveal)
            transform: Translate { y: (1 - presentation.reveal) * 22 * root.unit }
            Connections {
                target: root
                function onVisibleChanged() {
                    if (root.visible) entrance.restart()
                    else { entrance.stop(); presentation.reveal = 0 }
                }
            }
            Component.onCompleted: if (root.visible) entrance.start()
            NumberAnimation {
                id: entrance; target: presentation; property: "reveal"; from: 0; to: 1
                duration: 460; easing.type: Easing.OutBack; easing.overshoot: .55
            }
            Item {
                id: posterFrame
                width: Math.min(presentation.width * .24, 210 * root.unit); height: width * 1.5
                opacity: root.launching && root.contentReveal < 1 ? 0 : 1
                Image {
                    id: poster
                    objectName: root.objectName === "frozenDashboard" ? "frozenGameCover" : "runningGameCover"
                    anchors.fill: parent; source: root.game.image || "../assets/local.svg"
                    fillMode: Image.PreserveAspectFit; asynchronous: true; sourceSize.width: 640
                    layer.enabled: GraphicsInfo.api !== GraphicsInfo.Software; layer.smooth: true
                    layer.effect: MultiEffect {
                        maskEnabled: true; maskThresholdMin: .5; maskSpreadAtMin: 1
                        maskSource: Rectangle {
                            parent: posterFrame; width: posterFrame.width; height: posterFrame.height
                            radius: 12 * root.unit; color: "white"; visible: false; layer.enabled: true; layer.smooth: true
                        }
                    }
                }
            }
            Column {
                width: presentation.width - posterFrame.width - presentation.spacing; spacing: 20 * root.unit
                Text {
                    objectName: root.objectName === "frozenDashboard" ? "frozenGameTitle" : "runningGameTitle"
                    width: parent.width
                    text: root.launching && !root.state.running ? root.game.name || qsTranslate("NGS", "Game")
                        : root.state.name || root.game.name || root.bridge.operationGame || qsTranslate("NGS", "Game")
                    textFormat: Text.PlainText; wrapMode: Text.WordWrap
                    color: uiTheme.themeColors.text; font.pixelSize: Math.max(26, Math.min(48 * root.unit, width * .08))
                    font.weight: Font.DemiBold; font.letterSpacing: -.7 * root.unit; lineHeight: 1.08
                }
        Grid {
            objectName: "runningStatistics"
            width: parent.width; columns: 3; spacing: 10 * root.unit
            Repeater {
                model: [
                    {label:qsTranslate("NGS", "Current session"), value:root.duration(root.state.session_seconds)},
                    {label:qsTranslate("NGS", "Total playtime"), value:root.duration(root.state.total_seconds)},
                    {label:qsTranslate("NGS", "Total launches"), value:String(root.state.launches)},
                    {label:qsTranslate("NGS", "Frozen / asleep this session"), value:root.duration(root.state.paused_seconds)},
                    {label:qsTranslate("NGS", "Longest session"), value:root.duration(root.state.longest_seconds)},
                    {label:qsTranslate("NGS", "Session started"), value:root.state.started_at ? new Date(root.state.started_at * 1000).toLocaleTimeString(Qt.locale(), "HH:mm") : "—"}
                ]
                FluidSurface {
                    required property var modelData
                    width: (parent.width - parent.spacing * 2) / 3; height: Math.max(74, 86 * root.unit)
                    radius: 16; color: "transparent"; border.width: 0; shadowEnabled: false
                    Column {
                        anchors.fill: parent; anchors.margins: 4 * root.unit; spacing: 6 * root.unit
                        Text { text: modelData.label; width: parent.width; height: Math.max(28, 34 * root.unit); color: uiTheme.themeColors.muted; font.pixelSize: Math.max(12, 14 * root.unit); wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight }
                        Text { text: modelData.value; width: parent.width; color: uiTheme.themeColors.text; font.pixelSize: Math.max(21, 28 * root.unit); font.weight: Font.DemiBold; elide: Text.ElideRight }
                    }
                }
            }
        }
        }
        }
        }
        Text {
            Layout.fillWidth: true; text: root.confirming ? root.confirmReset ? qsTranslate("NGS", "Reset this game's playtime, launches and longest session on this device? Shared profile statistics are preserved. Tracking restarts while the game keeps running.") : qsTranslate("NGS", "Force-close the game? Unsaved progress will be lost.")
                : root.error || root.state.memory_status || (root.frozen ? qsTranslate("NGS", "Playtime tracking is paused. Trimming memory reduces the working set; returning may require reloading data.")
                : qsTranslate("NGS", "Minimizing does not stop the game. Sleep and frozen time are excluded from playtime."))
            textFormat: Text.PlainText; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter
            color: root.error || root.confirming ? uiTheme.themeColors.warning : uiTheme.themeColors.muted; font.pixelSize: Math.max(13, 16 * root.unit)
        }
        Grid {
            Layout.fillWidth: true; Layout.preferredHeight: implicitHeight; columns: 2; spacing: 12 * root.unit
            Repeater {
                model: root.confirming ? [qsTranslate("NGS", "Cancel", "Отмена"), root.confirmReset ? qsTranslate("NGS", "Reset on this device") : qsTranslate("NGS", "Force close")]
                    : [root.bridge.powerError && !root.frozen ? qsTranslate("NGS", "Got it") : root.frozen ? qsTranslate("NGS", "Unfreeze and return") : root.bridge.launchingGame ? qsTranslate("NGS", "Cancel launch") : root.state.running ? qsTranslate("NGS", "Return to game") : qsTranslate("NGS", "Got it"),
                       root.frozen ? qsTranslate("NGS", "Unfreeze here") : qsTranslate("NGS", "Freeze game"), qsTranslate("NGS", "Force minimize game"), qsTranslate("NGS", "Force close")].concat(root.frozen ? [qsTranslate("NGS", "Return to launcher"), root.state.memory_busy ? qsTranslate("NGS", "Trimming memory…") : qsTranslate("NGS", "Trim game memory")] : []).concat([qsTranslate("NGS", "Minimize NGS"), qsTranslate("NGS", "Reset statistics")])
                FluidButton {
                    id: actionButton
                    required property string modelData; required property int index
                    objectName: root.confirming ? "runningConfirm-" + index : index === root.minimizeAction ? "minimizeNgsButton" : index === root.resetAction ? "resetGameStatisticsButton" : ["thawGameButton", "freezeGameButton", "minimizeGameButton", "forceCloseGameButton", "backToLauncherButton", "trimGameMemoryButton"][index]
                    width: (parent.width - parent.spacing) / 2; height: Math.max(54, 70 * root.unit)
                    text: modelData; font.pixelSize: Math.max(14, 18 * root.unit)
                    readonly property string shortcut: root.actionHint(index)
                    Accessible.name: text
                    Accessible.description: shortcut ? qsTranslate("NGS", "Quick shortcut: ") + shortcut : root.hints.activate + qsTranslate("NGS", " — select")
                    contentItem: Item {
                        Item {
                            width: 28 * root.unit; height: width; anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                            KeyboardGlyph {
                                objectName: "runningActionIcon-" + actionButton.index
                                width: parent.width; height: width
                                keyName: root.confirming ? actionButton.index === 0 ? "@back" : root.confirmReset ? "@sync" : "@stop"
                                    : actionButton.index === root.minimizeAction ? "@minimize" : actionButton.index === root.resetAction ? "@sync" : ["@play", root.frozen ? "@play" : "@pause", "@minimize", "@stop", "@library", "@memory"][actionButton.index]
                                ink: uiTheme.themeColors.text
                            }
                        }
                        Text {
                            objectName: "runningActionLabel-" + actionButton.index
                            anchors.left: parent.left; anchors.leftMargin: 40 * root.unit
                            anchors.right: shortcutBadge.left; anchors.rightMargin: 10 * root.unit; anchors.verticalCenter: parent.verticalCenter
                            text: actionButton.text; font: actionButton.font; color: uiTheme.themeColors.text
                            wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
                        }
                        Rectangle {
                            id: shortcutBadge; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                            width: actionButton.shortcut ? Math.max(28, shortcutLabel.implicitWidth + 16) : 0
                            height: Math.max(26, 30 * root.unit); radius: 7; color: uiTheme.themeColors.soft
                            visible: actionButton.shortcut.length > 0
                            Text {
                                id: shortcutLabel; objectName: "runningActionHint-" + actionButton.index
                                anchors.centerIn: parent; text: actionButton.shortcut; color: uiTheme.themeColors.text
                                font.pixelSize: Math.max(12, 14 * root.unit); font.weight: Font.DemiBold
                            }
                        }
                    }
                    enabled: !root.state.closing && !root.state.memory_busy && (root.state.running || index === 0)
                    highlighted: root.selectedAction === index
                    onClicked: { root.selectedAction = index; root.activate() }
                }
            }
        }
        Text {
            Layout.fillWidth: true; text: (root.state.local_only ? qsTranslate("NGS", "Local device statistics") : root.bridge.playerAccount.state.logged_in ? qsTranslate("NGS", "Profile ") + root.bridge.playerAccount.state.username + " · " + root.bridge.playerAccount.state.status : qsTranslate("NGS", "Sign in through Settings to view shared statistics")) + " · " + root.hints.activate + qsTranslate("NGS", " Select · ") + root.hints.back + qsTranslate("NGS", " Back")
            horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: Math.max(12, 14 * root.unit)
        }
    }
}
