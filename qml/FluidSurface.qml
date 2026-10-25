import QtQuick
import QtQuick.Effects

Rectangle {
    id: surface
    property bool pressed: parent && (parent.down === true || parent.padPressed === true)
    property bool focused: parent && (parent.highlighted === true || parent.activeFocus === true)
    property bool hovered: parent && parent.hovered === true
    property real elevation: pressed ? .12 : focused || hovered ? 1 : .22
    property real shadowBlur: 12
    property color shadowColor: "#000000"
    property bool shadowEnabled: glass
    property bool glass: width > 240 && height > 120 && color.a > 0 && radius >= 20
    radius: Math.min(24, height / 2)
    Behavior on elevation { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
    Behavior on color { ColorAnimation { duration: 180 } }
    Behavior on border.color { ColorAnimation { duration: 180 } }
    Rectangle {
        anchors.fill: parent; radius: surface.radius
        visible: GraphicsInfo.api === GraphicsInfo.Software && surface.color.a > 0 && surface.color.a < 1
        readonly property color base: uiTheme.themeColors.surface
        color: Qt.rgba(surface.color.r * surface.color.a + base.r * (1 - surface.color.a),
                       surface.color.g * surface.color.a + base.g * (1 - surface.color.a),
                       surface.color.b * surface.color.a + base.b * (1 - surface.color.a), 1)
    }
    Rectangle {
        anchors.fill: parent; radius: surface.radius
        visible: surface.glass
        color: "transparent"
        border.width: 1; border.color: uiTheme.themeColors.glassEdge
    }
    RectangularShadow {
        objectName: "fluidShadow"
        anchors.fill: parent; z: -1
        visible: surface.shadowEnabled && opacity > .001
        radius: surface.radius; blur: surface.shadowBlur
        offset.y: 6
        color: surface.shadowColor
        opacity: surface.elevation * (uiTheme.themeColors.light ? .08 : .18)
        cached: false
    }
}
