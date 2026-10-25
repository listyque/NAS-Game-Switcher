import QtQuick

Window {
    id: root
    objectName: "topEdgeSwipeWindow"
    required property QtObject controller
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
    transientParent: null
    color: "#01000000"
    x: screen.virtualX + (screen.width - width) / 2; y: screen.virtualY
    // The first pointer must hit this HWND. Keep its visible target stable even at rest.
    // Only the compact handle owns touch; the rest of the title bar stays available.
    width: Math.min(480, screen.width); height: 32
    visible: controller.edgeWindowVisible
    property real reveal: 0
    Behavior on reveal { enabled: root.visible; NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
    readonly property bool autoHide: controller.edgeAutoHide
    onAutoHideChanged: if (visible) awaken()
    function awaken() { reveal = 1; if (autoHide) idle.restart(); else idle.stop() }
    Component.onCompleted: if (visible) Qt.callLater(awaken)
    onVisibleChanged: { if (visible) { reveal = 0; Qt.callLater(awaken) } else { idle.stop(); reveal = 0 } }
    Connections {
        target: root.controller
        function onEdgeContact(screenName) { if (screenName === root.screen.name) root.awaken() }
        function onEdgeDragChanged() { if (root.controller.dragProgress > 0) root.awaken() }
    }
    readonly property bool panelOpen: controller.visible || controller.exiting || controller.dragProgress > 0
    onPanelOpenChanged: if (!panelOpen && visible) awaken()
    Timer { id: idle; interval: 4000; onTriggered: if (root.autoHide && !root.panelOpen) root.reveal = 0 }
    Rectangle {
        id: handlePill
        objectName: "topEdgeHandlePill"
        anchors.horizontalCenter: parent.horizontalCenter
        readonly property bool hiddenByPanel: root.panelOpen
        width: 72
        height: 4
        y: hiddenByPanel ? -height - 2 : (-height + (height + 5) * root.reveal)
        radius: height / 2
        color: root.controller.keyboard.palette.panel_text
        opacity: hiddenByPanel ? 0 : 0.72 * root.reveal
        border.width: 0
    }
    MultiPointTouchArea {
        objectName: "edgeTouchArea"
        anchors.fill: parent
        Accessible.role: Accessible.Button
        Accessible.name: qsTranslate("NGS", "Swipe to open quick settings")
        enabled: root.controller.edgeEnabled
        mouseEnabled: false
        minimumTouchPoints: 1; maximumTouchPoints: 2
        onPressed: points => { for (const point of points) root.controller.edgeTouch(root.screen.name, point.pointId, point.x, point.y, true) }
        onUpdated: points => { for (const point of points) root.controller.edgeTouch(root.screen.name, point.pointId, point.x, point.y, true) }
        onReleased: points => { for (const point of points) root.controller.edgeTouch(root.screen.name, point.pointId, point.x, point.y, false) }
        onCanceled: root.controller.cancelEdgeTouch()
    }
}
