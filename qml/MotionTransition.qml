import QtQuick

Transition {
    id: motion
    property bool closing: false
    ParallelAnimation {
        NumberAnimation { property: "opacity"; from: motion.closing ? 1 : 0; to: motion.closing ? 0 : 1; duration: motion.closing ? 140 : 160; easing.type: Easing.OutCubic }
        NumberAnimation {
            property: "scale"; from: motion.closing ? 1 : .96; to: motion.closing ? .985 : 1
            duration: motion.closing ? 140 : 360
            easing.type: motion.closing ? Easing.InQuad : Easing.OutBack; easing.overshoot: .65
        }
    }
}
