import QtQuick
import QtQuick.Controls.Basic

Item {
    id: root
    required property var rows
    property int selectedButton: 0x1000
    property bool navigating: true
    signal chosen(int button)
    signal navigated(int button)
    implicitWidth: 600; implicitHeight: 420
    readonly property var buttons: [
        {button: 0x10000, label: "LT", x: 105, y: 12, w: 100, h: 38}, {button: 0x20000, label: "RT", x: 395, y: 12, w: 100, h: 38},
        {button: 0x100, label: "LB", x: 95, y: 58, w: 120, h: 34}, {button: 0x200, label: "RB", x: 385, y: 58, w: 120, h: 34},
        {button: 0x40, label: "L3", x: 119, y: 131, w: 76, h: 76},
        {button: 0x80, label: "R3", x: 340, y: 244, w: 76, h: 76},
        {button: 0x1, label: "↑", x: 207, y: 224, w: 42, h: 42}, {button: 0x2, label: "↓", x: 207, y: 306, w: 42, h: 42},
        {button: 0x4, label: "←", x: 166, y: 265, w: 42, h: 42}, {button: 0x8, label: "→", x: 248, y: 265, w: 42, h: 42},
        {button: 0x1000, label: "A", x: 433, y: 210, w: 48, h: 48, tint: "#a8e480"},
        {button: 0x2000, label: "B", x: 482, y: 161, w: 48, h: 48, tint: "#f0aaa4"},
        {button: 0x4000, label: "X", x: 384, y: 161, w: 48, h: 48, tint: "#9acaf2"},
        {button: 0x8000, label: "Y", x: 433, y: 112, w: 48, h: 48, tint: "#f4d68b"},
        {button: 0x20, label: "View", x: 254, y: 155, w: 38, h: 36}, {button: 0x10, label: "Menu", x: 310, y: 155, w: 38, h: 36}]
    function bindingFor(button) { const row = rows.find(row => row.button === button); return row ? row.keys : "" }
    function buttonName(button) {
        const names = {1:qsTranslate("NGS", "D-pad up"), 2:qsTranslate("NGS", "D-pad down"), 4:qsTranslate("NGS", "D-pad left"), 8:qsTranslate("NGS", "D-pad right"),
            16:qsTranslate("NGS", "Start / menu"), 32:qsTranslate("NGS", "View / back"), 64:qsTranslate("NGS", "Left stick press · L3"), 128:qsTranslate("NGS", "Right stick press · R3"),
            256:qsTranslate("NGS", "Left bumper · LB"), 512:qsTranslate("NGS", "Right bumper · RB"), 65536:qsTranslate("NGS", "Left trigger · LT"), 131072:qsTranslate("NGS", "Right trigger · RT")}
        const row = rows.find(row => row.button === button)
        return names[button] || (row ? row.name : qsTranslate("NGS", "Button"))
    }
    function move(dx, dy) {
        const current = buttons.find(item => item.button === selectedButton) || buttons[0]
        let best = null, score = Infinity
        for (let item of buttons) {
            const x = item.x + item.w / 2 - current.x - current.w / 2
            const y = item.y + item.h / 2 - current.y - current.h / 2
            const along = x * dx + y * dy, across = Math.abs(x * dy - y * dx)
            if (along <= 1) continue
            const distance = along + across * 3
            if (distance < score) { best = item; score = distance }
        }
        if (best) navigated(best.button)
    }
    Item {
        width: 600; height: 420
        scale: Math.min(root.width / width, root.height / height)
        transformOrigin: Item.TopLeft
        x: (root.width - width * scale) / 2; y: (root.height - height * scale) / 2
        Canvas {
            anchors.fill: parent
            property var colors: uiTheme.themeColors
            onColorsChanged: requestPaint()
            onPaint: {
                const c = getContext("2d"); c.reset()
                c.beginPath(); c.moveTo(142, 94); c.bezierCurveTo(80, 85, 62, 140, 38, 240)
                c.bezierCurveTo(12, 350, 40, 415, 85, 390); c.lineTo(190, 320)
                c.bezierCurveTo(250, 352, 350, 352, 410, 320); c.lineTo(515, 390)
                c.bezierCurveTo(560, 415, 588, 350, 562, 240); c.bezierCurveTo(538, 140, 520, 85, 458, 94)
                c.bezierCurveTo(350, 106, 250, 106, 142, 94); c.closePath()
                const gradient = c.createLinearGradient(0, 90, 0, 400); gradient.addColorStop(0, colors.button); gradient.addColorStop(1, colors.base)
                c.fillStyle = gradient; c.fill(); c.strokeStyle = colors.border; c.lineWidth = 2; c.stroke()
            }
        }
        Rectangle { x: 282; y: 111; width: 36; height: 25; radius: 12; color: uiTheme.themeColors.base; Text { anchors.centerIn: parent; text: "NGS"; color: uiTheme.themeColors.muted; font.pixelSize: 10 } }
        Repeater {
            model: root.buttons
            FluidButton {
                id: button
                required property var modelData
                x: modelData.x; y: modelData.y; width: modelData.w; height: modelData.h; focusPolicy: Qt.NoFocus
                readonly property bool selected: root.selectedButton === modelData.button
                highlighted: selected && root.navigating
                readonly property string assignment: root.bindingFor(modelData.button)
                objectName: "padDiagram-" + modelData.button
                Accessible.name: root.buttonName(modelData.button) + ": " + (assignment || qsTranslate("NGS", "Not assigned"))
                background: FluidSurface {
                    radius: button.modelData.w > 80 ? 10 : width / 2
                    color: button.selected ? uiTheme.themeColors.soft : button.down ? uiTheme.themeColors.hover : uiTheme.themeColors.base
                    border.width: button.selected ? 3 : 2
                    border.color: button.selected ? uiTheme.themeColors.accent : uiTheme.themeColors.border
                }
                contentItem: Text { text: button.modelData.label; color: button.modelData.tint || uiTheme.themeColors.text; font.pixelSize: button.modelData.label.length > 3 ? 10 : button.modelData.w > 70 ? 18 : 23; font.weight: Font.DemiBold; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                Rectangle { visible: !!button.assignment; anchors.right: parent.right; anchors.bottom: parent.bottom; width: 8; height: 8; radius: 4; color: uiTheme.themeColors.accent; border.color: uiTheme.themeColors.base }
                onClicked: root.chosen(modelData.button)
            }
        }
    }
}
