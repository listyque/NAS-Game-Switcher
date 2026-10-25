import QtQuick
import QtQuick.Controls.Basic

Popup {
    id: popup
    // Popup anchors can retain the opening scale when a closing transition is interrupted.
    x: (parent.width - width) / 2
    y: (parent.height - height) / 2
    enter: enterAnimation
    property Transition enterAnimation: MotionTransition {}
    exit: exitAnimation
    property Transition exitAnimation: MotionTransition { closing: true }
    Connections {
        target: popup
        function onAboutToShow() { popup.enabled = true }
        function onAboutToHide() { popup.enabled = false }
    }
}
