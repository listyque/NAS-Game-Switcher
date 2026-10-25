import QtQuick
import QtQuick.Controls.Basic
import "FocusNavigation.js" as Navigation

ToolPage {
    id: root
    objectName: "padKeyboardEditor"
    required property QtObject bridge
    property real uiScale: 1
    readonly property var keyboard: bridge.keyboard
    property int selectedField: 0
    property int padIndex: 0
    property string keyName: "A"
    property var modifiers: []
    property int presetIndex: 0
    property int presetAction: 0
    readonly property var presets: keyboard.padPresets
    readonly property var chosenPreset: presets[presetIndex]
    readonly property var keyNames: keyboard.keyNames
    readonly property var rows: keyboard.padRows
    readonly property string assignment: modifiers.concat(keyName ? [keyName] : []).join("+")
    pageScale: uiScale

    onAboutToShow: { selectedField = 0; loadAssignment() }
    onAboutToHide: { presetPicker.close(); keyboard.finishEditor() }
    Connections { target: root.keyboard; function onChordPicked(value) { const parts = value.split("+"); root.keyName = parts.pop(); root.modifiers = parts } }
    function loadAssignment() {
        const parts = (rows[padIndex].keys || "A").split("+")
        keyName = parts.pop(); modifiers = parts
    }
    function selectPad(button, edit) {
        padIndex = rows.findIndex(row => row.button === button)
        loadAssignment()
        if (edit) selectedField = 1
    }
    function toggleModifier(name) { modifiers = modifiers.indexOf(name) >= 0 ? modifiers.filter(item => item !== name) : modifiers.concat([name]) }
    function keyLabel(key) {
        const names = {Esc:qsTranslate("NGS", "Esc — cancel"), Tab:qsTranslate("NGS", "Tab — next field"), Backspace:qsTranslate("NGS", "Backspace — delete left"), Enter:qsTranslate("NGS", "Enter — confirm"), Space:qsTranslate("NGS", "Space"),
            CapsLock:qsTranslate("NGS", "Caps Lock — capital letters"), NumLock:qsTranslate("NGS", "Num Lock — numeric keypad"), Left:qsTranslate("NGS", "← Left"), Up:qsTranslate("NGS", "↑ Up"), Right:qsTranslate("NGS", "→ Right"), Down:qsTranslate("NGS", "↓ Down"),
            Home:qsTranslate("NGS", "Home — start"), End:qsTranslate("NGS", "End — end"), PageUp:qsTranslate("NGS", "Page Up — previous page"), PageDown:qsTranslate("NGS", "Page Down — next page"), Insert:qsTranslate("NGS", "Insert — insert"), Delete:qsTranslate("NGS", "Delete — delete"),
            Backtick:qsTranslate("NGS", "Backtick / Ё"), Minus:qsTranslate("NGS", "− Minus"), Equal:qsTranslate("NGS", "= Equals"), LeftBracket:qsTranslate("NGS", "[ Left bracket"), RightBracket:qsTranslate("NGS", "] Right bracket"), Backslash:qsTranslate("NGS", "\\ Backslash"), Semicolon:qsTranslate("NGS", "; Semicolon"), Quote:qsTranslate("NGS", "' Quote"), Comma:qsTranslate("NGS", ", Comma"), Period:qsTranslate("NGS", ". Period"), Slash:qsTranslate("NGS", "/ Slash"),
            Add:"Num +", Subtract:"Num −", Multiply:"Num ×", Divide:"Num /", Decimal:"Num .", NumEnter:"Num Enter"}
        return names[key] || key
    }
    function save() { if (keyboard.savePad(rows[padIndex].button, assignment)) loadAssignment() }
    function openPresets() { presetAction = 0; presetPicker.open() }
    function applyPreset() { if (keyboard.applyPadPreset(chosenPreset.id)) { loadAssignment(); presetPicker.close() } }
    function fields() { return [diagram, keyChoice, modifierButtons.itemAt(0), modifierButtons.itemAt(1), modifierButtons.itemAt(2), modifierButtons.itemAt(3), saveButton, clearButton, modeButton, backButton, pickButton, presetsButton] }
    readonly property string fieldHelp: {
        if (selectedField === 0) return qsTranslate("NGS", "L3/R3 — stick presses, LT/RT — triggers. A dot marks an assigned button. ") + bridge.inputHints.next_control + qsTranslate("NGS", " — move to the fields.")
        if (selectedField === 1) return qsTranslate("NGS", "Main key: use Left / Right to choose. Enable Ctrl, Shift, Alt or Win below to create a shortcut.")
        if (selectedField === 10) return qsTranslate("NGS", "Opens the on-screen keyboard to choose a shortcut, such as Ctrl+Shift+A. It will not be sent to the game now.")
        if (selectedField >= 2 && selectedField <= 5) return qsTranslate("NGS", "Highlighted modifiers are held with the main key. You can enable several, such as Ctrl+Shift+A.")
        if (selectedField === 6) return qsTranslate("NGS", "Applies the binding only to the selected button. Changes are not saved until you apply them.")
        if (selectedField === 7) return qsTranslate("NGS", "Removes only the selected button's binding. Other bindings are preserved.")
        if (selectedField === 11) return qsTranslate("NGS", "Ready-made WASD, arrow-key and racing layouts. Preview all buttons before replacing your bindings.")
        if (selectedField === 8) return qsTranslate("NGS", "Sends keyboard input in games, even while the keyboard is hidden. Normal NGS navigation is preserved.")
        return qsTranslate("NGS", "Closes the editor. Applied bindings are saved; the mode turns off when NGS restarts.")
    }
    function reveal(item) {
        const y = item.mapToItem(form, 0, 0).y
        if (y < formScroll.contentY) formScroll.contentY = y
        else if (y + item.height > formScroll.contentY + formScroll.height) formScroll.contentY = y + item.height - formScroll.height
    }
    onSelectedFieldChanged: if (selectedField === 0 || selectedField === 1) formScroll.contentY = 0
    function handleAction(action) {
        if (presetPicker.visible && presetPicker.enabled) {
            if (action === "back" || action === "menu") presetPicker.close()
            else if (action === "previous_control" || action === "next_control") presetAction = (presetAction + (action === "next_control" ? 1 : 2)) % 3
            else if (presetAction === 0 && (action === "left" || action === "right")) presetIndex = (presetIndex + (action === "right" ? 1 : presets.length - 1)) % presets.length
            else if (action === "up") presetAction = 0
            else if (action === "down") presetAction = 1
            else if (action === "left" || action === "right") presetAction = presetAction === 1 ? 2 : 1
            else if (action === "activate") { if (presetAction === 0) presetAction = 1; else if (presetAction === 1) applyPreset(); else presetPicker.close() }
            return
        }
        if (action === "back" || action === "menu") { close(); return }
        if (action === "previous_section" || action === "next_section") {
            padIndex = (padIndex + (action === "next_section" ? 1 : rows.length - 1)) % rows.length; loadAssignment(); return
        }
        if (selectedField === 0 && ["up", "down", "left", "right"].indexOf(action) >= 0) {
            diagram.move(action === "right" ? 1 : action === "left" ? -1 : 0, action === "down" ? 1 : action === "up" ? -1 : 0)
            return
        }
        const order = [0, 1, 10, 2, 3, 4, 5, 6, 7, 8, 11, 9]
        if (action === "previous_control" || action === "next_control") selectedField = order[(order.indexOf(selectedField) + (action === "next_control" ? 1 : order.length - 1)) % order.length]
        else if (action === "up" || action === "down") selectedField = Navigation.next(fields(), selectedField, action, contentItem)
        else if (action === "left" || action === "right") {
            const delta = action === "right" ? 1 : -1
            if (selectedField === 0) { padIndex = (padIndex + delta + rows.length) % rows.length; loadAssignment() }
            else if (selectedField === 1) keyName = keyNames[(Math.max(0, keyNames.indexOf(keyName)) + delta + keyNames.length) % keyNames.length]
            else selectedField = Navigation.next(fields(), selectedField, action, contentItem)
        } else if (action === "activate") {
            if (selectedField === 0) selectedField = 1
            else if (selectedField === 1) keyboard.pickKeys()
            else if (selectedField <= 5) toggleModifier(["Ctrl", "Shift", "Alt", "Win"][selectedField - 2])
            else if (selectedField === 6) save()
            else if (selectedField === 7) { keyboard.savePad(rows[padIndex].button, ""); loadAssignment() }
            else if (selectedField === 8) keyboard.setPadEnabled(!keyboard.padEnabled)
            else if (selectedField === 10) keyboard.pickKeys()
            else if (selectedField === 11) openPresets()
            else close()
        }
    }
    component EditorButton: FluidButton {
        property int field: -1
        highlighted: root.selectedField === field
        onHighlightedChanged: if (highlighted && root.visible && field !== 8 && field !== 9 && field !== 11) Qt.callLater(root.reveal, this)
        height: Math.max(48, 50 * root.uiScale); focusPolicy: Qt.NoFocus; font.pixelSize: 14 * root.uiScale
        background: FluidSurface { radius: 16; color: parent.highlighted || parent.checked ? uiTheme.themeColors.soft : uiTheme.themeColors.button; border.width: 0; shadowEnabled: false }
        contentItem: Text { text: parent.text; font: parent.font; color: uiTheme.themeColors.text; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
    }
    contentItem: Column {
        opacity: presetPicker.visible ? 0 : 1
        spacing: 12 * root.uiScale
        Text { text: qsTranslate("NGS", "Gamepad as keyboard"); color: uiTheme.themeColors.text; font.pixelSize: 32 * root.uiScale; font.weight: Font.Light }
        Text { width: parent.width; text: qsTranslate("NGS", "1. Select a button in the diagram.  2. Choose a key or shortcut.  3. Choose Apply to button. Or start with a preset."); color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale; wrapMode: Text.WordWrap }
        Row {
            width: parent.width; height: parent.height - y - footer.height - explanation.height - 24 * root.uiScale; spacing: 20 * root.uiScale
            Column {
                width: parent.width * .57; height: parent.height; spacing: 8 * root.uiScale
                GamepadDiagram {
                    id: diagram; objectName: "padKeyboardDiagram"
                    width: parent.width; height: parent.height - selectionLabel.height - diagramHint.height - 16 * root.uiScale
                    rows: root.rows; selectedButton: root.rows[root.padIndex].button; navigating: root.selectedField === 0
                    onChosen: function(button) { root.selectPad(button, true) }
                    onNavigated: function(button) { root.selectPad(button, false) }
                }
                Text { id: selectionLabel; width: parent.width; text: diagram.buttonName(root.rows[root.padIndex].button) + qsTranslate("NGS", "\nCurrent: ") + (root.rows[root.padIndex].keys || qsTranslate("NGS", "not assigned")); color: uiTheme.themeColors.text; font.pixelSize: 18 * root.uiScale; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap }
                Text { id: diagramHint; width: parent.width; text: root.bridge.inputHints.activate + qsTranslate("NGS", " — edit · ") + root.bridge.inputHints.next_control + qsTranslate("NGS", " — to fields"); color: uiTheme.themeColors.muted; font.pixelSize: 14 * root.uiScale; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap }
            }
            Flickable {
                id: formScroll; width: parent.width - parent.spacing - parent.width * .57; height: parent.height
                clip: true; contentHeight: form.height; boundsBehavior: Flickable.StopAtBounds
                Column {
                    id: form; width: parent.width; spacing: 12 * root.uiScale
                    Text { text: qsTranslate("NGS", "Keyboard key"); color: uiTheme.themeColors.text; font.pixelSize: 20 * root.uiScale }
                    FluidComboBox {
                        id: keyChoice
                        objectName: "padKeyChoice"; width: parent.width; height: 50 * root.uiScale
                        model: root.keyNames.map(key => ({key: key, label: root.keyLabel(key)})); textRole: "label"; valueRole: "key"
                        currentIndex: root.keyNames.indexOf(root.keyName); focusPolicy: Qt.NoFocus
                        onActivated: function(index) { root.keyName = root.keyNames[index] }
                        background: FluidSurface { color: root.selectedField === 1 ? uiTheme.themeColors.soft : uiTheme.themeColors.button; radius: 16; border.width: 0 }
                    }
                    EditorButton { id: pickButton; width: parent.width; text: qsTranslate("NGS", "Pick a shortcut on the keyboard"); field: 10; onClicked: root.keyboard.pickKeys() }
                    Text { text: qsTranslate("NGS", "Hold with the key"); color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale }
                    Row {
                        spacing: 6 * root.uiScale
                        Repeater { id: modifierButtons; model: ["Ctrl", "Shift", "Alt", "Win"]; EditorButton { required property string modelData; required property int index; width: (form.width - 18 * root.uiScale) / 4; text: modelData; field: 2 + index; checked: root.modifiers.indexOf(modelData) >= 0; onClicked: root.toggleModifier(modelData) } }
                    }
                    Text { objectName: "padAssignmentPreview"; width: parent.width; text: root.rows[root.padIndex].name + "  →  " + root.assignment; color: uiTheme.themeColors.accent; font.pixelSize: 23 * root.uiScale; wrapMode: Text.WordWrap }
                    EditorButton { id: saveButton; objectName: "savePadAssignment"; width: parent.width; text: qsTranslate("NGS", "Apply to button ") + root.rows[root.padIndex].name; field: 6; onClicked: root.save() }
                    EditorButton { id: clearButton; width: parent.width; text: qsTranslate("NGS", "Remove this button's binding"); field: 7; onClicked: { root.keyboard.savePad(root.rows[root.padIndex].button, ""); root.loadAssignment() } }
                    Text { width: parent.width; text: root.keyboard.status; visible: text.length > 0; wrapMode: Text.WordWrap; color: uiTheme.themeColors.warning; font.pixelSize: 14 * root.uiScale }
                }
            }
        }
        Row {
            id: footer; spacing: 12 * root.uiScale
            EditorButton { id: modeButton; objectName: "togglePadKeyboard"; text: root.keyboard.padEnabled ? qsTranslate("NGS", "Gamepad keyboard input: on") : qsTranslate("NGS", "Gamepad keyboard input: off"); field: 8; onClicked: root.keyboard.setPadEnabled(!root.keyboard.padEnabled) }
            EditorButton { id: presetsButton; objectName: "padPresetsButton"; text: qsTranslate("NGS", "Presets"); field: 11; onClicked: root.openPresets() }
            EditorButton { id: backButton; text: qsTranslate("NGS", "Back · ") + root.bridge.inputHints.back; field: 9; onClicked: root.close() }
        }
        Text { id: explanation; objectName: "padEditorHelp"; width: parent.width; text: root.fieldHelp; color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale; wrapMode: Text.WordWrap }
    }
    ToolPage {
        id: presetPicker; objectName: "padPresetPicker"
        parent: root.parent; pageScale: root.uiScale
        contentItem: Column {
            spacing: 14 * root.uiScale
            Text { text: qsTranslate("NGS", "Presets"); color: uiTheme.themeColors.text; font.pixelSize: 26 * root.uiScale }
            Row {
                width: parent.width; spacing: 8 * root.uiScale
                Repeater {
                    model: root.presets
                    FluidButton {
                        required property var modelData; required property int index
                        objectName: "padPreset-" + modelData.id
                        width: (parent.width - parent.spacing * 2) / 3; height: 54 * root.uiScale
                        text: modelData.name; font.pixelSize: 15 * root.uiScale
                        highlighted: root.presetAction === 0 && root.presetIndex === index
                        onClicked: root.presetIndex = index
                    }
                }
            }
            Text { width: parent.width; text: root.chosenPreset.description; color: uiTheme.themeColors.muted; font.pixelSize: 17 * root.uiScale; wrapMode: Text.WordWrap }
            Flow {
                width: parent.width; spacing: 8 * root.uiScale
                Repeater {
                    model: root.chosenPreset.rows
                    Rectangle {
                        required property var modelData
                        width: bindingLabel.implicitWidth + 20 * root.uiScale; height: 34 * root.uiScale; radius: 7; color: uiTheme.themeColors.button
                        Text { id: bindingLabel; anchors.centerIn: parent; text: modelData.name + " → " + modelData.keys; color: uiTheme.themeColors.text; font.pixelSize: 15 * root.uiScale }
                    }
                }
            }
            Text { width: parent.width; text: qsTranslate("NGS", "Replaces all current bindings. The mode stays off; enable it separately after applying the preset."); color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale; wrapMode: Text.WordWrap }
            Row {
                width: parent.width; spacing: 12 * root.uiScale
                FluidButton { objectName: "applyPadPreset"; text: qsTranslate("NGS", "Replace bindings with preset"); width: (parent.width - parent.spacing) * .65; height: 52 * root.uiScale; highlighted: root.presetAction === 1; onClicked: root.applyPreset() }
                FluidButton { objectName: "cancelPadPreset"; text: qsTranslate("NGS", "Cancel", "Отмена"); width: (parent.width - parent.spacing) * .35; height: 52 * root.uiScale; highlighted: root.presetAction === 2; onClicked: presetPicker.close() }
            }
        }
    }
}
