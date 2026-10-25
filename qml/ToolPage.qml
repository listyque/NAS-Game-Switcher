import QtQuick
import QtQuick.Controls.Basic

MotionPopup {
    id: toolPage
    readonly property real animScale: uiTheme.animationScale !== undefined ? uiTheme.animationScale : 1.0
    property real pageScale: 1
    property string navigationPath: ""
    property bool covered: false
    opacity: covered ? 0 : 1
    Behavior on opacity { NumberAnimation { duration: Math.round(180 * toolPage.animScale) } }
    implicitWidth: 0; implicitHeight: 0
    x: 0; y: 0; width: parent.width; height: parent.height
    leftPadding: Math.max(width * .05, (width - 1080 * pageScale) / 2)
    rightPadding: leftPadding
    topPadding: Math.max(24, height * .06) + (navigationPath ? 34 * pageScale : 0)
    bottomPadding: Math.max(24, height * .05)
    modal: true; focus: true
    closePolicy: Popup.NoAutoClose
    enterAnimation: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Math.round(180 * toolPage.animScale) } }
    exitAnimation: Transition { NumberAnimation { property: "opacity"; from: 1; to: 0; duration: Math.round(140 * toolPage.animScale) } }
    background: Item {
        FluidSurface { anchors.fill: parent; radius: 0; color: uiTheme.themeColors.glass; glass: false; shadowEnabled: false }
        Text {
            objectName: "toolNavigationPath"
            x: toolPage.leftPadding; y: Math.max(24, toolPage.height * .06)
            width: toolPage.availableWidth; visible: toolPage.navigationPath.length > 0
            text: toolPage.navigationPath; color: uiTheme.themeColors.muted; font.pixelSize: 14 * toolPage.pageScale
            elide: Text.ElideRight
        }
    }
    Overlay.modal: Rectangle { color: toolPage.covered ? "transparent" : uiTheme.themeColors.scrim }
}
