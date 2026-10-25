import QtQuick
import QtQuick.Controls.Basic
import "FocusNavigation.js" as Navigation

ToolPage {
    id: root
    objectName: "keyboardLayoutEditor"
    required property QtObject keyboard
    property var draft: ({id:"", name:"", rows:[]})
    property int rowIndex: 0
    property int columnIndex: 0
    property int selectedField: -1
    property int layerIndex: 0
    property string error: ""
    readonly property var layers: ["base", "shift", "altgr", "shift_altgr"]
    readonly property var keyChoices: keyboard.layoutKeyNames.map(key => ({key:key, title:({"@settings":qsTranslate("NGS", "Keyboard settings"), "@touchpads":qsTranslate("NGS", "Open touchpads"), "@voice":qsTranslate("NGS", "Windows voice typing"), PrintScreen:qsTranslate("NGS", "Screenshot — Print Screen"), "@hide":qsTranslate("NGS", "Hide keyboard"), "@language":qsTranslate("NGS", "Change keyboard language"), "@symbols":qsTranslate("NGS", "Numbers and symbols"), "@letters":qsTranslate("NGS", "Letters"), AltGr:qsTranslate("NGS", "AltGr — right Alt"), RightAlt:qsTranslate("NGS", "Right Alt"), RightCtrl:qsTranslate("NGS", "Right Ctrl"), RightShift:qsTranslate("NGS", "Right Shift"), Space:qsTranslate("NGS", "Space")}[key] || (/^Num\d$/.test(key) ? key.slice(3) + " · NumPad" : key))}))
    readonly property var chosen: draft.rows.length ? draft.rows[rowIndex][columnIndex] : ({key:"A", width:1, states:{}})
    readonly property bool canEditSymbols: chosen.key[0] !== "@" && ["Ctrl", "Shift", "Alt", "Win", "AltGr", "RightShift", "RightCtrl", "RightAlt"].indexOf(chosen.key) < 0
    readonly property var fields: [nameInput, keyType, widthMinus, widthPlus, baseInput, shiftInput, altgrInput, bothInput, moveLeft, moveRight, moveUp, moveDown, addKey, removeKey, addRow, removeRow, copyLayout, saveButton, cancelButton]
    readonly property bool typing: keyboard.visible && !keyboard.touchpadMode
    width: parent.width
    height: typing ? nameInput.activeFocus ? 76 : Math.max(96, parent.height * (1 - keyboard.options.height_percent / 100) - 24) : parent.height
    y: typing ? 8 : (parent.height - height) / 2
    topPadding: typing ? 10 : Math.max(24, height * .06)
    bottomPadding: typing ? 10 : Math.max(24, height * .05)
    leftPadding: typing ? 10 : Math.max(width * .05, (width - 1080) / 2)
    rightPadding: leftPadding
    background: FluidSurface { color: uiTheme.themeColors.glass; radius: root.typing ? 20 : 0; glass: false; shadowEnabled: false }
    Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim }
    function begin(value) { draft = JSON.parse(JSON.stringify(value)); rowIndex = 0; columnIndex = 0; selectedField = -1; layerIndex = 0; error = ""; nameInput.text = draft.name; loadKey() }
    function loadKey() {
        keyType.currentIndex = keyboard.layoutKeyNames.indexOf(chosen.key)
        baseInput.text = chosen.states.base || ""; shiftInput.text = chosen.states.shift || ""
        altgrInput.text = chosen.states.altgr || ""; bothInput.text = chosen.states.shift_altgr || ""
    }
    function flush() {
        const next = JSON.parse(JSON.stringify(draft)), states = {}, inputs = [baseInput, shiftInput, altgrInput, bothInput]
        if (canEditSymbols) layers.forEach((layer, i) => { if (inputs[i].text) states[layer] = inputs[i].text })
        next.name = nameInput.text
        next.rows[rowIndex][columnIndex].states = states
        draft = next
    }
    function select(row, column) { flush(); rowIndex = row; columnIndex = column; loadKey(); selectedField = -1 }
    function changeKey(key) {
        flush(); const next = JSON.parse(JSON.stringify(draft)); next.rows[rowIndex][columnIndex].key = key
        if (["Ctrl", "Shift", "Alt", "Win", "AltGr", "RightShift", "RightCtrl", "RightAlt"].indexOf(key) >= 0 || key[0] === "@") next.rows[rowIndex][columnIndex].states = {}
        draft = next; loadKey()
    }
    function resizeKey(delta) { flush(); const next = JSON.parse(JSON.stringify(draft)); next.rows[rowIndex][columnIndex].width = Math.max(.5, Math.min(8, chosen.width + delta)); draft = next }
    function moveKey(dx, dy) {
        flush(); const next = JSON.parse(JSON.stringify(draft)), target = Math.max(0, Math.min(next.rows.length - 1, rowIndex + dy))
        if (dy && next.rows[rowIndex].length === 1) { error = qsTranslate("NGS", "Move the last key together with its row, or add another key first."); return }
        const item = next.rows[rowIndex].splice(columnIndex, 1)[0]
        const column = Math.max(0, Math.min(next.rows[target].length, columnIndex + dx))
        next.rows[target].splice(column, 0, item); draft = next; rowIndex = target; columnIndex = column; loadKey()
    }
    function add(row) {
        flush(); const next = JSON.parse(JSON.stringify(draft)), item = {key:"A", width:1, states:{}}
        if (row) { if (next.rows.length >= 8) return; next.rows.splice(rowIndex + 1, 0, [item]); rowIndex++ ; columnIndex = 0 }
        else { if (next.rows[rowIndex].length >= 24) return; next.rows[rowIndex].splice(columnIndex + 1, 0, item); columnIndex++ }
        draft = next; loadKey()
    }
    function remove(row) {
        flush(); const next = JSON.parse(JSON.stringify(draft))
        if (["@settings", "@hide"].indexOf(chosen.key) >= 0 || row && next.rows[rowIndex].some(item => ["@settings", "@hide"].indexOf(item.key) >= 0)) { error = qsTranslate("NGS", "The settings and hide buttons are required. You can move them."); return }
        if (row || next.rows[rowIndex].length === 1) { if (next.rows.length === 1) return; next.rows.splice(rowIndex, 1); rowIndex = Math.min(rowIndex, next.rows.length - 1); columnIndex = 0 }
        else { next.rows[rowIndex].splice(columnIndex, 1); columnIndex = Math.min(columnIndex, next.rows[rowIndex].length - 1) }
        draft = next; loadKey()
    }
    function display(item) {
        const layer = layers[layerIndex]
        return item.states[layer] === undefined ? item.states.base === undefined ? ({"@settings":qsTranslate("NGS", "Options"), "@touchpads":qsTranslate("NGS", "Touchpads"), PrintScreen:"PrtSc", "@hide":qsTranslate("NGS", "Hide"), "@language":qsTranslate("NGS", "Language"), "@symbols":"&123", "@letters":qsTranslate("NGS", "Letters"), Space:qsTranslate("NGS", "Space")}[item.key] || item.key) : item.states.base : item.states[layer]
    }
    function save() { flush(); if (keyboard.saveLayout(draft)) close(); else error = keyboard.status }
    function handleAction(action) {
        if (keyType.popup.visible) {
            if (action === "back") { keyType.currentIndex = keyboard.layoutKeyNames.indexOf(chosen.key); keyType.popup.close() }
            else if (action === "up" || action === "down") keyType.currentIndex = Math.max(0, Math.min(keyboard.layoutKeyNames.length - 1, keyType.currentIndex + (action === "down" ? 1 : -1)))
            else if (action === "activate") { changeKey(keyboard.layoutKeyNames[keyType.currentIndex]); keyType.popup.close() }
            return
        }
        if (action === "back" || action === "menu") { close(); return }
        if (action === "next_control" || action === "previous_control") {
            let next = selectedField
            for (let i = 0; i < fields.length; i++) { next = (next + (action === "next_control" ? 1 : fields.length - 1) + fields.length) % fields.length; if (fields[next].visible && fields[next].enabled) { selectedField = next; break } }
            return
        }
        if (action === "next_section" || action === "previous_section") { layerIndex = (layerIndex + (action === "next_section" ? 1 : 3)) % 4; return }
        if (selectedField < 0) {
            if (action === "activate") selectedField = 1
            else if (action === "left" || action === "right") select(rowIndex, Math.max(0, Math.min(draft.rows[rowIndex].length - 1, columnIndex + (action === "right" ? 1 : -1))))
            else if (action === "up" || action === "down") { const row = Math.max(0, Math.min(draft.rows.length - 1, rowIndex + (action === "down" ? 1 : -1))); select(row, Math.min(columnIndex, draft.rows[row].length - 1)) }
        } else if (action === "activate") {
            if (selectedField === 1) keyType.popup.open()
            else if ([0,4,5,6,7].indexOf(selectedField) >= 0) { fields[selectedField].forceActiveFocus(); keyboard.show() }
            else fields[selectedField].clicked()
        } else { const next = Navigation.neighbor(fields, selectedField, action, root.contentItem); if (next >= 0) selectedField = next }
    }
    function revealField() {
        if (selectedField >= 1 && selectedField <= 7) {
            const item = fields[selectedField], top = item.mapToItem(sideColumn,0,0).y
            if (top < sidebar.contentY) sidebar.contentY = top
            else if (top + item.height > sidebar.contentY + sidebar.height) sidebar.contentY = top + item.height - sidebar.height
        }
    }
    onSelectedFieldChanged: Qt.callLater(revealField)
    Connections { target: root.keyboard; function onChanged() { if (root.visible) Qt.callLater(root.revealField) } }
    component EditorField: TextField {
        property int field: 0
        enabled: field < 4 || root.canEditSymbols
        height: 42; maximumLength: 16; color: uiTheme.themeColors.text
        font.pixelSize: 16
        background: Rectangle { radius: 12; color: root.selectedField === parent.field || parent.activeFocus ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
        onActiveFocusChanged: if (activeFocus) { root.selectedField = field; Qt.callLater(root.revealField) }
    }
    onAboutToHide: keyboard.finishEditor()
    Text { visible: !root.typing; text: qsTranslate("NGS", "Layout editor", "Редактор раскладки"); font.pixelSize: 24; color: uiTheme.themeColors.text }
    EditorField { id: nameInput; field: 0; visible: !root.typing || activeFocus; objectName: "layoutName"; x: root.typing ? 0 : 280; width: root.typing ? parent.width : parent.width - 280; height: 42; placeholderText: qsTranslate("NGS", "Layout name"); color: uiTheme.themeColors.text; maximumLength: 60 }
    Item {
        id: preview; objectName: "layoutPreview"; visible: !root.typing
        x: 0; y: 62; width: parent.width - 346; height: Math.max(80, Math.min(root.draft.rows.length * 63 - 5, parent.height - 240))
        readonly property real unit: draft.rows.length ? Math.min(...draft.rows.map(row => (preview.width - 4 * (row.length - 1)) / row.reduce((sum,item) => sum + item.width,0))) : 1
        Column {
            spacing: 5
            Repeater {
                model: root.draft.rows
                Row {
                    id: previewRow
                    required property var modelData; required property int index
                    spacing: 4
                    readonly property real unit: preview.unit
                    Repeater {
                        model: parent.modelData
                        FluidButton {
                            required property var modelData; required property int index
                            objectName: "layoutPreviewKey-" + previewRow.index + "-" + index
                            width: modelData.width * previewRow.unit; height: Math.max(20, Math.min(58, (preview.height - 5 * (root.draft.rows.length - 1)) / root.draft.rows.length))
                            padding: 2; text: root.display(modelData); highlighted: root.rowIndex === previewRow.index && root.columnIndex === index
                            font.pixelSize: Math.min(18, width / Math.max(1, text.length) * 1.25)
                            contentItem: Text { text: parent.text; textFormat: Text.PlainText; font: parent.font; color: uiTheme.themeColors.text; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
                            onClicked: root.select(previewRow.index, index)
                        }
                    }
                }
            }
        }
    }
    Row {
        visible: !root.typing
        x: 0; y: preview.y + preview.height + 14; spacing: 6
        Repeater {
            model: [qsTranslate("NGS", "Default"), "Shift", "AltGr", "Shift+AltGr"]
            FluidButton { required property string modelData; required property int index; text: modelData; width: (preview.width - 18) / 4; height: 42; highlighted: root.layerIndex === index; onClicked: { root.flush(); root.layerIndex = index } }
        }
    }
    Grid {
        visible: !root.typing
        x: 0; y: preview.y + preview.height + 66; width: preview.width; columns: 4; spacing: 6
        FluidButton { id: moveLeft; text: "←"; width: (preview.width - 18)/4; highlighted: root.selectedField === 8; onClicked: root.moveKey(-1,0) }
        FluidButton { id: moveRight; text: "→"; width: (preview.width - 18)/4; highlighted: root.selectedField === 9; onClicked: root.moveKey(1,0) }
        FluidButton { id: moveUp; text: "↑"; width: (preview.width - 18)/4; highlighted: root.selectedField === 10; onClicked: root.moveKey(0,-1) }
        FluidButton { id: moveDown; text: "↓"; width: (preview.width - 18)/4; highlighted: root.selectedField === 11; onClicked: root.moveKey(0,1) }
        FluidButton { id: addKey; text: qsTranslate("NGS", "+ Key"); width: (preview.width - 18)/4; font.pixelSize: 13; highlighted: root.selectedField === 12; onClicked: root.add(false) }
        FluidButton { id: removeKey; text: qsTranslate("NGS", "− Key"); width: (preview.width - 18)/4; font.pixelSize: 13; highlighted: root.selectedField === 13; onClicked: root.remove(false) }
        FluidButton { id: addRow; text: qsTranslate("NGS", "+ Row"); width: (preview.width - 18)/4; font.pixelSize: 13; highlighted: root.selectedField === 14; onClicked: root.add(true) }
        FluidButton { id: removeRow; text: qsTranslate("NGS", "− Row"); width: (preview.width - 18)/4; font.pixelSize: 13; highlighted: root.selectedField === 15; onClicked: root.remove(true) }
    }
    Flickable {
        id: sidebar
        visible: !root.typing || !nameInput.activeFocus
        x: root.typing ? 0 : parent.width - 322; y: root.typing ? 0 : 62; width: root.typing ? parent.width : 322
        height: parent.height - y - (root.typing ? 0 : 80)
        clip: true; contentHeight: sideColumn.implicitHeight; boundsBehavior: Flickable.StopAtBounds
        Column {
            id: sideColumn; width: sidebar.width; spacing: 8
        Text { text: qsTranslate("NGS", "Key / action"); color: uiTheme.themeColors.muted }
        FluidComboBox {
            id: keyType; objectName: "layoutKeyType"; width: parent.width; model: root.keyChoices; textRole: "title"; valueRole: "key"; onActivated: root.changeKey(currentValue)
            background: FluidSurface { radius: 12; border.width: 0; color: root.selectedField === 1 ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
        }
        Text { text: qsTranslate("NGS", "Width · ") + Math.round(root.chosen.width * 100) + qsTranslate("NGS", "% of a normal key"); color: uiTheme.themeColors.muted }
        Row {
            spacing: 8
            FluidButton { id: widthMinus; width: 154; text: "−"; highlighted: root.selectedField === 2; onClicked: root.resizeKey(-.25) }
            FluidButton { id: widthPlus; width: 154; text: "+"; highlighted: root.selectedField === 3; onClicked: root.resizeKey(.25) }
        }
        Text { text: qsTranslate("NGS", "An empty layer uses the base layer. Leave all fields empty for a normal key."); width: parent.width; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted }
        Grid {
            columns: 2; spacing: 8; width: parent.width
            Column { width: (parent.width - 8)/2; spacing: 5
                Text { text: qsTranslate("NGS", "Base layer"); color: uiTheme.themeColors.text }
                EditorField { id: baseInput; field: 4; objectName: "layoutBase"; width: parent.width }
            }
            Column { width: (parent.width - 8)/2; spacing: 5
                Text { text: "Shift / Caps Lock"; color: uiTheme.themeColors.text }
                EditorField { id: shiftInput; field: 5; objectName: "layoutShift"; width: parent.width }
            }
            Column { width: (parent.width - 8)/2; spacing: 5
                Text { text: "AltGr"; color: uiTheme.themeColors.text }
                EditorField { id: altgrInput; field: 6; objectName: "layoutAltGr"; width: parent.width }
            }
            Column { width: (parent.width - 8)/2; spacing: 5
                Text { text: "Shift + AltGr"; color: uiTheme.themeColors.text }
                EditorField { id: bothInput; field: 7; objectName: "layoutShiftAltGr"; width: parent.width }
            }
        }
        }
    }
    Text { visible: !root.typing; x: 0; y: parent.height - 80; width: parent.width; text: root.error || qsTranslate("NGS", "Select a key in the layout. Changes are saved only when you choose Save."); color: root.error ? uiTheme.themeColors.warning : uiTheme.themeColors.muted; wrapMode: Text.WordWrap; font.pixelSize: 13 }
    Row {
        visible: !root.typing
        y: parent.height - 48; spacing: 10
        FluidButton { id: copyLayout; visible: !!root.draft.id; text: qsTranslate("NGS", "Save a copy"); highlighted: root.selectedField === 16; onClicked: { root.flush(); root.draft = {id:"", name:root.draft.name.slice(0,53) + qsTranslate("NGS", " copy"), rows:root.draft.rows}; nameInput.text = root.draft.name; root.save() } }
        FluidButton { id: saveButton; objectName: "layoutSave"; text: qsTranslate("NGS", "Save and enable"); highlighted: root.selectedField === 17; onClicked: root.save() }
        FluidButton { id: cancelButton; text: qsTranslate("NGS", "Cancel", "Отмена"); highlighted: root.selectedField === 18; onClicked: root.close() }
    }
}
