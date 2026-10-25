import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "FocusNavigation.js" as Navigation

ToolPage {
    id: root
    objectName: "installDestination"
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    property real uiScale: 1
    property string gameId: ""
    property string gameName: ""
    property int selectedField: 0
    readonly property var preview: bridge.installPreview
    readonly property bool currentPreview: preview.game_id === gameId && preview.directory === folders.currentText
    readonly property bool canDownload: currentPreview && preview.can_install && !preview.busy
    pageScale: uiScale


    function choose(identity, name) {
        gameId = identity; gameName = name; selectedField = 0; folders.currentIndex = 0
        open()
        refresh()
    }
    function refresh() { if (visible && gameId) bridge.previewInstall(gameId, folders.currentText) }
    function download() { if (canDownload) root.bridge.queueDownload(gameId, folders.currentText) }
    function sizeLabel(value) {
        if (value === undefined) return "—"
        let unit = value >= 1073741824 ? qsTranslate("NGS", "GiB") : value >= 1048576 ? qsTranslate("NGS", "MiB") : value >= 1024 ? qsTranslate("NGS", "KiB") : qsTranslate("NGS", "B")
        let divisor = unit === qsTranslate("NGS", "GiB") ? 1073741824 : unit === qsTranslate("NGS", "MiB") ? 1048576 : unit === qsTranslate("NGS", "KiB") ? 1024 : 1
        return (value / divisor).toLocaleString(Qt.locale(), "f", divisor === 1 ? 0 : 2) + " " + unit
    }
    onAboutToHide: bridge.cancelInstallPreview()
    function handleAction(action) {
        if (action === "back" || action === "menu") close()
        else if (action === "refresh") refresh()
        else if (action === "up" || action === "down") {
            const order = canDownload ? [0, 1, 3] : [0, 3]
            selectedField = order[(Math.max(0, order.indexOf(selectedField)) + (action === "down" ? 1 : order.length - 1)) % order.length]
        }
        else if ((action === "left" || action === "right") && selectedField === 0)
            folders.currentIndex = (folders.currentIndex + (action === "right" ? 1 : folders.count - 1)) % Math.max(1, folders.count)
        else if (action === "left" || action === "right")
            selectedField = Navigation.next([folders, confirmButton, null, backButton], selectedField, action, root.contentItem)
        else if (action === "activate") {
            if (selectedField === 0) selectedField = 1
            else if (selectedField === 1) download()
            else if (selectedField === 2) refresh()
            else close()
        }
    }
    contentItem: ColumnLayout {
        spacing: 12 * root.uiScale
      ScrollView {
        id: installScroll; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
        contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
       Column {
        width: installScroll.availableWidth; spacing: 12 * root.uiScale
        Label { text: root.preview.kind === "installer" ? qsTranslate("NGS", "Where should we download the installer package?") : qsTranslate("NGS", "Where should we download the game?"); color: uiTheme.themeColors.text; font.pixelSize: 28 }
        Label { text: root.gameName; textFormat: Text.PlainText; width: parent.width; elide: Text.ElideRight; color: uiTheme.themeColors.muted; font.pixelSize: 20 }
        FluidComboBox {
            id: folders; objectName: "destinationFolders"
            width: parent.width; height: 48; model: root.bridge.installDirectories; focusPolicy: Qt.NoFocus
            onCurrentTextChanged: root.refresh()
            background: FluidSurface { radius: 8; border.width: 0; color: root.selectedField === 0 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
        }
        Label { text: folders.currentText ? root.bridge.directoryLabel(folders.currentText) : qsTranslate("NGS", "Add a folder in Settings");
            textFormat: Text.PlainText; width: parent.width; elide: Text.ElideMiddle; color: uiTheme.themeColors.muted }
        Rectangle {
            width: parent.width; height: spaceRows.implicitHeight + 28
            radius: 12; color: uiTheme.themeColors.button
            Column {
                id: spaceRows; x: 14; y: 14; width: parent.width - 28; spacing: 10 * root.uiScale
                Row {
                    width: parent.width
                    Label { width: parent.width * .62; text: qsTranslate("NGS", "Game size"); color: uiTheme.themeColors.muted; font.pixelSize: 18 * root.uiScale }
                    Label { objectName: "installGameSize"; width: parent.width * .38; horizontalAlignment: Text.AlignRight;
                        text: root.sizeLabel(root.currentPreview ? root.preview.size : undefined); color: uiTheme.themeColors.text; font.pixelSize: 18 * root.uiScale }
                }
                Row {
                    width: parent.width
                    Label { width: parent.width * .62; text: qsTranslate("NGS", "Currently free"); color: uiTheme.themeColors.muted; font.pixelSize: 18 * root.uiScale }
                    Label { objectName: "installFreeSpace"; width: parent.width * .38; horizontalAlignment: Text.AlignRight;
                        text: root.sizeLabel(root.currentPreview ? root.preview.free : undefined); color: uiTheme.themeColors.text; font.pixelSize: 18 * root.uiScale }
                }
                Row {
                    width: parent.width
                    Label { width: parent.width * .62; text: qsTranslate("NGS", "Reserved by the queue"); color: uiTheme.themeColors.muted; font.pixelSize: 18 * root.uiScale }
                    Label { objectName: "installReservedSpace"; width: parent.width * .38; horizontalAlignment: Text.AlignRight;
                        text: root.sizeLabel(root.currentPreview ? root.preview.reserved : undefined); color: uiTheme.themeColors.text; font.pixelSize: 18 * root.uiScale }
                }
                Row {
                    width: parent.width
                    Label { width: parent.width * .62; text: qsTranslate("NGS", "Free after the entire queue"); color: uiTheme.themeColors.text; font.pixelSize: 18 * root.uiScale }
                    Label { objectName: "installRemainingSpace"; width: parent.width * .38; horizontalAlignment: Text.AlignRight;
                        text: !root.currentPreview || root.preview.remaining === undefined ? "—"
                            : root.preview.remaining < 0 ? "−" + root.sizeLabel(-root.preview.remaining) : "≈ " + root.sizeLabel(root.preview.remaining);
                        color: root.canDownload ? uiTheme.themeColors.accent : uiTheme.themeColors.warning; font.pixelSize: 18 * root.uiScale; font.bold: true }
                }
            }
        }
        Label { objectName: "installSpaceStatus"; text: root.currentPreview ? root.preview.status : qsTranslate("NGS", "Checking storage…");
            textFormat: Text.PlainText; width: parent.width; height: Math.max(38 * root.uiScale, implicitHeight); wrapMode: Text.WordWrap;
            color: root.preview.busy || root.canDownload ? uiTheme.themeColors.muted : uiTheme.themeColors.warning }
        Label { text: qsTranslate("NGS", "Includes all downloads on this drive, including paused items. Temporary packages and a reserve are also required: ")
                + root.sizeLabel(root.currentPreview ? root.preview.reserve : undefined) + qsTranslate("NGS", ". Other applications may change the available space.");
            width: parent.width; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 13 * root.uiScale }
       }
      }
        Row {
            Layout.fillWidth: true; Layout.preferredHeight: 52 * root.uiScale
            spacing: 12
            FluidButton { id: confirmButton; objectName: "confirmDestination"; text: qsTranslate("NGS", "Download", "Загрузить"); enabled: root.canDownload; highlighted: root.selectedField === 1; onClicked: root.download() }
            FluidButton { id: backButton; text: qsTranslate("NGS", "Back · ") + root.hints.back; highlighted: root.selectedField === 3; onClicked: root.close() }
        }
    }
}
