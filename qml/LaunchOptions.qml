import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ToolPage {
    id: root
    objectName: "launchOptionsPane"
    required property QtObject bridge
    property string gameId: ""
    property string gameName: ""
    property bool ready: false
    property string status: ""
    property int selectedField: 0

    function openGame(identity, name) {
        gameId = identity; gameName = name; selectedField = 0
        const state = bridge.launchOptions(identity)
        launchArguments.text = state.arguments; ready = state.ready; status = state.status
        open()
    }
    function save() {
        if (!ready) return
        if (bridge.setLaunchArguments(gameId, launchArguments.text)) close()
        else status = bridge.operationStatus
    }
    function handleAction(action) {
        if (action === "back" || action === "menu") close()
        else if (["up", "down", "left", "right"].indexOf(action) >= 0) {
            launchArguments.focus = false
            selectedField = (selectedField + (action === "up" || action === "left" ? 2 : 1)) % 3
        } else if (action === "activate") {
            if (selectedField === 0) { launchArguments.forceActiveFocus(); bridge.keyboard.show() }
            else if (selectedField === 1) save()
            else close()
        }
    }
    onAboutToShow: Qt.callLater(launchArguments.forceActiveFocus)
    onAboutToHide: bridge.keyboard.hide()

    contentItem: ColumnLayout {
        spacing: 12
        Label { text: qsTranslate("NGS", "Launch options"); color: uiTheme.themeColors.text; font.pixelSize: 32; font.weight: Font.Light }
        Label { text: root.gameName; textFormat: Text.PlainText; Layout.fillWidth: true; elide: Text.ElideRight; color: uiTheme.themeColors.accent; font.pixelSize: 18 }
        Label { text: qsTranslate("NGS", "Additional arguments after the executable. Quote values containing spaces. Leave empty to disable additional arguments."); Layout.fillWidth: true; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 14 }
        TextField {
            id: launchArguments
            objectName: "launchArgumentsField"
            Layout.fillWidth: true; Layout.preferredHeight: 48
            maximumLength: 8192; selectByMouse: true; enabled: root.ready
            placeholderText: '-windowed -novid -language "Russian"'
            font.pixelSize: 16
            background: FluidSurface { radius: 8; border.width: 0; color: root.selectedField === 0 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
            onAccepted: root.save()
        }
        Label { text: root.status; textFormat: Text.PlainText; visible: text.length > 0; Layout.fillWidth: true; wrapMode: Text.WordWrap; color: uiTheme.themeColors.warning; font.pixelSize: 14; maximumLineCount: 3; elide: Text.ElideRight }
        Item { Layout.fillHeight: true }
        RowLayout {
            Layout.fillWidth: true; spacing: 12
            FluidButton { objectName: "saveLaunchArguments"; text: qsTranslate("NGS", "Save"); Layout.fillWidth: true; Layout.preferredHeight: 44; enabled: root.ready; highlighted: root.selectedField === 1; focusPolicy: Qt.NoFocus; onClicked: root.save() }
            FluidButton { text: qsTranslate("NGS", "Cancel", "Отмена"); Layout.fillWidth: true; Layout.preferredHeight: 44; highlighted: root.selectedField === 2; focusPolicy: Qt.NoFocus; onClicked: root.close() }
        }
    }
}
