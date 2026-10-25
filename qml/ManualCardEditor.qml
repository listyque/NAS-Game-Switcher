import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick.Dialogs
import "FocusNavigation.js" as Navigation

ToolPage {
    id: root
    objectName: "manualCardEditor"
    required property QtObject bridge
    required property QtObject keyboard
    required property var hints
    property real uiScale: 1
    property int selectedField: 0
    property int page: 0
    property string draftPreview: ""
    property string cover: ""
    property string backgroundImage: ""
    property string imageKind: "cover"
    readonly property var fields: [titleField, descriptionField, coverButton, backgroundButton, saveButton, backButton, nextButton]
    pageScale: uiScale

    function openEditor() {
        if (draftPreview !== bridge.state.preview) {
            draftPreview = bridge.state.preview
            titleField.text = bridge.state.card.name || ""
            descriptionField.text = bridge.state.card.description || ""
            cover = ""; backgroundImage = ""
        }
        page = 0; selectedField = 0; open()
    }
    function activate() {
        if (selectedField < 2) { fields[selectedField].forceActiveFocus(); keyboard.show() }
        else fields[selectedField].clicked()
    }
    function back() {
        titleField.focus = false; descriptionField.focus = false; keyboard.hide()
        if (page > 0) { page = 0; selectedField = 6 }
        else close()
    }
    function handleAction(action) {
        if (files.visible) { if (action === "back") files.close(); return }
        if (bridge.state.busy) return
        if (action === "back") back()
        else if (action === "menu") close()
        else if (["left", "right", "up", "down"].indexOf(action) >= 0) {
            titleField.focus = false; descriptionField.focus = false
            selectedField = Navigation.next(fields, selectedField, action, contentItem)
        } else if (action === "activate" || action === "toggle_installed") activate()
    }
    Connections { target: root.bridge; function onCustomSaved() { root.close() } }
    FileDialog {
        id: files
        title: root.imageKind === "cover" ? qsTranslate("NGS", "Choose cover", "Выбери обложку") : qsTranslate("NGS", "Choose background", "Выбери фон")
        nameFilters: [qsTranslate("NGS", "Images (*.png *.jpg *.jpeg *.webp)")]
        onAccepted: { if (root.imageKind === "cover") root.cover = selectedFile.toString(); else root.backgroundImage = selectedFile.toString() }
    }
    contentItem: ColumnLayout {
        spacing: 12 * root.uiScale
        Label { text: qsTranslate("NGS", "Custom artwork"); color: uiTheme.themeColors.text; font.pixelSize: 32 * root.uiScale; font.weight: Font.Light }
        WorkflowSteps { Layout.fillWidth: true; uiScale: root.uiScale; steps: [qsTranslate("NGS", "Title and description"), qsTranslate("NGS", "Images")]; current: root.page }
        Label { visible: root.page === 0; text: qsTranslate("NGS", "Title", "Название"); color: uiTheme.themeColors.muted }
        TextField {
            id: titleField; objectName: "manualCardTitle"; Layout.fillWidth: true; maximumLength: 160
            visible: root.page === 0; Layout.preferredHeight: 54 * root.uiScale
            color: uiTheme.themeColors.text
            background: FluidSurface { radius: 8; border.width: 0; color: root.selectedField === 0 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
        }
        Label { visible: root.page === 0; text: qsTranslate("NGS", "Description · up to 3,000 characters"); color: uiTheme.themeColors.muted }
        ScrollView {
            visible: root.page === 0
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 70 * root.uiScale
            TextArea {
                id: descriptionField; objectName: "manualCardDescription"; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText
                color: uiTheme.themeColors.text
                background: FluidSurface { radius: 8; border.width: 0; color: root.selectedField === 1 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
            }
        }
        RowLayout {
            visible: root.page === 1
            Layout.fillWidth: true; spacing: 16 * root.uiScale
            Repeater {
                model: [root.cover || root.bridge.state.artwork.cover || "", root.backgroundImage || root.bridge.state.artwork.background || ""]
                Rectangle {
                    required property string modelData
                    Layout.fillWidth: true; Layout.preferredHeight: Math.min(240 * root.uiScale, root.availableHeight * .4)
                    color: uiTheme.themeColors.base; radius: 8
                    Image { anchors.fill: parent; anchors.margins: 6; source: parent.modelData; fillMode: Image.PreserveAspectFit; asynchronous: true }
                }
            }
        }
        RowLayout {
            visible: root.page === 1
            Layout.fillWidth: true
            FluidButton { id: coverButton; objectName: "manualCardCover"; Layout.fillWidth: true; text: qsTranslate("NGS", "Choose cover", "Выбрать обложку"); enabled: !root.bridge.state.busy; highlighted: root.selectedField === 2; onClicked: { root.imageKind = "cover"; files.open() } }
            FluidButton { id: backgroundButton; objectName: "manualCardBackground"; Layout.fillWidth: true; text: qsTranslate("NGS", "Choose background", "Выбрать фон"); enabled: !root.bridge.state.busy; highlighted: root.selectedField === 3; onClicked: { root.imageKind = "background"; files.open() } }
        }
        Label { Layout.fillWidth: true; text: root.bridge.state.status; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; textFormat: Text.PlainText }
        Item { visible: root.page === 1; Layout.fillHeight: true }
        RowLayout {
            Layout.fillWidth: true
            FluidButton { id: nextButton; objectName: "manualCardNext"; visible: root.page === 0; text: qsTranslate("NGS", "Next · images"); highlighted: root.selectedField === 6; onClicked: { titleField.focus = false; descriptionField.focus = false; root.keyboard.hide(); root.page = 1; root.selectedField = 2 } }
            FluidButton { id: saveButton; objectName: "manualCardSave"; visible: root.page === 1; text: qsTranslate("NGS", "Save artwork"); enabled: !root.bridge.state.busy; highlighted: root.selectedField === 4; onClicked: root.bridge.saveCustomCard(titleField.text, descriptionField.text, root.cover, root.backgroundImage) }
            FluidButton { id: backButton; text: qsTranslate("NGS", "Back · ") + root.hints.back; enabled: !root.bridge.state.busy; highlighted: root.selectedField === 5; onClicked: root.back() }
        }
    }
}
