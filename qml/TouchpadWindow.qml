import QtQuick
import QtQuick.Controls.Basic

Window {
    id: root
    readonly property var inputColors: keyboard.padPalette
    required property QtObject keyboard
    required property var hints
    property bool rightSide: false
    property real uiScale: 1
    readonly property var area: keyboard.screenArea
    objectName: rightSide ? "rightTouchpadWindow" : "leftTouchpadWindow"
    x: rightSide ? area.x + area.width - width : area.x
    y: area.y + area.height - height
    width: Math.min(area.width * .48, Math.max(240, Math.round(area.width * .3 * keyboard.options.touchpad_height_percent / 46)))
    height: Math.min(area.height, Math.max(220, Math.round(area.height * keyboard.options.touchpad_height_percent / 100)))
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
    transientParent: null
    visible: !keyboard.remoteHost && keyboard.visible && keyboard.touchpadMode
    color: "transparent"
    property bool controlsOpen: false
    onVisibleChanged: controlsOpen = false
    readonly property real padTop: toolbar.y + toolbar.height + 6
    readonly property real padBottom: (controlsOpen ? adjustments.y : sensitivityButton.y) - 6
    readonly property var toolRects: {
        const rects = [0,1,2].map(i => ({index:i, x:toolbar.x + i * ((root.width - 20) / 3 + 4), y:toolbar.y, width:(root.width - 20) / 3, height:toolbar.height}))
        rects.push({index:3, x:sensitivityButton.x, y:sensitivityButton.y, width:sensitivityButton.width, height:sensitivityButton.height})
        if (controlsOpen) for (let i = 0; i < 3; ++i) {
            rects.push({index:4+i*2, x:adjustments.x, y:adjustments.y+i*38, width:40, height:34})
            rects.push({index:5+i*2, x:adjustments.x+adjustments.width-40, y:adjustments.y+i*38, width:40, height:34})
        }
        return rects
    }
    Rectangle { anchors.fill: parent; color: root.inputColors.panel; opacity: root.keyboard.options.opacity; radius: 8 }
    Row {
        id: toolbar; x: 6; y: 6; spacing: 4
        Repeater {
            model: root.rightSide ? [qsTranslate("NGS", "Left click"), qsTranslate("NGS", "Right click"), qsTranslate("NGS", "Hide")] : [qsTranslate("NGS", "Keys"), "PrtSc", "Pad"]
            FluidButton {
                id: toolButton
                inputSource: root.keyboard
                required property string modelData
                required property int index
                property bool screenshotHeld: false
                onPressed: screenshotHeld = false
                Timer {
                    interval: 700; running: !root.rightSide && toolButton.index === 1 && toolButton.down
                    onTriggered: { toolButton.screenshotHeld = true; root.keyboard.takeScreenshot(true) }
                }
                width: (root.width - 20) / 3; height: 36; text: modelData; focusPolicy: Qt.NoFocus
                font.pixelSize: 12
                opacity: Math.max(.45, root.keyboard.options.opacity)
                Accessible.name: modelData
                background: KeyboardSurface { inputColors: root.inputColors; pressed: parent.pressing; selected: parent.highlighted; hovered: parent.hovered; subtle: true }
                contentItem: Text { text: parent.text; font: parent.font; color: root.inputColors.text; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                onClicked: if (!screenshotHeld) root.activateTool(index)
            }
        }
    }
    function activateTool(index) {
        if (!rightSide || index === 2) keyboard.feedback()
        if (index === 3) controlsOpen = !controlsOpen
        else if (index >= 4) {
            const delta = index % 2 === 0 ? -1 : 1
            if (index < 6) keyboard.setPointerSensitivity(Math.max(.25, Math.min(3, Math.round((keyboard.options.pointer_sensitivity + delta * .25) * 100) / 100)))
            else if (index < 8) keyboard.setTouchpadHeight(Math.max(20, Math.min(75, keyboard.options.touchpad_height_percent + delta * 5)))
            else keyboard.setOpacity(Math.max(.15, Math.min(1, Math.round((keyboard.options.opacity + delta * .05) * 100) / 100)))
        }
        else if (rightSide) { if (index === 2) keyboard.hide(); else keyboard.clickPointer(index === 0 ? "left" : "right") }
        else if (index === 0) keyboard.setTouchpads(false)
        else if (index === 1) keyboard.press("PrintScreen")
        else keyboard.setPadEnabled(!keyboard.padEnabled)
    }
    Connections {
        target: root.keyboard
        function onPadTool(rightSide, index) { if (root.visible && root.rightSide === rightSide) root.activateTool(index) }
    }
    Text { anchors.centerIn: pad; text: root.rightSide ? qsTranslate("NGS", "Right touchpad") : qsTranslate("NGS", "Left touchpad"); color: root.inputColors.muted; opacity: root.keyboard.options.opacity; font.pixelSize: 15 }
    Column {
        id: adjustments; x: 6; y: sensitivityButton.y - height - 6; width: parent.width - 12; spacing: 4
        visible: root.controlsOpen
        Repeater {
            model: [qsTranslate("NGS", "Speed · ") + root.keyboard.options.pointer_sensitivity.toFixed(2) + "×", qsTranslate("NGS", "Size · ") + root.keyboard.options.touchpad_height_percent + "%", qsTranslate("NGS", "Opacity · ") + Math.round(root.keyboard.options.opacity * 100) + "%"]
            Row {
                required property string modelData; required property int index
                width: adjustments.width; height: 34
                FluidButton {
                    objectName: "touchpadAdjustMinus-" + parent.index
                    inputSource: root.keyboard; width: 40; height: 34; text: "−"; focusPolicy: Qt.NoFocus
                    opacity: Math.max(.45, root.keyboard.options.opacity)
                    background: KeyboardSurface { inputColors: root.inputColors; pressed: parent.pressing; selected: parent.highlighted; hovered: parent.hovered }
                    contentItem: Text { text: parent.text; color: root.inputColors.text; font.pixelSize: 22; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    Accessible.name: qsTranslate("NGS", "Decrease: ") + parent.modelData
                    onClicked: root.activateTool(4 + parent.index * 2)
                }
                Text { text: parent.modelData; width: parent.width - 80; height: 34; color: root.inputColors.panel_text; font.pixelSize: 13; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                FluidButton {
                    objectName: "touchpadAdjustPlus-" + parent.index
                    inputSource: root.keyboard; width: 40; height: 34; text: "+"; focusPolicy: Qt.NoFocus
                    opacity: Math.max(.45, root.keyboard.options.opacity)
                    background: KeyboardSurface { inputColors: root.inputColors; pressed: parent.pressing; selected: parent.highlighted; hovered: parent.hovered }
                    contentItem: Text { text: parent.text; color: root.inputColors.text; font.pixelSize: 22; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                    Accessible.name: qsTranslate("NGS", "Increase: ") + parent.modelData
                    onClicked: root.activateTool(5 + parent.index * 2)
                }
            }
        }
    }
    FluidButton {
        id: sensitivityButton; objectName: "touchpadSensitivity"
        x: 6; y: parent.height - height - 6; width: parent.width - 12; height: 36
        inputSource: root.keyboard; focusPolicy: Qt.NoFocus; opacity: Math.max(.45, root.keyboard.options.opacity)
        text: qsTranslate("NGS", "Sensitivity · ") + root.keyboard.options.pointer_sensitivity.toFixed(2) + "×  " + (root.controlsOpen ? "⌄" : "⌃")
        background: KeyboardSurface { inputColors: root.inputColors; pressed: parent.pressing; selected: parent.highlighted; hovered: parent.hovered; subtle: true }
        contentItem: Text { text: parent.text; color: root.inputColors.text; font.pixelSize: 13; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
        onClicked: root.activateTool(3)
    }
    MultiPointTouchArea {
        id: pad; objectName: root.rightSide ? "rightTouchpadArea" : "leftTouchpadArea"
        x: 0; y: root.padTop; width: parent.width; height: Math.max(0, root.padBottom - y)
        enabled: !root.keyboard.nativeTouch
        minimumTouchPoints: 1; maximumTouchPoints: 1; mouseEnabled: false
        property real lastX: 0
        property real lastY: 0
        property real distance: 0
        property double started: 0
        touchPoints: [TouchPoint { id: finger }]
        onPressed: { lastX = finger.x; lastY = finger.y; distance = 0; started = Date.now() }
        onUpdated: {
            const dx = finger.x - lastX, dy = finger.y - lastY
            distance += Math.abs(dx) + Math.abs(dy)
            root.keyboard.movePointer(dx, dy); lastX = finger.x; lastY = finger.y
        }
        onReleased: if (distance < 10 && Date.now() - started < 350) root.keyboard.clickPointer("left")
        onCanceled: { distance = 100; started = 0 }
    }
}
