import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ToolPage {
    id: root
    objectName: "localScanner"
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    property real uiScale: 1
    readonly property var state: bridge.scannerState
    property int currentIndex: 0
    property int matchIndex: 0
    property int actionIndex: 1
    property int focusZone: 1
    property int pathAction: 1
    property string previousStage: ""
    property bool showAllResults: false
    readonly property bool batch: state.stage === "batch"
    readonly property bool stores: state.stage === "stores"
    readonly property var rows: stores ? (showAllResults ? state.store_games : state.store_games.filter(row => row.state !== "added" && row.state !== "already")) : batch ? (showAllResults ? state.batch_rows
        : state.batch_rows.filter(row => row.state !== "added" && row.state !== "already")) : state.stage === "browse"
        ? (state.path ? [{name: qsTranslate("NGS", "..  Up one level"), path: state.parent}] : []).concat(state.folders) : state.candidates
    readonly property bool confirming: state.stage === "confirm"
    pageScale: uiScale


    function openScanner(installerId = "") {
        if (bridge.operationBusy) return
        if (!bridge.selectInstallerForImport(installerId)) return
        open()
        if (stores) bridge.showStoreGames(); else bridge.browseLocalFolder(state.path)
    }
    function chooseRow(index) {
        if (bridge.operationBusy || index < 0 || index >= rows.length) return
        currentIndex = index
        if (stores) bridge.reviewStoreGame(rows[index].path)
        else if (batch) bridge.reviewLocalGame(rows[index].path)
        else if (state.stage === "browse") bridge.browseLocalFolder(rows[index].path)
        else bridge.selectLocalExecutable(rows[index].path)
    }
    function back() {
        if (bridge.operationBusy && !bridge.downloadActive) { bridge.cancelOperation(); return }
        if (state.store_review) { if (confirming) bridge.reviewStoreGame(state.store_review); else bridge.showStoreGames() }
        else if (stores) bridge.browseLocalFolder("")
        else if (state.batch_review) bridge.showCollectionResults()
        else if (state.stage === "confirm" || state.stage === "exes" || batch) bridge.browseLocalFolder(state.path)
        else close()
    }
    function cycleMatch() {
        matchIndex = (matchIndex + 1) % (state.matches.length + 1)
        gameName.text = matchIndex ? state.matches[matchIndex - 1].name : state.name
    }
    function submit() {
        gameName.focus = false
        bridge.confirmLocalGame(gameName.text, matchIndex ? state.matches[matchIndex - 1].id : "")
    }
    function handleAction(action) {
        if (action === "back" || action === "menu") { back(); return }
        if (bridge.operationBusy) return
        if (confirming) {
            if (action === "toggle_installed") cycleMatch()
            else if (action === "up" || action === "down") {
                gameName.focus = false
                focusZone = Math.max(0, Math.min(2, focusZone + (action === "down" ? 1 : -1)))
            } else if (action === "left" || action === "right") {
                if (focusZone === 1) cycleMatch()
                else actionIndex = 1 - actionIndex
            } else if (action === "activate") {
                if (focusZone === 0) { gameName.forceActiveFocus(); bridge.keyboard.show() }
                else if (focusZone === 1) cycleMatch()
                else if (actionIndex) submit(); else back()
            }
        } else {
            if (focusZone === -1 && (action === "activate" || action === "refresh")) { bridge.scanStoreGames(); return }
            if (action === "up" || action === "down") {
                folderPath.focus = false
                if (focusZone === 1 && action === "up" && currentIndex > 0) --currentIndex
                else if (focusZone === 1 && action === "down" && currentIndex + 1 < rows.length) ++currentIndex
                else { focusZone = Math.max(-1, Math.min(2, focusZone + (action === "down" ? 1 : -1))); if (stores && focusZone === 0) focusZone = action === "up" ? -1 : 1 }
                entries.positionViewAtIndex(currentIndex, ListView.Contain)
            } else if (action === "left" || action === "right") {
                if (focusZone === 0) pathAction = 1 - pathAction
                else if (focusZone === 2) actionIndex = (actionIndex + (action === "right" ? 1 : 3)) % 4
                else if (focusZone === 1 && action === "right") chooseRow(currentIndex)
                else if (focusZone === 1 && state.stage === "browse" && state.path) chooseRow(0)
            } else if (action === "activate") {
                if (focusZone === 0) {
                    if (pathAction) bridge.browseLocalFolder(folderPath.text)
                    else { folderPath.forceActiveFocus(); bridge.keyboard.show() }
                } else if (focusZone === 2) {
                    if (actionIndex === 0) back()
                    else if (actionIndex === 1) {
                        if (stores) bridge.importStoreGames()
                        else if (batch) bridge.scanLocalCollection(state.path)
                        else bridge.scanLocalFolder(state.path)
                    }
                    else if (actionIndex === 2) bridge.browseLocalFolder("")
                    else if (batch || stores) showAllResults = !showAllResults
                    else bridge.scanLocalCollection(state.path)
                } else chooseRow(currentIndex)
            }
            else if (action === "toggle_installed") bridge.browseLocalFolder("")
            else if (action === "refresh" && (stores || state.path)) {
                if (stores) bridge.scanStoreGames()
                else if (batch) bridge.scanLocalCollection(state.path)
                else bridge.scanLocalFolder(state.path)
            }
        }
    }
    Connections {
        target: root.bridge
        function onScannerChanged() {
            if (root.previousStage !== root.state.stage) root.currentIndex = 0
            root.currentIndex = Math.min(root.currentIndex, Math.max(0, root.rows.length - 1))
            if (root.confirming && root.previousStage !== "confirm") {
                root.matchIndex = root.state.matches.length === 1 ? 1 : 0
                gameName.text = root.matchIndex ? root.state.matches[0].name : root.state.name
                root.actionIndex = 1
                root.focusZone = 2
            }
            if (!root.confirming && root.previousStage !== root.state.stage) root.focusZone = 1
            root.previousStage = root.state.stage
        }
        function onLocalGameAdded(identity) { root.close() }
        function onMetadataApplied() {
            if (root.visible && root.confirming && root.state.matches.length === 1) {
                root.matchIndex = 1
                gameName.text = root.state.matches[0].name
            }
        }
    }

    component ScannerButton: FluidButton {
        id: button
        property bool chosen: false
        font.pixelSize: 16 * root.uiScale
        height: Math.max(48, 54 * root.uiScale)
        focusPolicy: Qt.NoFocus
        background: FluidSurface { radius: 16; border.width: 0; color: button.chosen ? uiTheme.themeColors.soft : uiTheme.themeColors.button; shadowEnabled: false }
        contentItem: Text { text: button.text; font: button.font; color: uiTheme.themeColors.text; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignHCenter }
    }

    contentItem: ColumnLayout {
        spacing: 10 * root.uiScale
        Text { Layout.fillWidth: true; elide: Text.ElideRight; text: qsTranslate("NGS", "Add games from this device"); color: uiTheme.themeColors.text; font.pixelSize: 32 * root.uiScale; font.weight: Font.Light }
        WorkflowSteps { Layout.fillWidth: true; uiScale: root.uiScale; steps: [qsTranslate("NGS", "Source"), qsTranslate("NGS", "Choose game"), qsTranslate("NGS", "Confirmation")];
            current: root.confirming ? 2 : root.state.stage === "browse" ? 0 : 1 }
        ScannerButton {
            objectName: "scannerStoresButton"; visible: !root.confirming
            Layout.fillWidth: true; Layout.preferredHeight: Math.max(48, 48 * root.uiScale)
            text: qsTranslate("NGS", "Find Steam / GOG / Epic / Battle.net games")
            enabled: !root.bridge.operationBusy; chosen: root.focusZone === -1
            onClicked: root.bridge.scanStoreGames()
        }
        Row {
            Layout.fillWidth: true; Layout.preferredHeight: 38 * root.uiScale; spacing: 10 * root.uiScale
            ActivityIndicator {
                id: scanActivity
                visible: root.bridge.operationBusy && !root.bridge.downloadActive
                width: visible ? 32 * root.uiScale : 0; height: width
                running: root.visible && visible; palette.dark: uiTheme.themeColors.accent
            }
            Text {
                text: root.state.status; textFormat: Text.PlainText
                width: parent.width - (scanActivity.visible ? scanActivity.width + parent.spacing : 0)
                wrapMode: Text.Wrap; color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale; height: parent.height
            }
        }
        Row {
            visible: !root.confirming && !root.stores; Layout.fillWidth: true; Layout.preferredHeight: Math.max(48, 48 * root.uiScale); spacing: 10 * root.uiScale
            TextField {
                id: folderPath
                objectName: "scannerFolderPath"
                width: parent.width - openPath.width - parent.spacing; height: 48 * root.uiScale
                text: root.state.path; placeholderText: "D:/Games/My Game"
                selectByMouse: true; enabled: !root.bridge.operationBusy
                font.pixelSize: 16 * root.uiScale; color: uiTheme.themeColors.text
                background: FluidSurface { radius: 8; border.width: 0; color: folderPath.activeFocus || root.focusZone === 0 && root.pathAction === 0 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
                onAccepted: { focus = false; root.bridge.browseLocalFolder(text) }
            }
            ScannerButton {
                id: openPath; objectName: "scannerOpenPath"; text: qsTranslate("NGS", "Open path"); width: 170 * root.uiScale
                enabled: !root.bridge.operationBusy
                chosen: root.focusZone === 0 && root.pathAction === 1
                onClicked: { folderPath.focus = false; root.bridge.browseLocalFolder(folderPath.text) }
            }
        }
        ListView {
            id: entries
            objectName: "scannerEntries"
            visible: !root.confirming
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 54 * root.uiScale
            clip: true; spacing: 5 * root.uiScale
            model: root.rows
            ScrollBar.vertical: ScrollBar {}
            delegate: FluidButton {
                id: entryButton
                required property var modelData
                required property int index
                width: entries.width - 16 * root.uiScale; height: Math.max(48, 54 * root.uiScale)
                focusPolicy: Qt.NoFocus; enabled: !root.bridge.operationBusy
                Accessible.name: label.text
                background: FluidSurface {
                    radius: 16; color: root.focusZone === 1 && root.currentIndex === entryButton.index ? uiTheme.themeColors.soft : "transparent"
                    border.width: 0; shadowEnabled: false
                    Behavior on color { ColorAnimation { duration: 130 } }
                }
                contentItem: Text {
                    id: label
                    text: root.batch || root.stores ? entryButton.modelData.name + "  ·  " + entryButton.modelData.status
                        : root.state.stage === "browse" ? "▸  " + entryButton.modelData.name
                        : (entryButton.modelData.utility ? qsTranslate("NGS", "Utility?  ·  ") : "EXE  ·  ") + entryButton.modelData.path
                    textFormat: Text.PlainText; elide: Text.ElideMiddle; verticalAlignment: Text.AlignVCenter
                    color: entryButton.modelData.utility ? uiTheme.themeColors.warning : uiTheme.themeColors.text; font.pixelSize: 16 * root.uiScale
                }
                onClicked: root.chooseRow(index)
            }
            Text {
                anchors.centerIn: parent; visible: !entries.count && !root.bridge.operationBusy
                text: root.stores ? qsTranslate("NGS", "No new installed games found. Use the button below to show games already added.")
                    : root.batch ? qsTranslate("NGS", "No games need manual review. Added games are in My games.") : root.state.stage === "browse" ? qsTranslate("NGS", "No accessible subfolders") : qsTranslate("NGS", "No executables found")
                width: parent.width - 30; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter
                color: uiTheme.themeColors.muted; font.pixelSize: 17 * root.uiScale
            }
        }
        ScrollView {
            id: confirmationScroll
            visible: root.confirming; Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 60
            clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
          Column {
            width: confirmationScroll.availableWidth; spacing: 17 * root.uiScale
            Text { text: qsTranslate("NGS", "Title", "Название"); color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale }
            TextField {
                id: gameName; objectName: "scannerGameName"
                width: parent.width; height: Math.max(48, 54 * root.uiScale)
                font.pixelSize: 22 * root.uiScale; color: uiTheme.themeColors.text; maximumLength: 160; selectByMouse: true
                enabled: !root.bridge.operationBusy
                background: FluidSurface { radius: 8; border.width: 0; color: gameName.activeFocus || root.focusZone === 0 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
                onAccepted: focus = false
            }
            Text { text: root.state.path + "\n\nEXE: " + root.state.exe; textFormat: Text.PlainText; width: parent.width; wrapMode: Text.WrapAnywhere; color: uiTheme.themeColors.muted; font.pixelSize: 17 * root.uiScale }
            ScannerButton {
                objectName: "scannerMetadata"
                width: parent.width
                text: root.matchIndex > 0 && root.matchIndex <= root.state.matches.length
                                     ? qsTranslate("NGS", "Artwork: ") + root.state.matches[root.matchIndex - 1].name + "  ·  " + root.hints.toggle
                                     : qsTranslate("NGS", "Not matched to the catalog  ·  ") + root.hints.toggle
                enabled: !root.bridge.operationBusy && root.state.matches.length > 0
                chosen: root.focusZone === 1
                onClicked: root.cycleMatch()
            }
            Text { text: qsTranslate("NGS", "The executable's folder is used as the working directory. Game files will not be copied or deleted."); width: parent.width; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale }
          }
        }
        Row {
            Layout.fillWidth: true; Layout.preferredHeight: Math.max(48, 54 * root.uiScale); spacing: 12 * root.uiScale
            ScannerButton {
                objectName: "scannerBack"
                text: root.bridge.operationBusy ? qsTranslate("NGS", "Cancel", "Отменить") : qsTranslate("NGS", "Back · ") + root.hints.back
                width: (parent.width - parent.spacing * (root.confirming ? 1 : 3)) / (root.confirming ? 2 : 4)
                chosen: root.focusZone === 2 && root.actionIndex === 0
                onClicked: root.back()
            }
            ScannerButton {
                objectName: "scannerConfirm"
                width: (parent.width - parent.spacing * (root.confirming ? 1 : 3)) / (root.confirming ? 2 : 4)
                text: root.confirming ? qsTranslate("NGS", "Add to My games") : root.stores ? qsTranslate("NGS", "Add all") : root.batch ? qsTranslate("NGS", "Search again") : qsTranslate("NGS", "Find executables · ") + root.hints.refresh
                chosen: root.focusZone === 2 && root.actionIndex === 1
                enabled: !root.bridge.operationBusy && (root.confirming ? gameName.text.trim().length > 0 : root.stores ? root.rows.length > 0 : root.state.path.length > 0)
                onClicked: { if (root.confirming) root.submit(); else if (root.stores) root.bridge.importStoreGames(); else if (root.batch) root.bridge.scanLocalCollection(root.state.path); else root.bridge.scanLocalFolder(root.state.path) }
            }
            ScannerButton {
                text: qsTranslate("NGS", "Drives · ") + root.hints.toggle; width: (parent.width - 3 * parent.spacing) / 4; visible: !root.confirming; enabled: !root.bridge.operationBusy
                chosen: root.focusZone === 2 && root.actionIndex === 2
                onClicked: root.bridge.browseLocalFolder("")
            }
            ScannerButton {
                objectName: "scannerBulkButton"
                text: root.batch || root.stores ? (root.showAllResults ? qsTranslate("NGS", "Hide added games") : qsTranslate("NGS", "Show all")) : qsTranslate("NGS", "Find all games")
                width: (parent.width - 3 * parent.spacing) / 4
                visible: !root.confirming; enabled: !root.bridge.operationBusy && (root.stores || root.state.path.length > 0)
                chosen: root.focusZone === 2 && root.actionIndex === 3
                onClicked: { if (root.batch || root.stores) root.showAllResults = !root.showAllResults; else root.bridge.scanLocalCollection(root.state.path) }
            }
        }
    }
}
