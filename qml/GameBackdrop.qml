import QtQuick

Item {
    id: root
    required property url source
    property real pan: 0
    property real animationScale: 1
    property bool initialized: false
    property bool firstIsFront: true
    property bool loading: false
    property real blend: 0
    readonly property url displayedSource: firstIsFront ? first.source : second.source
    readonly property bool ready: first.status !== Image.Loading && second.status !== Image.Loading && !debounceTimer.running && !loading
    readonly property bool transitioning: loading || dissolve.running

    transform: Translate { id: panOffset }
    onPanChanged: if (initialized) updatePan()
    onSourceChanged: {
        if (!initialized) return
        if (String(displayedSource) === "" || animationScale <= 0.01) {
            debounceTimer.stop()
            loadLatest()
        } else {
            debounceTimer.restart()
        }
    }
    Timer {
        id: debounceTimer
        interval: 85
        repeat: false
        onTriggered: root.loadLatest()
    }
    onVisibleChanged: {
        if (!initialized) return
        debounceTimer.stop()
        panMotion.stop(); panOffset.x = pan
        if (visible) loadLatest()
        else { dissolve.stop(); loading = false; blend = 0 }
    }
    Component.onCompleted: {
        initialized = true; panOffset.x = pan
        if (visible) first.source = source
    }
    function updatePan() {
        panMotion.stop()
        if (!visible || animationScale <= 0.01) { panOffset.x = pan; return }
        panMotion.from = panOffset.x; panMotion.to = pan
        if (panMotion.from !== panMotion.to) panMotion.start()
    }
    NumberAnimation {
        id: panMotion; target: panOffset; property: "x"
        duration: Math.round(700 * root.animationScale); easing.type: Easing.OutCubic
    }

    function loadLatest() {
        // Retain the last usable artwork while the next selection has no image.
        if (!initialized || !String(source) || !visible || loading || dissolve.running) return
        const front = firstIsFront ? first : second
        const incoming = firstIsFront ? second : first
        if (String(front.source) === String(source)) return
        loading = true
        incoming.source = source
        incomingReady(incoming)
    }

    function incomingReady(image) {
        const incoming = firstIsFront ? second : first
        if (!loading || image !== incoming) return
        if (image.status === Image.Error) {
            loading = false
            if (String(image.source) !== String(source)) loadLatest()
            return
        }
        if (image.status !== Image.Ready) return
        loading = false
        if (!visible) return
        // Finish the current dissolve, then use the latest selection rather than queueing every keypress.
        if (String(image.source) !== String(source)) loadLatest()
        else if (root.animationScale <= 0.01) {
            root.firstIsFront = !root.firstIsFront
            root.blend = 0
            root.loadLatest()
        } else dissolve.start()
    }

    Image {
        id: first
        anchors.fill: parent
        sourceSize.width: 1600
        sourceSize.height: 1000
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        z: root.firstIsFront ? 0 : 1
        opacity: root.firstIsFront ? 1 : root.blend
        onStatusChanged: root.incomingReady(first)
    }
    Image {
        id: second
        anchors.fill: parent
        sourceSize.width: 1600
        sourceSize.height: 1000
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        z: root.firstIsFront ? 1 : 0
        opacity: root.firstIsFront ? root.blend : 1
        onStatusChanged: root.incomingReady(second)
    }
    NumberAnimation {
        id: dissolve
        target: root
        property: "blend"
        from: 0; to: 1
        duration: Math.round(420 * root.animationScale)
        easing.type: Easing.InOutCubic
        onFinished: {
            root.firstIsFront = !root.firstIsFront
            root.blend = 0
            root.loadLatest()
        }
    }
}
