import QtQuick
import QtQuick.Controls.Basic

Item {
    id: root
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    property int currentIndex: 0
    property int actionIndex: 0
    function revealSelected() {
        if (!visible || !jobs.length) return
        list.forceLayout()
        list.positionViewAtIndex(currentIndex, ListView.Contain)
    }
    onCurrentIndexChanged: Qt.callLater(revealSelected)
    onHeightChanged: if (visible) Qt.callLater(revealSelected)
    onWidthChanged: if (visible) Qt.callLater(revealSelected)
    readonly property var jobs: bridge.downloadJobs
    readonly property var selectedJob: jobs[currentIndex] || null
    readonly property var statistics: bridge.downloadStatistics
    readonly property color networkColor: uiTheme.themeColors.light ? "#355f92" : "#8bbcff"
    readonly property color diskColor: uiTheme.themeColors.light ? "#785821" : "#ffd395"
    readonly property bool compact: height < 540
    function sizeLabel(value) { return value >= 0 ? (value / 1048576).toLocaleString(Qt.locale(), "f", 1) + qsTranslate("NGS", " MiB") : "—" }
    signal focused()
    property bool jobsDirty: true
    ListModel { id: queueModel; objectName: "downloadQueueModel" }
    function syncJobs() {
        if (!visible) { jobsDirty = true; return }
        for (let i = 0; i < jobs.length; i++) {
            const job = jobs[i]
            const value = {identity: job.id, title: job.name, status: job.state,
                           detail: job.message, completion: job.progress, discard: !!job.discard,
                           destination: job.destination, installer: job.card.kind === "installer",
                           completedBytes: job.completed_bytes !== undefined ? job.completed_bytes : job.progress > 0 ? -1 : 0,
                           totalBytes: job.total_bytes !== undefined ? job.total_bytes : job.card.total_size !== undefined ? job.card.total_size : -1,
                           networkBytes: job.network_bytes !== undefined ? job.network_bytes : job.progress > 0 ? -1 : 0, networkTotal: job.network_total !== undefined ? job.network_total : -1}
            let position = i
            while (position < queueModel.count && queueModel.get(position).identity !== job.id) position++
            if (position === queueModel.count) queueModel.insert(i, value)
            else {
                if (position !== i) queueModel.move(position, i, 1)
                queueModel.set(i, value)
            }
        }
        if (queueModel.count > jobs.length) queueModel.remove(jobs.length, queueModel.count - jobs.length)
        jobsDirty = false
    }
    onJobsChanged: syncJobs()
    onVisibleChanged: if (visible && jobsDirty) syncJobs()
    Component.onCompleted: syncJobs()
    component QueueButton: FluidButton {
        id: button
        focusPolicy: Qt.NoFocus; font.pixelSize: 16
        scale: down ? .97 : hovered ? 1.02 : 1
        Behavior on scale { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
        background: FluidSurface { radius: 16; color: button.highlighted ? uiTheme.themeColors.soft : uiTheme.themeColors.button; border.width: 0; shadowEnabled: false }
        contentItem: Text { text: button.text; font: button.font; color: button.enabled ? uiTheme.themeColors.text : uiTheme.themeColors.muted; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
    }
    function primary() {
        if (!selectedJob) return
        if (["paused", "error"].indexOf(selectedJob.state) >= 0) bridge.resumeDownload(selectedJob.id)
        else if (["queued", "running"].indexOf(selectedJob.state) >= 0) bridge.pauseDownload(selectedJob.id)
    }
    function handleAction(action) {
        if (!jobs.length) return false
        if (action === "up" && currentIndex === 0) return false
        if (action === "up" || action === "down") {
            currentIndex = Math.max(0, Math.min(jobs.length - 1, currentIndex + (action === "down" ? 1 : -1)))
            Qt.callLater(revealSelected)
        } else if (action === "left" || action === "right") actionIndex = 1 - actionIndex
        else if (action === "activate") { if (actionIndex) bridge.cancelDownload(selectedJob.id); else primary() }
        else if (action === "toggle_installed") bridge.cancelDownload(selectedJob.id)
        else return false
        return true
    }
    Connections {
        target: root.bridge
        function onDownloadsChanged() { root.currentIndex = Math.max(0, Math.min(root.currentIndex, root.jobs.length - 1)) }
    }
    Text { id: heading; text: qsTranslate("NGS", "Downloads"); color: uiTheme.themeColors.text; font.pixelSize: 36; font.weight: Font.Light }
    Text {
        y: root.compact ? 52 : 80; width: parent.width; height: root.compact ? 36 : 54
        text: root.jobs.length ? qsTranslate("NGS", "One game at a time · ") + root.hints.activate + qsTranslate("NGS", " — pause / resume · ") + root.hints.toggle + qsTranslate("NGS", " — cancel and remove partial files")
                              : root.bridge.operationStatus
        textFormat: Text.PlainText; wrapMode: Text.Wrap; color: uiTheme.themeColors.muted; font.pixelSize: root.compact ? 14 : 17
    }
    Rectangle {
        y: root.compact ? 96 : 130; width: parent.width - 20; height: root.compact ? 86 : 102; radius: 12
        visible: root.jobs.length > 0; color: "transparent"; border.width: 0
        Text { x: 18; y: 12; text: qsTranslate("NGS", "Network   ") + (root.statistics.network / 1048576).toFixed(1) + qsTranslate("NGS", " MiB/s"); color: root.networkColor; font.pixelSize: 18 }
        Text { x: 18; y: root.compact ? 38 : 47; text: qsTranslate("NGS", "Disk   ") + (root.statistics.disk / 1048576).toFixed(1) + qsTranslate("NGS", " MiB/s"); color: root.diskColor; font.pixelSize: 18 }
        Text { x: 18; y: root.compact ? 65 : 78; text: qsTranslate("NGS", "Writing extracted files"); color: uiTheme.themeColors.muted; font.pixelSize: 12 }
        Canvas {
            id: speedGraph; objectName: "downloadSpeedGraph"
            readonly property var colors: uiTheme.themeColors
            x: 310; y: 12; width: parent.width - x - 18; height: parent.height - 24
            onWidthChanged: if (visible) requestPaint()
            onHeightChanged: if (visible) requestPaint()
            onColorsChanged: if (visible) requestPaint()
            onVisibleChanged: if (visible) requestPaint()
            onPaint: {
                const ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                const history = root.statistics.history
                let peak = 1048576
                for (const sample of history) peak = Math.max(peak, sample[0], sample[1])
                ctx.strokeStyle = uiTheme.themeColors.hover; ctx.lineWidth = 1
                for (let line = 0; line <= 2; line++) {
                    ctx.beginPath(); ctx.moveTo(0, 2 + line * (height - 4) / 2)
                    ctx.lineTo(width, 2 + line * (height - 4) / 2); ctx.stroke()
                }
                for (let series = 0; series < 2; series++) {
                    ctx.strokeStyle = series === 0 ? root.networkColor : root.diskColor; ctx.lineWidth = 2
                    ctx.beginPath()
                    for (let i = 0; i < history.length; i++) {
                        const x = width * (60 - history.length + i) / 59
                        const y = height - 2 - history[i][series] / peak * (height - 4)
                        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                    }
                    ctx.stroke()
                }
            }
            Connections { target: root.bridge; enabled: root.visible && speedGraph.visible; function onDownloadStatisticsChanged() { speedGraph.requestPaint() } }
        }
    }
    ListView {
        id: list; objectName: "downloadQueueList"
        x: 0; y: root.compact ? 196 : 250; width: parent.width; height: parent.height - y
        visible: root.jobs.length > 0; model: queueModel; clip: true; spacing: 14
        add: Transition { ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 180 }
            NumberAnimation { property: "scale"; from: .97; to: 1; duration: 360; easing.type: Easing.OutBack; easing.overshoot: .6 }
        } }
        remove: Transition { ParallelAnimation {
            NumberAnimation { property: "opacity"; to: 0; duration: 140 }
            NumberAnimation { property: "scale"; to: .97; duration: 140; easing.type: Easing.InQuad }
        } }
        displaced: Transition { NumberAnimation { properties: "y"; duration: 340; easing.type: Easing.OutCubic } }
        ScrollBar.vertical: ScrollBar {}
        delegate: FluidSurface {
            id: row
            required property string identity
            required property string title
            required property string status
            required property string detail
            required property real completion
            required property bool discard
            required property string destination
            required property bool installer
            required property real completedBytes
            required property real totalBytes
            required property real networkBytes
            required property real networkTotal
            required property int index
            readonly property bool expanded: root.currentIndex === index
            onHeightChanged: if (expanded && root.visible) Qt.callLater(root.revealSelected)
            readonly property real fraction: root.bridge.downloadActive && root.bridge.operationId === identity
                                             ? root.bridge.operationProgress : completion
            x: 0; width: list.width - 16; height: expanded ? queueDetail.y + 68 : 112; radius: 20
            shadowEnabled: false; glass: false; border.width: 0
            color: expanded ? uiTheme.themeColors.button : "transparent"
            Behavior on color { ColorAnimation { duration: 180 } }
            TapHandler { onTapped: { root.currentIndex = row.index; root.focused() } }
            Text { objectName: "queueName-" + row.identity; x: 20; y: 15; width: parent.width - 40; text: (row.index + 1) + ".  " + row.title; textFormat: Text.PlainText; elide: Text.ElideRight; color: uiTheme.themeColors.text; font.pixelSize: 22 }
            Text {
                x: 20; y: 48; width: parent.width - 40
                text: ({queued: qsTranslate("NGS", "Queued"), running: qsTranslate("NGS", "Downloading"), pausing: qsTranslate("NGS", "Stopping…"), paused: qsTranslate("NGS", "Paused"), error: qsTranslate("NGS", "Error · retry available"), cancelling: qsTranslate("NGS", "Canceling…"), stopping: qsTranslate("NGS", "Saving before exit…")})[row.status]
                      + " · " + Math.floor(row.fraction * 100) + "%"
                textFormat: Text.PlainText; color: row.status === "error" ? uiTheme.themeColors.warning : uiTheme.themeColors.muted; font.pixelSize: 15
            }
            Text {
                id: destinationLabel; objectName: "queueDestination-" + row.identity
                visible: row.expanded
                x: 20; y: 73; width: parent.width - 40
                text: (row.installer ? qsTranslate("NGS", "Installer package: ") : qsTranslate("NGS", "Game folder: ")) + row.destination
                textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; color: uiTheme.themeColors.muted; font.pixelSize: 14
            }
            Text {
                id: fileSize; objectName: "queueFileSize-" + row.identity
                visible: row.expanded
                x: 20; y: destinationLabel.y + destinationLabel.implicitHeight + 6; width: parent.width - 40
                text: qsTranslate("NGS", "Files written: ") + root.sizeLabel(row.completedBytes) + qsTranslate("NGS", " of ") + root.sizeLabel(row.totalBytes)
                textFormat: Text.PlainText; color: uiTheme.themeColors.accent; font.pixelSize: 16
            }
            Text {
                id: networkSize; objectName: "queueNetworkSize-" + row.identity
                visible: row.expanded
                x: 20; y: fileSize.y + fileSize.implicitHeight + 5; width: parent.width - 40
                text: qsTranslate("NGS", "Downloaded: ") + root.sizeLabel(row.networkBytes) + qsTranslate("NGS", " · Total download: ") + (row.networkTotal >= 0 ? root.sizeLabel(row.networkTotal) : qsTranslate("NGS", "calculated during transfer"))
                textFormat: Text.PlainText; color: uiTheme.themeColors.muted; font.pixelSize: 14
            }
            ProgressBar {
                id: transferProgress
                x: 20; y: row.expanded ? networkSize.y + networkSize.implicitHeight + 12 : 82; width: parent.width - 40; height: 8; value: row.fraction
                background: FluidSurface { radius: 6; color: uiTheme.themeColors.hover }
                contentItem: Item {
                    Rectangle {
                        width: parent.width * transferProgress.position; height: parent.height; radius: 6; color: uiTheme.themeColors.accent
                        Behavior on width { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
                    }
                }
            }
            Text {
                id: queueDetail
                visible: row.expanded
                x: 20; y: transferProgress.y + 26; width: parent.width - 450; height: 43
                text: row.status === "running" && root.bridge.downloadActive && root.bridge.operationId === row.identity
                      ? root.bridge.operationStatus : row.detail
                textFormat: Text.PlainText; wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
                color: uiTheme.themeColors.muted; font.pixelSize: 14
            }
            QueueButton {
                objectName: "queuePrimary-" + row.identity
                visible: row.expanded
                x: parent.width - 410; y: queueDetail.y; width: 190; height: 52; focusPolicy: Qt.NoFocus
                text: row.status === "error" && row.discard ? qsTranslate("NGS", "Retry cancellation · ") + root.hints.activate
                      : ["paused", "error"].indexOf(row.status) >= 0 ? qsTranslate("NGS", "Resume · ") + root.hints.activate : qsTranslate("NGS", "Pause · ") + root.hints.activate
                enabled: ["queued", "running", "paused", "error"].indexOf(row.status) >= 0
                highlighted: root.currentIndex === row.index && root.actionIndex === 0
                onClicked: { root.currentIndex = row.index; root.focused(); root.primary() }
            }
            QueueButton {
                objectName: "queueCancel-" + row.identity
                visible: row.expanded
                x: parent.width - 205; y: queueDetail.y; width: 185; height: 52; focusPolicy: Qt.NoFocus
                text: qsTranslate("NGS", "Cancel · ") + root.hints.toggle; enabled: row.status !== "cancelling"
                highlighted: root.currentIndex === row.index && root.actionIndex === 1
                onClicked: { root.currentIndex = row.index; root.focused(); root.bridge.cancelDownload(row.identity) }
            }
        }
    }
    ProgressBar { y: 145; width: Math.min(parent.width, 850); height: 20; value: root.bridge.operationProgress; visible: !root.jobs.length && root.bridge.operationBusy }
    QueueButton {
        objectName: "downloadAction"
        y: 190; width: 270; height: 58; visible: !root.jobs.length; focusPolicy: Qt.NoFocus
        text: root.bridge.operationBusy ? qsTranslate("NGS", "Cancel operation") : root.bridge.installedIds.length ? qsTranslate("NGS", "My games") : qsTranslate("NGS", "Open catalog")
        onClicked: { if (root.bridge.operationBusy) root.bridge.cancelOperation(); else root.bridge.action("activate") }
    }
}
