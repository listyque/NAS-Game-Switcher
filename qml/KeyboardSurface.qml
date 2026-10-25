import QtQuick

Rectangle {
    id: surface
    property bool pressed: false
    property bool held: false
    property bool selected: false
    property bool subtle: false
    property bool hovered: false
    property var inputColors: ({edge:"#262629",key:"#363639",text:"#f2f2f4",accent:"#b8cce7",hover:"#454549",pressed:"#505054",held:"#475e76"})
    radius: 4
    color: inputColors.edge
    border.width: selected ? 2 : 0
    border.color: inputColors.accent
    Rectangle {
        x: surface.selected ? 2 : 0; y: surface.selected ? 2 : surface.pressed ? 1 : 0
        width: parent.width - (surface.selected ? 4 : 0)
        height: parent.height - (surface.selected ? 4 : surface.pressed ? 1 : 2)
        radius: surface.radius
        color: surface.pressed ? surface.inputColors.pressed : surface.held ? surface.inputColors.held : surface.hovered ? surface.inputColors.hover : surface.inputColors.key
    }
}
