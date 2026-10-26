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
    readonly property var preview: state.preview || ({})
    readonly property bool hasPreview: !!preview.id
    onHasPreviewChanged: if (hasPreview) { selectedField = 0; previewDescription.contentY = 0 }
    property int elapsedSeconds: 0
    property double startedAt: 0
    onBusyChanged: if (busy) { startedAt = Date.now(); elapsedSeconds = 0 }
    Timer {
        interval: 1000; repeat: true; running: root.visible && root.busy
        onTriggered: root.elapsedSeconds = Math.floor((Date.now() - root.startedAt) / 1000)
    }
    property int currentIndex: 0
    pageScale: uiScale
    covered: customEditor.visible
    onClosed: customEditor.close()

    QtObject {
        id: customAdapter
        property var state: Object.assign({preview: "", card: ({}), artwork: ({})}, root.state.custom || ({}),
                                          {busy: !!root.state.custom_saving, status: root.state.custom_status || ""})
        signal customSaved()
        function saveCustomCard(name, description, cover, background) { root.bridge.saveCustomMetadata(name, description, cover, background) }
    }
    ManualCardEditor {
        id: customEditor; objectName: "localArtworkEditor"; parent: root.parent
        bridge: customAdapter; keyboard: root.bridge.keyboard; hints: root.hints; uiScale: root.uiScale
    }
    function openCustom() { query.focus = false; if (bridge.editCustomMetadata()) customEditor.openEditor() }


    function back() {
        if (state.applying) return
        if (bridge.operationBusy && !bridge.downloadActive) bridge.cancelOperation()
        else if (hasPreview) { bridge.clearMetadataPreview(); selectedField = 2 }
        else close()
    }
    function editQuery() { selectedField = 0; query.forceActiveFocus(); bridge.keyboard.show() }
    function choose(index) {
        query.focus = false
        if (index >= 0 && index < state.results.length) bridge.previewMetadata(state.results[index].app_id)
    }
    function handleAction(action) {
        if (customEditor.visible) { customEditor.handleAction(action); return }
        if (action === "back" || action === "menu") { back(); return }
        if (bridge.operationBusy) {
            if (busy && !state.applying) {
                if (["left", "right", "up", "down"].indexOf(action) >= 0) selectedField = selectedField === 4 ? 3 : 4
                else if (action === "activate") { if (selectedField === 4) openCustom(); else back() }
            }
            return
        }
        if (hasPreview) {
            if (action === "left" || action === "right") selectedField = selectedField === 0 ? 1 : 0
            else if (action === "up" || action === "down") previewDescription.contentY = Math.max(0, Math.min(Math.max(0, previewDescription.contentHeight-previewDescription.height), previewDescription.contentY + (action === "down" ? 60 : -60) * root.uiScale))
            else if (action === "activate") { if (selectedField === 0) bridge.applyMetadataPreview(); else back() }
            return
        }
        if (action === "up" || action === "down") {
            query.focus = false
            if (selectedField === 2 && action === "up" && currentIndex > 0) --currentIndex
            else if (selectedField === 2 && action === "down" && currentIndex + 1 < state.results.length) ++currentIndex
            else selectedField = (selectedField + (action === "down" ? 1 : 4)) % 5
            results.positionViewAtIndex(currentIndex, ListView.Contain)
        } else if (action === "left" || action === "right") { query.focus = false; selectedField = (selectedField + (action === "right" ? 1 : 4)) % 5 }
        else if (action === "activate") {
            if (selectedField === 0) editQuery()
            else if (selectedField === 1) bridge.searchMetadata(query.text)
            else if (selectedField === 2) choose(currentIndex)
            else if (selectedField === 4) openCustom()
            else back()
        }
        else if (action === "refresh") { query.focus = false; bridge.searchMetadata(query.text) }
        else if (action === "toggle_installed") editQuery()
    }
    Connections {
        target: root.bridge
        function onMetadataRequested() { query.text = root.state.query; root.currentIndex = 0; root.selectedField = 2; root.open() }
        function onMetadataApplied() { customAdapter.customSaved(); root.close() }
    }
    contentItem: ColumnLayout {
        spacing: 12
        Label { text: root.hasPreview ? qsTranslate("NGS", "Review artwork") : qsTranslate("NGS", "Find artwork"); color: uiTheme.themeColors.text; font.pixelSize: 32 * root.uiScale; font.weight: Font.Light }
        Row {
            visible: !root.hasPreview
            Layout.fillWidth: true; Layout.preferredHeight: 52 * root.uiScale; spacing: 12
            TextField {
                id: query; objectName: "metadataQuery"; width: parent.width - searchButton.width - parent.spacing; height: parent.height; maximumLength: 160
                placeholderText: qsTranslate("NGS", "Game title or store link"); selectByMouse: true; color: uiTheme.themeColors.text
                onActiveFocusChanged: if (activeFocus) root.selectedField = 0
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
            id: results; visible: !root.busy && !root.hasPreview; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
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
        RowLayout {
            visible: root.hasPreview && !root.busy; Layout.fillWidth: true; Layout.fillHeight: true; spacing: 20 * root.uiScale
            Image {
                objectName: "metadataPreviewCover"; Layout.preferredWidth: Math.min(220 * root.uiScale, root.availableWidth * .3); Layout.fillHeight: true
                source: root.preview.image || ""; sourceSize.width: 600; sourceSize.height: 900
                fillMode: Image.PreserveAspectFit; asynchronous: true
            }
            ColumnLayout {
                Layout.fillWidth: true; Layout.fillHeight: true; spacing: 12 * root.uiScale
                Label { objectName: "metadataPreviewTitle"; Layout.fillWidth: true; text: root.preview.name || ""; textFormat: Text.PlainText; wrapMode: Text.WordWrap; color: uiTheme.themeColors.text; font.pixelSize: 24 * root.uiScale }
                Image { objectName: "metadataPreviewBackground"; Layout.fillWidth: true; Layout.preferredHeight: Math.min(160 * root.uiScale, root.availableHeight * .25); source: root.preview.backgroundImage || ""; sourceSize.width: 1024; sourceSize.height: 512; fillMode: Image.PreserveAspectCrop; asynchronous: true }
                Flickable {
                    id: previewDescription; objectName: "metadataPreviewDescription"; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                    contentWidth: width; contentHeight: description.implicitHeight; boundsBehavior: Flickable.StopAtBounds
                    Text { id: description; width: parent.width; text: root.preview.description || ""; textFormat: Text.PlainText; wrapMode: Text.WordWrap; color: uiTheme.themeColors.text; font.pixelSize: 17 * root.uiScale }
                    ScrollBar.vertical: ScrollBar {}
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            FluidButton { objectName: "metadataApply"; visible: root.hasPreview; enabled: !root.bridge.operationBusy; text: qsTranslate("NGS", "Use this game"); highlighted: root.selectedField === 0; Layout.fillWidth: true; onClicked: root.bridge.applyMetadataPreview() }
            FluidButton { objectName: "metadataCustom"; visible: !root.hasPreview; enabled: !root.state.applying && (!root.bridge.operationBusy || root.busy); text: qsTranslate("NGS", "Add artwork manually"); highlighted: root.selectedField === 4; Layout.fillWidth: true; onClicked: root.openCustom() }
            FluidButton { objectName: "metadataBack"; text: root.state.applying ? qsTranslate("NGS", "Saving artwork…") : root.state.cancelling && root.busy ? qsTranslate("NGS", "Canceling…") : root.busy ? qsTranslate("NGS", "Cancel · ") + root.hints.back : root.hasPreview ? qsTranslate("NGS", "Back to results") : qsTranslate("NGS", "Back · ") + root.hints.back;
                enabled: !root.state.applying && (!root.busy || !root.state.cancelling); Layout.fillWidth: true
                highlighted: root.hasPreview ? root.selectedField === 1 || root.busy : root.selectedField === 3 || (root.busy && root.selectedField !== 4); focusPolicy: Qt.NoFocus; onClicked: root.back() }
        }
    }
}
