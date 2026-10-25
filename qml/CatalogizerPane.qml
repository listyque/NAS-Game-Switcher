import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts

ToolPage {
    id: root
    objectName: "catalogizerPane"
    required property QtObject bridge
    required property QtObject keyboard
    required property var hints
    property real uiScale: 1
    font.pixelSize: 16 * uiScale
    readonly property var state: bridge.state
    readonly property var displayRows: ["folders", "batch", "done"].indexOf(state.stage) >= 0 && state.hide_published
        ? state.rows.filter(row => !row.published) : state.rows
    readonly property bool showingRows: ["folders", "exes", "matches", "batch"].indexOf(state.stage) >= 0 || (state.stage === "done" && displayRows.length > 0)
    property int currentIndex: 0
    readonly property int workflowStep: state.stage === "folders" ? 0 : state.stage === "exes" ? 1 : state.stage === "matches" ? 2 : state.stage === "review" ? 3 : 4
    property int actionIndex: -1
    readonly property bool editing: manualEditor.visible
    property string previousStage: ""
    property int previousCount: 0
    onBridgeChanged: { previousStage = ""; previousCount = 0 }
    readonly property int firstAction: ["working", "batch"].indexOf(state.stage) >= 0 ? 1 : 0
    readonly property var actions: state.stage === "folders" ? [qsTranslate("NGS", "Rescan folders"), qsTranslate("NGS", "Previous"), qsTranslate("NGS", "Next"), bridge.installerMode ? qsTranslate("NGS", "Add all installer packages") : qsTranslate("NGS", "Add all games"), qsTranslate("NGS", "Close")]
        : state.stage === "exes" ? [qsTranslate("NGS", "Back to folders"), qsTranslate("NGS", "Close")]
        : state.stage === "matches" ? [qsTranslate("NGS", "Search", "Искать"), qsTranslate("NGS", "No artwork"), qsTranslate("NGS", "Back to folders"), qsTranslate("NGS", "Close")]
        : state.stage === "review" ? (state.manual ? [qsTranslate("NGS", "Confirm and publish"), qsTranslate("NGS", "Change artwork"), qsTranslate("NGS", "Back to folders"), qsTranslate("NGS", "Close")] : [qsTranslate("NGS", "Confirm and publish"), qsTranslate("NGS", "Back to folders"), qsTranslate("NGS", "Close")])
        : state.stage === "done" && state.result_total >= 0 ? [qsTranslate("NGS", "Previous"), qsTranslate("NGS", "Next"), qsTranslate("NGS", "Back to folders"), qsTranslate("NGS", "Close")]
        : ["working", "batch"].indexOf(state.stage) >= 0 ? [qsTranslate("NGS", "Refresh status"), qsTranslate("NGS", "Stop adding games"), qsTranslate("NGS", "Close")] : [qsTranslate("NGS", "Back to folders"), qsTranslate("NGS", "Close")]
    pageScale: uiScale
    leftPadding: state.stage === "review" ? Math.max(24, width * .045) : Math.max(width * .05, (width - 1080 * pageScale) / 2)
    rightPadding: leftPadding


    function choose(index) {
        if (state.busy) return
        if ((state.stage === "folders" || state.stage === "done") && displayRows[index]) bridge.reviewFolder(displayRows[index].folder)
        else if (state.stage !== "batch") bridge.choose(index)
    }
    function runAction(index) {
        if (actions[index] === qsTranslate("NGS", "Close")) { query.focus = false; close(); return }
        if (state.busy) return
        query.focus = false
        if (state.stage === "folders" && index === 3) bridge.batch()
        else if (state.stage === "folders") bridge.folders(index === 1 ? Math.max(0, state.offset - 20) : index === 2 ? (state.offset + 20 < state.total ? state.offset + 20 : state.offset) : state.offset)
        else if (state.stage === "matches" && index === 0) bridge.search(query.text.trim())
        else if (state.stage === "matches" && index === 1) bridge.preview("manual")
        else if (state.stage === "review" && index === 0) bridge.publish()
        else if (state.stage === "review" && state.manual && index === 1) manualEditor.openEditor()
        else if (["working", "batch"].indexOf(state.stage) >= 0 && index === 0) bridge.poll()
        else if (["working", "batch"].indexOf(state.stage) >= 0 && index === 1) bridge.cancel()
        else if (state.stage === "done" && state.result_total >= 0 && index < 2)
            bridge.results(index === 0 ? Math.max(0, state.result_offset - 20) : (state.result_offset + 20 < state.result_total ? state.result_offset + 20 : state.result_offset))
        else {
            bridge.backToFolders()
            currentIndex = Math.min(state.selected_index || 0, Math.max(0, displayRows.length - 1))
            rows.positionViewAtIndex(currentIndex, ListView.Contain)
        }
    }
    function handleAction(action) {
        if (manualEditor.visible && manualEditor.enabled) { manualEditor.handleAction(action); return }
        if (action === "back" || action === "menu") {
            query.focus = false
            if (!bridge.back()) close()
            rows.positionViewAtIndex(currentIndex, ListView.Contain)
            return
        }
        query.focus = false
        if (state.stage === "review" && (action === "up" || action === "down")) {
            const view = review.contentItem
            view.contentY = Math.max(0, Math.min(view.contentHeight - view.height,
                view.contentY + (action === "down" ? 60 : -60) * uiScale))
            return
        }
        if (action === "down") {
            if (actionIndex < 0 && currentIndex + 1 < displayRows.length) ++currentIndex
            else actionIndex = firstAction
        } else if (action === "up") {
            if (actionIndex >= 0 && displayRows.length && showingRows && state.stage !== "batch") actionIndex = -1
            else currentIndex = Math.max(0, currentIndex - 1)
        } else if (action === "left" || action === "right")
            actionIndex = actionIndex < 0 ? (action === "right" ? firstAction : actions.length - 1)
                : firstAction + (actionIndex - firstAction + (action === "right" ? 1 : actions.length - firstAction - 1)) % (actions.length - firstAction)
        else if (action === "activate") {
            if (actionIndex < 0) choose(currentIndex)
            else runAction(actionIndex)
        } else if (action === "refresh" && ["folders", "matches", "working", "batch"].indexOf(state.stage) >= 0) runAction(0)
        else if (action === "refresh" && state.stage === "done" && state.result_total >= 0) bridge.results(state.result_offset)
        else if (action === "toggle_installed" && state.stage === "matches") { query.forceActiveFocus(); keyboard.show() }
        else if (action === "toggle_installed" && ["folders", "batch", "done"].indexOf(state.stage) >= 0) bridge.setHidePublished(!state.hide_published)
        rows.positionViewAtIndex(currentIndex, ListView.Contain)
    }
    ManualCardEditor { id: manualEditor; parent: root.parent; bridge: root.bridge; keyboard: root.keyboard; hints: root.hints; uiScale: root.uiScale }
    onStateChanged: {
            if (!root.state.busy) {
                const count = root.state.rows.filter(row => !(["folders", "batch", "done"].indexOf(root.state.stage) >= 0 && root.state.hide_published) || !row.published).length
                const changedStage = root.previousStage !== root.state.stage
                if (changedStage) {
                    root.currentIndex = root.state.selected_index || 0
                    if (root.state.stage === "review") review.contentItem.contentY = 0
                }
                root.previousStage = root.state.stage
                root.currentIndex = Math.min(root.currentIndex, Math.max(0, count - 1))
                if (changedStage || (root.previousCount === 0 && count > 0))
                    root.actionIndex = ["review", "working", "batch"].indexOf(root.state.stage) >= 0 || !count ? root.firstAction : -1
                else if (root.actionIndex >= 0) root.actionIndex = Math.max(root.firstAction, Math.min(root.actionIndex, root.actions.length - 1))
                if (!count && root.actionIndex < 0) root.actionIndex = root.firstAction
                root.previousCount = count
                if (!query.activeFocus) query.text = root.state.query
            }
    }
    contentItem: ColumnLayout {
        opacity: manualEditor.visible ? 0 : 1
        spacing: 12 * root.uiScale
        Label { text: root.bridge.installerMode ? qsTranslate("NGS", "Catalog manager · installers") : qsTranslate("NGS", "Catalog manager · NGS server"); color: uiTheme.themeColors.text; font.pixelSize: (root.state.stage === "review" ? 18 : 32) * root.uiScale; font.weight: Font.Light;
            Layout.fillWidth: true; Layout.minimumWidth: 0; elide: Text.ElideRight }
        WorkflowSteps { objectName: "catalogizerSteps"; Layout.fillWidth: true; uiScale: root.uiScale;
            steps: [qsTranslate("NGS", "Folder"), qsTranslate("NGS", "Launch"), qsTranslate("NGS", "Artwork"), qsTranslate("NGS", "Review"), qsTranslate("NGS", "Publish")]; current: root.workflowStep }
        Label { text: root.state.folder + (root.state.executable ? " / " + root.state.executable : ""); color: uiTheme.themeColors.muted;
            visible: text.length > 0
            textFormat: Text.PlainText; Layout.fillWidth: true; Layout.minimumWidth: 0; elide: Text.ElideMiddle }
        CheckBox { objectName: "catalogizerHidePublished"; text: qsTranslate("NGS", "Hide added games · ") + root.hints.toggle;
            visible: ["folders", "batch", "done"].indexOf(root.state.stage) >= 0
            checked: root.state.hide_published; enabled: !root.state.busy; focusPolicy: Qt.NoFocus
            palette.windowText: uiTheme.themeColors.muted; onToggled: root.bridge.setHidePublished(checked) }
        TextField { id: query; objectName: "catalogizerQuery"; visible: root.state.stage === "matches";
            padding: 14; implicitHeight: 48
            background: FluidSurface { radius: 16; color: uiTheme.themeColors.base; border.width: 0 }
            Layout.fillWidth: true; Layout.minimumWidth: 0; maximumLength: 160; placeholderText: qsTranslate("NGS", "Title or game page URL · ") + root.hints.toggle + qsTranslate("NGS", " — edit", " — ввод");
            onAccepted: if (text.trim() && !root.state.busy) root.bridge.search(text.trim()) }
        ScrollView {
            id: review; objectName: "catalogizerReview"; visible: root.state.stage === "review"
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0; Layout.minimumHeight: 0
            clip: true; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            Column {
                width: review.availableWidth; spacing: 12 * root.uiScale
                GamePresentation {
                    width: parent.width; title: root.state.card.name || ""
                    cover: (root.state.artwork || {}).cover || ""; background: (root.state.artwork || {}).background || ""
                    coverObjectName: "catalogizerPreview-cover"; backgroundObjectName: "catalogizerPreview-background"
                    uiScale: root.uiScale; presenting: root.visible && root.state.stage === "review"
                }
                Label { width: parent.width; text: root.state.card.description || "";
                    wrapMode: Text.Wrap; color: uiTheme.themeColors.text; textFormat: Text.PlainText; font.pixelSize: 18 * root.uiScale; lineHeight: 1.45 }
            }
        }
        ListView {
            id: rows; objectName: "catalogizerRows"
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0; Layout.minimumHeight: 0
            visible: root.showingRows
            clip: true; spacing: 5; model: root.displayRows
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
                required property var modelData
                required property int index
                width: rows.width; height: 56 * root.uiScale; radius: 16
                color: root.currentIndex === index && root.actionIndex < 0 ? uiTheme.themeColors.soft : "transparent"
                Text { anchors.fill: parent; anchors.margins: 10; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight;
                    text: (modelData.name || modelData.path || "") + (modelData.status ? " · " + modelData.status : modelData.published ? qsTranslate("NGS", " · in catalog") : "") + (modelData.utility ? qsTranslate("NGS", " · utility?") : "");
                    textFormat: Text.PlainText; color: uiTheme.themeColors.text; font.pixelSize: 16 * root.uiScale }
                MouseArea { anchors.fill: parent; enabled: !root.state.busy && root.state.stage !== "batch"; onClicked: { root.currentIndex = index; root.choose(index) } }
            }
        }
        Item { Layout.fillHeight: true; visible: !root.showingRows && root.state.stage !== "review" }
        ProgressBar { Layout.fillWidth: true; visible: ["working", "batch"].indexOf(root.state.stage) >= 0; value: root.state.done / Math.max(1, root.state.total) }
        RowLayout {
            Layout.fillWidth: true
            ActivityIndicator { running: root.visible && root.state.busy; opacity: running ? 1 : 0;
                Layout.preferredWidth: 28 * root.uiScale; Layout.preferredHeight: 28 * root.uiScale }
            ScrollView {
                id: statusScroll; objectName: "catalogizerStatusScroll"
                Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.preferredHeight: 58 * root.uiScale
                clip: true; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                Label { objectName: "catalogizerStatus"; text: root.state.status; width: statusScroll.availableWidth;
                    wrapMode: Text.Wrap; color: uiTheme.themeColors.muted; textFormat: Text.PlainText }
            }
        }
        Label { visible: root.state.stage === "folders"; text: qsTranslate("NGS", "Add all automatically adds unambiguous matches and leaves the others for review. Files on the NAS are preserved.");
            Layout.fillWidth: true; Layout.minimumWidth: 0; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 13 * root.uiScale }
        Flow {
            objectName: "catalogizerActions"
            Layout.fillWidth: true; Layout.minimumWidth: 0; Layout.preferredHeight: implicitHeight; spacing: 8 * root.uiScale
            Repeater {
                model: root.actions
                FluidButton { required property string modelData; required property int index
                    objectName: "catalogizerAction-" + index
                    visible: index >= root.firstAction
                    font.pixelSize: 17 * root.uiScale; implicitHeight: Math.max(48, 52 * root.uiScale)
                    text: modelData === qsTranslate("NGS", "Previous") ? "‹" : modelData === qsTranslate("NGS", "Next") ? "›" : modelData
                    Accessible.name: modelData
                    highlighted: root.actionIndex === index; enabled: !root.state.busy || modelData === qsTranslate("NGS", "Close");
                    onClicked: root.runAction(index) }
            }
        }
    }
}
