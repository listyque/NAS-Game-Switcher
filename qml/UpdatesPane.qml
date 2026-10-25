import QtQuick
import QtQuick.Controls.Basic

ToolPage {
    id: root
    objectName: "updatesPane"
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    property real uiScale: 1
    property int selectedField: 0
    readonly property var state: bridge.updatesState
    readonly property bool primaryEnabled: !state.busy && (!state.available || state.can_install)
    pageScale: uiScale


    onAboutToShow: {
        selectedField = primaryEnabled ? 1 : 2
        updatesScroll.contentItem.contentY = 0
        if (!state.busy) bridge.checkUpdates()
    }
    function back() { if (state.busy) bridge.cancelUpdate(); close() }
    function primaryAction() {
        if (!primaryEnabled) return
        if (state.available) bridge.installUpdate()
        else bridge.checkUpdates()
    }
    function handleAction(action) {
        if (action === "back" || action === "menu") back()
        else if ((action === "up" || action === "down") && updatesScroll.contentItem.contentHeight > updatesScroll.availableHeight) {
            const flick = updatesScroll.contentItem
            flick.cancelFlick()
            flick.contentY = Math.max(0, Math.min(flick.contentHeight - updatesScroll.availableHeight,
                flick.contentY + (action === "down" ? 1 : -1) * updatesScroll.availableHeight * .7))
        }
        else if (["left", "right", "up", "down"].indexOf(action) >= 0) {
            selectedField = primaryEnabled && selectedField === 2 ? 1 : 2
        }
        else if (action === "refresh") bridge.checkUpdates()
        else if (action === "activate") {
            if (selectedField === 1) primaryAction()
            else if (selectedField === 2) back()
        }
    }
    contentItem: Column {
        spacing: 16 * root.uiScale
        Label { id: heading; text: qsTranslate("NGS", "NGS updates"); color: uiTheme.themeColors.text; font.pixelSize: 32 * root.uiScale; font.weight: Font.Light }
        ScrollView {
            id: updatesScroll; objectName: "updatesScroll"
            width: parent.width; height: parent.height - heading.height - actions.height - 32 * root.uiScale
            contentWidth: availableWidth; contentHeight: form.implicitHeight; clip: true
        Column {
            id: form; width: updatesScroll.availableWidth; spacing: 12 * root.uiScale
        FluidSurface {
            objectName: "updateAnnouncement"; visible: root.state.available
            width: parent.width; height: announcement.implicitHeight + 32
            color: uiTheme.themeColors.soft; radius: 16; border.width: 0; shadowEnabled: false; glass: false
            Column {
                id: announcement; x: 16; y: 16; width: parent.width - 32; spacing: 8
                Label { width: parent.width; text: qsTranslate("NGS", "NGS update available"); wrapMode: Text.WordWrap; color: uiTheme.themeColors.text; font.pixelSize: 24 * root.uiScale }
                Label { objectName: "updateAvailableVersion"; width: parent.width; text: qsTranslate("NGS", "Version ") + root.state.version;
                    color: uiTheme.themeColors.text; font.pixelSize: 20 * root.uiScale; textFormat: Text.PlainText }
            }
        }
        Label { objectName: "updateCurrentVersion"; text: qsTranslate("NGS", "Installed: ") + (root.state.current === "0.0.0.0" ? qsTranslate("NGS", "source build") : root.state.current);
            color: uiTheme.themeColors.muted; font.pixelSize: 17 * root.uiScale }
        Label { objectName: "updateHttpServer"; text: qsTranslate("NGS", "Update server: ") + root.state.server;
            width: parent.width; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText; color: uiTheme.themeColors.muted }
        Label { objectName: "updateNotes"; width: parent.width; text: root.state.notes;
            textFormat: Text.PlainText; wrapMode: Text.Wrap;
            color: uiTheme.themeColors.text; font.pixelSize: 16 * root.uiScale }
        Label { objectName: "updateStatus"; text: root.state.status; textFormat: Text.PlainText; width: parent.width;
            wrapMode: Text.WordWrap; color: uiTheme.themeColors.text; height: Math.max(58 * root.uiScale, implicitHeight) }
        ProgressBar { objectName: "updateProgress"; width: parent.width; value: root.state.progress; visible: root.state.busy }
        Label { text: qsTranslate("NGS", "NGS will restart after installation. Settings and games are preserved, and downloads resume after startup.");
            width: parent.width; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 14 * root.uiScale }
        }
        }
        Row {
            id: actions
            spacing: 12 * root.uiScale
            FluidButton {
                id: primaryButton; objectName: "installUpdateButton"
                text: (root.state.available ? qsTranslate("NGS", "Download and update") : qsTranslate("NGS", "Check for updates")) + " · " + root.hints.activate
                enabled: root.primaryEnabled; implicitHeight: Math.max(48, 52 * root.uiScale)
                Accessible.name: root.state.available ? qsTranslate("NGS", "Download and update") : qsTranslate("NGS", "Check for updates")
                checked: root.state.available; font.weight: root.state.available ? Font.DemiBold : Font.Normal
                highlighted: root.selectedField === 1; onClicked: root.primaryAction()
                contentItem: Row {
                    spacing: 8
                    Canvas {
                        objectName: "updateActionIcon"; width: 24; height: 24; anchors.verticalCenter: parent.verticalCenter
                        property bool download: root.state.available
                        property color ink: uiTheme.themeColors.text
                        onDownloadChanged: requestPaint()
                        onInkChanged: requestPaint()
                        onPaint: {
                            const c = getContext("2d"); c.reset(); c.strokeStyle = ink; c.lineWidth = 1.8; c.lineCap = "round"; c.lineJoin = "round"; c.beginPath()
                            if (download) {
                                c.moveTo(12, 3); c.lineTo(12, 15); c.moveTo(7, 10); c.lineTo(12, 15); c.lineTo(17, 10)
                                c.moveTo(4, 16); c.lineTo(4, 21); c.lineTo(20, 21); c.lineTo(20, 16)
                            } else {
                                c.arc(12, 12, 8, -.7, 4.8); c.moveTo(20, 3); c.lineTo(20, 9); c.lineTo(14, 9)
                            }
                            c.stroke()
                        }
                    }
                    Text { text: primaryButton.text; font: primaryButton.font; color: uiTheme.themeColors.text; anchors.verticalCenter: parent.verticalCenter }
                }
            }
            FluidButton {
                id: backButton; objectName: "updatesBackButton"
                text: root.state.busy ? qsTranslate("NGS", "Cancel · ") + root.hints.back : qsTranslate("NGS", "Back · ") + root.hints.back
                implicitHeight: Math.max(48, 52 * root.uiScale)
                highlighted: root.selectedField === 2; onClicked: root.back()
                contentItem: Row {
                    spacing: 8
                    Item { width: 24; height: 24; anchors.verticalCenter: parent.verticalCenter
                        KeyboardGlyph { objectName: "updatesBackIcon"; keyName: root.state.busy ? "@stop" : "@back"; ink: uiTheme.themeColors.text; width: 24; height: 24 }
                    }
                    Text { text: backButton.text; font: backButton.font; color: uiTheme.themeColors.text; anchors.verticalCenter: parent.verticalCenter }
                }
            }
        }
    }
}
