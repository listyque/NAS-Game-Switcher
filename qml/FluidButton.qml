import QtQuick
import QtQuick.Controls.Basic

Button {
    id: control
    implicitHeight: Math.max(48, contentItem.implicitHeight + topPadding + bottomPadding)
    implicitWidth: Math.max(48, contentItem.implicitWidth + leftPadding + rightPadding)
    padding: 12
    font.pixelSize: 16
    palette.buttonText: uiTheme.themeColors.text
    palette.highlightedText: uiTheme.themeColors.text
    contentItem: Text {
        text: control.text; font: control.font; color: uiTheme.themeColors.text
        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    property QtObject inputSource: uiTheme
    property bool padPressed: false
    readonly property bool pressing: down || padPressed
    property real compression: pressing ? 1 : 0
    property real emphasis: enabled && (highlighted || hovered) ? 1 : 0
    Behavior on compression {
        NumberAnimation { duration: control.pressing ? 75 : 380; easing.type: control.pressing ? Easing.OutQuad : Easing.OutBack; easing.overshoot: .8 }
    }
    Behavior on emphasis { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
    Timer { id: releasePress; interval: 75; onTriggered: control.padPressed = false }
    Connections {
        target: control.inputSource
        function onAction(action) {
            if (action === "activate" && control.visible && control.enabled && control.highlighted) {
                control.padPressed = true; releasePress.restart()
            }
        }
    }
    transform: [
        Scale {
            objectName: "fluidButtonScale"
            origin.x: control.width / 2; origin.y: control.height / 2
            xScale: 1 + control.compression * .014 + control.emphasis * .008
            yScale: 1 - control.compression * .075 + control.emphasis * .008
        },
        Translate { y: control.compression * 1.5 }
    ]
    FocusFrame {
        anchors.fill: parent; z: 100
        selected: control.enabled && (control.highlighted || control.hovered || control.activeFocus)
        radius: control.background && control.background.radius !== undefined ? control.background.radius : 8
    }
    background: FluidSurface {
        color: control.down ? uiTheme.themeColors.hover : control.checked || control.highlighted ? uiTheme.themeColors.soft : uiTheme.themeColors.button
        border.width: 0
    }
}
