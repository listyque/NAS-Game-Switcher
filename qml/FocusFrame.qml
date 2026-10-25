import QtQuick

Rectangle {
    id: frame
    objectName: "focusFrame"
    property bool selected: false
    visible: selected && enabled
    color: "transparent"
    radius: 8
    border.width: 3
    border.color: "#ffffff"
    Rectangle {
        anchors.fill: parent; anchors.margins: -1
        radius: frame.radius + 1; color: "transparent"
        border.width: 5; border.color: "#26323e"
        visible: uiTheme.themeColors.light === true
        z: -1
    }
}
