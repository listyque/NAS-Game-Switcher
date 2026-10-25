import QtQuick
import QtQuick.Controls.Basic

ToolPage {
    id: root
    objectName: "directoryPicker"
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    readonly property var state: bridge.scannerState
    readonly property var rows: (state.path ? [{name: qsTranslate("NGS", ".. Up one level"), path: state.parent}] : []).concat(state.folders)
    property int currentIndex: 0
    property int actionIndex: -1
    signal chosen(string path)


    function browse(path) { bridge.browseLocalFolder(path, false) }
    function openFolder(path) { currentIndex = 0; actionIndex = -1; open(); browse(path === "games" ? bridge.directoryLabel(path) : path) }
    function back() { if (bridge.operationBusy && !bridge.downloadActive) bridge.cancelOperation(); else close() }
    function choose() { if (state.path && !bridge.operationBusy) { chosen(state.path); close() } }
    function handleAction(action) {
        if (action === "back" || action === "menu") { back(); return }
        if (bridge.operationBusy) return
        if (action === "down") {
            if (actionIndex < 0 && currentIndex + 1 < rows.length) ++currentIndex
            else actionIndex = 0
        } else if (action === "up") {
            if (actionIndex >= 0) actionIndex = -1
            else currentIndex = Math.max(0, currentIndex - 1)
        } else if (action === "left" || action === "right") {
            if (actionIndex >= 0) actionIndex = 1 - actionIndex
            else if (action === "left" && state.path) browse(state.parent)
            else if (action === "right" && rows.length) browse(rows[currentIndex].path)
        } else if (action === "activate") {
            if (actionIndex === 0) choose()
            else if (actionIndex === 1) back()
            else if (rows.length) browse(rows[currentIndex].path)
            else actionIndex = 0
        } else if (action === "toggle_installed") browse("")
        else if (action === "refresh") choose()
        folders.positionViewAtIndex(currentIndex, ListView.Contain)
    }
    Connections { target: root.bridge; function onScannerChanged() { root.currentIndex = 0 } }
    contentItem: Column {
        spacing: 12
        Label { text: qsTranslate("NGS", "Choose folder"); font.pixelSize: 32; font.weight: Font.Light; color: uiTheme.themeColors.text }
        Label { text: root.state.path || (qsTranslate("NGS", "Device drives · ") + root.hints.toggle + qsTranslate("NGS", " — back to drives")); textFormat: Text.PlainText; width: parent.width; elide: Text.ElideMiddle; color: uiTheme.themeColors.muted }
        Label { text: root.state.status; width: parent.width; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted }
        ListView {
            id: folders; width: parent.width; height: Math.max(48, root.availableHeight - y - folderActions.height - 12); clip: true; spacing: 5; model: root.rows
            ScrollBar.vertical: ScrollBar {}
            delegate: FluidButton {
                required property var modelData
                required property int index
                width: folders.width - 16; height: 56; text: modelData.name; font.pixelSize: 18
                highlighted: root.actionIndex < 0 && root.currentIndex === index
                focusPolicy: Qt.NoFocus; enabled: !root.bridge.operationBusy
                onClicked: { root.currentIndex = index; root.browse(modelData.path) }
            }
        }
        Row {
            id: folderActions
            spacing: 16
            FluidButton { text: qsTranslate("NGS", "Select this folder · ") + root.hints.refresh; highlighted: root.actionIndex === 0; focusPolicy: Qt.NoFocus; enabled: !!root.state.path && !root.bridge.operationBusy; onClicked: root.choose() }
            FluidButton { text: qsTranslate("NGS", "Back · ") + root.hints.back; highlighted: root.actionIndex === 1; focusPolicy: Qt.NoFocus; onClicked: root.back() }
        }
    }
}
