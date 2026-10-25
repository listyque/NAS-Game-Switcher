import QtQuick

QtObject {
    id: root
    required property QtObject keyboard
    required property QtObject appearance
    required property QtObject statusOverlay
    property var status: StatusBarWindow { bridge: root.statusOverlay }
    property var keys: KeyboardWindow { keyboard: root.keyboard; hints: root.appearance.hints }
    property var left: TouchpadWindow { keyboard: root.keyboard; hints: root.appearance.hints }
    property var right: TouchpadWindow { keyboard: root.keyboard; hints: root.appearance.hints; rightSide: true }
}
