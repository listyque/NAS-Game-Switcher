import QtQuick
import QtQuick.Controls.Basic

ToolPage {
    id: root
    objectName: "bindingsPane"
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    property real uiScale: 1
    property int selectedField: 0
    property int page: 0
    onSelectedFieldChanged: {
        if ([6, 7, 8, 9, 12].indexOf(selectedField) >= 0 || selectedField >= 15) page = 1
        else if (selectedField >= 0 && selectedField !== 10) page = 0
        if (selectedField === 6 || selectedField === 7) Qt.callLater(reveal, selectedField === 6 ? macroName : macroSequence)
    }
    property bool holdCapture: false
    onHoldCaptureChanged: if (controls.capturing) controls.cancelCapture()
    readonly property var controls: bridge.desktop
    pageScale: uiScale


    onAboutToShow: { page = 0; selectedField = 0; flick.cancelFlick(); flick.contentX = 0; flick.contentY = 0 }
    function endEditing() { macroName.focus = false; macroSequence.focus = false; bridge.keyboard.hide() }
    onAboutToHide: { if (controls.capturing) controls.cancelCapture(); endEditing() }
    onCoveredChanged: if (covered) endEditing()
    function handleAction(action) {
        if (controls.capturing) return
        if (action === "menu") { close(); return }
        if (action === "back") { close(); return }
        let fields = (page === 0 ? [-2, -1, 0, 1, 13, 2, 3, 14, 4, 5, 11, 10] : [-2, -1, 12, 6, 7, 8, 9, 10]).filter(
            field => field !== 13 && field !== 14 || controls.rows[field - 13].assigned)
        if (page === 1) for (let i = 15; i < 15 + bridge.keyboard.macros.length * 3; i++) fields.push(i)
        if (["left", "right", "up", "down"].indexOf(action) >= 0) {
            macroName.focus = false; macroSequence.focus = false
            const position = fields.indexOf(selectedField)
            selectedField = fields[(position + (action === "right" || action === "down" ? 1 : fields.length - 1)) % fields.length]
        } else if (action === "activate") {
            if (selectedField < 0) { page = selectedField + 2; selectedField = page ? 12 : 0 }
            else if (selectedField < 6) {
                const key = controls.rows[Math.floor(selectedField / 2)].action
                if (selectedField % 2) controls.clear(key); else controls.beginCapture(key, holdCapture)
            } else if (selectedField === 6 || selectedField === 7) {
                (selectedField === 6 ? macroName : macroSequence).forceActiveFocus()
                bridge.keyboard.show()
            } else if (selectedField === 8) saveMacro()
            else if (selectedField === 9) bridge.keyboard.toggle()
            else if (selectedField === 10) close()
            else if (selectedField === 11) holdCapture = !holdCapture
            else if (selectedField === 12) bridge.keyboard.editPad()
            else if (selectedField === 13 || selectedField === 14) {
                const row = controls.rows[selectedField - 13]
                controls.setToggle(row.action, !row.toggle)
            }
            else {
                const macro = bridge.keyboard.macros[Math.floor((selectedField - 15) / 3)]
                const operation = (selectedField - 15) % 3
                if (operation === 1) { bridge.keyboard.removeMacro(macro.name); selectedField = bridge.keyboard.macros.length ? Math.min(selectedField, 14 + bridge.keyboard.macros.length * 3) : 12 }
                else if (operation === 2) bridge.keyboard.runMacro(macro.name)
                else editMacro(macro)
            }
        }
    }
    function saveMacro() {
        if (bridge.keyboard.saveMacro(macroName.text, macroSequence.text)) { macroName.text = ""; macroSequence.text = "" }
    }
    function editMacro(macro) { macroName.text = macro.name; macroSequence.text = macro.sequence; selectedField = 6 }
    function reveal(item) {
        if (!item || !root.visible || !item.visible || item.fieldIndex < 0 || item.fieldIndex === 10 || item.fieldIndex === 11) return
        // Resolve positioners before scrolling to a newly shown section.
        for (let node = item.parent; node && node !== flick; node = node.parent)
            if (typeof node.forceLayout === "function") node.forceLayout()
        const y = item.mapToItem(form, 0, 0).y
        const target = y < flick.contentY ? y : y + item.height > flick.contentY + flick.height ? y + item.height - flick.height : flick.contentY
        flick.contentY = Math.max(0, Math.min(Math.max(0, flick.contentHeight - flick.height), target))
    }
    component ControlButton: FluidButton {
        id: control
        property int fieldIndex: -1
        property string glyph: "@settings"
        focusPolicy: Qt.NoFocus
        implicitWidth: Math.min(root.availableWidth, Math.max(200 * root.uiScale, metrics.width + 122 * root.uiScale))
        TextMetrics { id: metrics; text: control.text; font: control.font }
        font.pixelSize: 16 * root.uiScale
        height: Math.max(54, 58 * root.uiScale)
        highlighted: fieldIndex === root.selectedField
        onPressed: root.selectedField = fieldIndex
        onHighlightedChanged: if (highlighted && root.visible) Qt.callLater(root.reveal, this)
        onVisibleChanged: if (visible && highlighted) Qt.callLater(root.reveal, this)
        background: FluidSurface {
            radius: 16; color: control.highlighted ? uiTheme.themeColors.soft : control.checked ? uiTheme.themeColors.button : "transparent"; border.width: 0; shadowEnabled: false
            Rectangle {
                x: 0; anchors.verticalCenter: parent.verticalCenter; width: 3; height: parent.height * .45; radius: 1.5
                color: uiTheme.themeColors.accent; opacity: control.highlighted ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: 160 } }
            }
        }
        Accessible.name: text
        contentItem: Item {
            Item {
                width: 22 * root.uiScale; height: width; anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                KeyboardGlyph { width: parent.width; height: width; keyName: control.glyph; ink: uiTheme.themeColors.text }
            }
            Text {
                id: label; anchors.left: parent.left; anchors.leftMargin: 32 * root.uiScale
                anchors.right: parent.right; anchors.rightMargin: 54 * root.uiScale; anchors.verticalCenter: parent.verticalCenter
                text: control.text; color: control.enabled ? uiTheme.themeColors.text : uiTheme.themeColors.muted; font: control.font
                wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
            }
            Text {
                anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                text: control.fieldIndex === 10 ? root.hints.back : control.highlighted ? root.hints.activate : ""
                color: uiTheme.themeColors.muted; font.pixelSize: 13 * root.uiScale; font.weight: Font.DemiBold
            }
        }
    }
    contentItem: Column {
        spacing: 12 * root.uiScale
        Text { objectName: "bindingsHeading"; text: qsTranslate("NGS", "Buttons and gamepad"); color: uiTheme.themeColors.text; font.pixelSize: 32 * root.uiScale; font.weight: Font.DemiBold }
        Row {
            width: parent.width; spacing: 12 * root.uiScale
            ControlButton { objectName: "bindingsServices"; width: (parent.width - parent.spacing) / 2; fieldIndex: -2; checked: root.page === 0; text: qsTranslate("NGS", "System shortcuts"); onClicked: { root.page = 0; root.selectedField = 0; flick.contentY = 0 } }
            ControlButton { objectName: "bindingsMacros"; width: (parent.width - parent.spacing) / 2; fieldIndex: -1; checked: root.page === 1; text: qsTranslate("NGS", "Macros and gamepad"); onClicked: { root.page = 1; root.selectedField = 12; flick.contentY = 0 } }
        }
        FluidSurface {
            width: parent.width; height: statusLabel.implicitHeight + 24 * root.uiScale
            radius: 16; color: uiTheme.themeColors.base; border.width: 0; shadowEnabled: false; glass: false
            Text {
                id: statusLabel; objectName: "bindingsStatus"
                x: 12 * root.uiScale; y: x; width: parent.width - x * 2
                color: root.controls.capturing ? uiTheme.themeColors.accent : uiTheme.themeColors.muted
                font.pixelSize: 15 * root.uiScale; wrapMode: Text.WordWrap
                text: root.controls.status || (root.page === 0 ? qsTranslate("NGS", "Select an action and choose Assign. Choose a short or long press below.") : qsTranslate("NGS", "Map gamepad buttons to keys or create a multi-step macro."))
            }
        }
        Flickable {
            id: flick; objectName: "bindingsFormScroll"
            width: parent.width; height: root.availableHeight - y - footer.height - 12 * root.uiScale
            contentWidth: width; contentHeight: form.height; clip: true
            flickableDirection: Flickable.VerticalFlick; boundsBehavior: Flickable.StopAtBounds
            onWidthChanged: contentX = 0
            ScrollBar.vertical: ScrollBar {}
            Column {
                id: form
                width: flick.width; spacing: 12 * root.uiScale
                Repeater {
                    model: root.controls.rows
                    FluidSurface {
                        id: service
                        visible: root.page === 0
                        required property var modelData
                        required property int index
                        objectName: "bindingGroup-" + modelData.action
                        width: form.width; height: serviceContent.implicitHeight + 32 * root.uiScale
                        color: uiTheme.themeColors.base; radius: 18; border.width: 0; shadowEnabled: false; glass: false
                        Column {
                        id: serviceContent; x: 16 * root.uiScale; y: x; width: parent.width - x * 2; spacing: 10 * root.uiScale
                        Text { width: parent.width; text: service.modelData.name; color: uiTheme.themeColors.text; font.pixelSize: 20 * root.uiScale; font.weight: Font.DemiBold; wrapMode: Text.WordWrap }
                        Text { width: parent.width; text: service.modelData.binding; color: uiTheme.themeColors.accent; font.pixelSize: 19 * root.uiScale; wrapMode: Text.WordWrap }
                        Row {
                            width: parent.width; spacing: 10 * root.uiScale
                            ControlButton { width: (parent.width - parent.spacing) / 2; fieldIndex: service.index * 2; text: root.controls.capturing === service.modelData.action ? qsTranslate("NGS", "Cancel", "Отмена") : qsTranslate("NGS", "Assign"); onClicked: { if (root.controls.capturing === service.modelData.action) root.controls.cancelCapture(); else root.controls.beginCapture(service.modelData.action, root.holdCapture) } }
                            ControlButton { width: (parent.width - parent.spacing) / 2; fieldIndex: service.index * 2 + 1; glyph: "@stop"; text: qsTranslate("NGS", "Remove"); onClicked: root.controls.clear(service.modelData.action) }
                        }
                        Text { visible: text.length > 0; width: parent.width; wrapMode: Text.WordWrap; text: service.modelData.error; color: uiTheme.themeColors.warning; font.pixelSize: 14 * root.uiScale }
                        ControlButton {
                            objectName: "toggleBinding_" + service.modelData.action
                            width: parent.width; visible: service.modelData.action !== "keyboard"
                            enabled: service.modelData.assigned
                            fieldIndex: 13 + service.index
                            text: qsTranslate("NGS", "Toggle: ") + (service.modelData.toggle ? qsTranslate("NGS", "on · hide / restore") : qsTranslate("NGS", "off · single action"))
                            onClicked: root.controls.setToggle(service.modelData.action, !service.modelData.toggle)
                        }
                        }
                    }
                }
              Column {
                visible: root.page === 1; width: parent.width; spacing: 12 * root.uiScale
                Text { text: qsTranslate("NGS", "Keyboard macros"); color: uiTheme.themeColors.text; font.pixelSize: 21 * root.uiScale }
                ControlButton { text: qsTranslate("NGS", "Gamepad to keys and shortcuts"); fieldIndex: 12; onClicked: root.bridge.keyboard.editPad() }
                Text { width: parent.width; text: qsTranslate("NGS", "Example: Ctrl+C; wait:200; Ctrl+V. Join keys with + and steps with ;. Up to 20 macros, 100 steps / 30 seconds."); wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 13 * root.uiScale }
                TextField {
                    id: macroName; objectName: "macroName"; width: parent.width; height: Math.max(54, 58 * root.uiScale); padding: 14 * root.uiScale; font.pixelSize: 18 * root.uiScale
                    placeholderText: qsTranslate("NGS", "Macro name"); maximumLength: 40; selectByMouse: true
                    onActiveFocusChanged: if (activeFocus) { root.selectedField = 6; root.reveal(this) }
                    background: FluidSurface { radius: 16; border.width: 0; shadowEnabled: false; color: root.selectedField === 6 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
                    color: uiTheme.themeColors.text
                }
                TextField {
                    id: macroSequence; objectName: "macroSequence"; width: parent.width; height: Math.max(54, 58 * root.uiScale); padding: 14 * root.uiScale; font.pixelSize: 18 * root.uiScale
                    placeholderText: "Ctrl+Shift+S; wait:200; Enter"; maximumLength: 2000; selectByMouse: true
                    onActiveFocusChanged: if (activeFocus) { root.selectedField = 7; root.reveal(this) }
                    background: FluidSurface { radius: 16; border.width: 0; shadowEnabled: false; color: root.selectedField === 7 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
                    color: uiTheme.themeColors.text
                }
                Row {
                    width: parent.width; spacing: 12 * root.uiScale
                    ControlButton { width: (parent.width - parent.spacing) / 2; text: qsTranslate("NGS", "Save macro"); fieldIndex: 8; onClicked: root.saveMacro() }
                    ControlButton { width: (parent.width - parent.spacing) / 2; objectName: "showVirtualKeyboard"; text: qsTranslate("NGS", "Open keyboard"); fieldIndex: 9; onClicked: root.bridge.keyboard.toggle() }
                }
                Text { width: parent.width; text: root.bridge.keyboard.status; wrapMode: Text.WordWrap; color: uiTheme.themeColors.accent; font.pixelSize: 14 * root.uiScale }
                Repeater {
                    model: root.bridge.keyboard.macros
                    Column {
                        required property var modelData
                        required property int index
                        width: form.width; spacing: 5 * root.uiScale
                        Text { width: parent.width; text: modelData.name + " · " + modelData.sequence; elide: Text.ElideRight; color: uiTheme.themeColors.text; font.pixelSize: 14 * root.uiScale }
                        Row {
                            spacing: 12 * root.uiScale
                            ControlButton { text: qsTranslate("NGS", "Edit"); fieldIndex: 15 + parent.parent.index * 3; onClicked: root.editMacro(parent.parent.modelData) }
                            ControlButton { text: qsTranslate("NGS", "Delete"); fieldIndex: 16 + parent.parent.index * 3; onClicked: root.bridge.keyboard.removeMacro(parent.parent.modelData.name) }
                            ControlButton { text: qsTranslate("NGS", "Run"); fieldIndex: 17 + parent.parent.index * 3; onClicked: root.bridge.keyboard.runMacro(parent.parent.modelData.name) }
                        }
                    }
                }
              }
            }
        }
        Row {
            id: footer; spacing: 16 * root.uiScale
            ControlButton { text: qsTranslate("NGS", "Back"); glyph: "@back"; fieldIndex: 10; onClicked: root.close() }
            ControlButton { objectName: "capturePressMode"; visible: root.page === 0; text: root.holdCapture ? qsTranslate("NGS", "Long press · 0.7 s") : qsTranslate("NGS", "Short press"); fieldIndex: 11; onClicked: root.holdCapture = !root.holdCapture }
        }
    }
}
