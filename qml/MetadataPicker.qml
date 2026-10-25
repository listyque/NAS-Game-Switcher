import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ToolPage {
    id: root
    objectName: "metadataPicker"
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    property real uiScale: 1
    property int selectedField: 2
    readonly property var state: bridge.metadataState
    readonly property bool busy: bridge.operationBusy && !bridge.downloadActive
    property int elapsedSeconds: 0
    onBusyChanged: if (busy) elapsedSeconds = 0
    Timer {
        interval: 1000; repeat: true; running: root.visible && root.busy
        onTriggered: ++root.elapsedSeconds
    }
    property int currentIndex: 0
    pageScale: uiScale


    function back() { if (bridge.operationBusy && !bridge.downloadActive) bridge.cancelOperation(); else close() }
    function choose(index) {
        query.focus = false
        if (index >= 0 && index < state.results.length) bridge.resolveMetadata(state.results[index].app_id)
    }
    function handleAction(action) {
        if (action === "back" || action === "menu") { back(); return }
        if (bridge.operationBusy) return
        if (action === "up" || action === "down") {
            query.focus = false
            if (selectedField === 2 && action === "up" && currentIndex > 0) --currentIndex
            else if (selectedField === 2 && action === "down" && currentIndex + 1 < state.results.length) ++currentIndex
            else selectedField = (selectedField + (action === "down" ? 1 : 3)) % 4
            results.positionViewAtIndex(currentIndex, ListView.Contain)
        } else if (action === "left" || action === "right") { query.focus = false; selectedField = (selectedField + (action === "right" ? 1 : 3)) % 4 }
        else if (action === "activate") {
            if (selectedField === 0) { query.forceActiveFocus(); bridge.keyboard.show() }
            else if (selectedField === 1) bridge.searchMetadata(query.text)
            else if (selectedField === 2) choose(currentIndex)
            else back()
        }
        else if (action === "refresh") { query.focus = false; bridge.searchMetadata(query.text) }
        else if (action === "toggle_installed") query.forceActiveFocus()
    }
    Connections {
        target: root.bridge
        function onMetadataRequested() { query.text = root.state.query; root.currentIndex = 0; root.selectedField = 2; root.open() }
        function onMetadataApplied() { root.close() }
    }
    contentItem: ColumnLayout {
        spacing: 12
        Label { text: qsTranslate("NGS", "Find artwork"); color: uiTheme.themeColors.text; font.pixelSize: 32 * root.uiScale; font.weight: Font.Light }
        Row {
            Layout.fillWidth: true; Layout.preferredHeight: 52 * root.uiScale; spacing: 12
            TextField {
                id: query; objectName: "metadataQuery"; width: parent.width - searchButton.width - parent.spacing; height: parent.height; maximumLength: 160
                placeholderText: qsTranslate("NGS", "Game title · ") + root.hints.toggle + qsTranslate("NGS", " — edit", " — редактировать"); selectByMouse: true
                background: FluidSurface { radius: 8; border.width: 0; color: root.selectedField === 0 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
                onAccepted: { focus = false; root.bridge.searchMetadata(text) }
            }
            FluidButton { id: searchButton; text: qsTranslate("NGS", "Search · ", "Найти · ") + root.hints.refresh; height: parent.height; highlighted: root.selectedField === 1; enabled: !root.bridge.operationBusy; focusPolicy: Qt.NoFocus; onClicked: { query.focus = false; root.bridge.searchMetadata(query.text) } }
        }
        Label { visible: !root.busy; text: root.state.status; Layout.fillWidth: true; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 16 * root.uiScale }
        Item {
            objectName: "metadataLoading"
            visible: root.busy; Layout.fillWidth: true; Layout.fillHeight: true
            Column {
                anchors.centerIn: parent; width: parent.width; spacing: 14 * root.uiScale
                ActivityIndicator {
                    objectName: "metadataSpinner"; anchors.horizontalCenter: parent.horizontalCenter
                    width: 52 * root.uiScale; height: width; running: root.visible && root.busy
                    palette.dark: uiTheme.themeColors.accent
                }
                Label {
                    text: root.state.selected_name || root.state.query
                    width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
                    color: uiTheme.themeColors.text; font.pixelSize: 22 * root.uiScale
                }
                Label {
                    objectName: "metadataLoadingStatus"; text: root.state.status
                    width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
                    color: uiTheme.themeColors.accent; font.pixelSize: 17 * root.uiScale
                }
                Label {
                    objectName: "metadataElapsed"
                    text: qsTranslate("NGS", "Elapsed: ") + root.elapsedSeconds + qsTranslate("NGS", " s") + (root.elapsedSeconds >= 10 && !root.state.cancelling
                        ? qsTranslate("NGS", "\nThe provider is responding slowly. You can cancel and try again later.") : "")
                    width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
                    color: uiTheme.themeColors.muted; font.pixelSize: 14 * root.uiScale
                }
            }
        }
        ListView {
            id: results; visible: !root.busy; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            model: root.state.results; spacing: 8
            delegate: FluidButton {
                required property var modelData
                required property int index
                width: results.width; height: 48; text: modelData.name + (modelData.app_id.indexOf("epic:") === 0 ? " · Epic Games" : modelData.app_id.indexOf("gog:") === 0 ? " · GOG" : modelData.app_id.indexOf("lutris:") === 0 ? " · Lutris" : modelData.app_id.indexOf("wikipedia:") === 0 ? " · Wikipedia" : " · Steam")
                highlighted: root.selectedField === 2 && index === root.currentIndex; focusPolicy: Qt.NoFocus
                onClicked: { root.currentIndex = index; root.choose(index) }
            }
            ScrollBar.vertical: ScrollBar {}
        }
        FluidButton { objectName: "metadataBack"; text: root.state.cancelling && root.busy ? qsTranslate("NGS", "Canceling…") : root.busy ? qsTranslate("NGS", "Cancel · ") + root.hints.back : qsTranslate("NGS", "Back · ") + root.hints.back;
            enabled: !root.busy || !root.state.cancelling
            highlighted: root.selectedField === 3 || root.busy; focusPolicy: Qt.NoFocus; onClicked: root.back() }
    }
}
