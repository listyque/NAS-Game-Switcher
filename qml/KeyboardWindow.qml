import QtQuick
import QtQuick.Controls.Basic

Window {
    id: root
    objectName: "virtualKeyboardWindow"
    required property QtObject keyboard
    required property var hints
    property real uiScale: 1
    readonly property var area: keyboard.screenArea
    readonly property var inputColors: keyboard.palette
    readonly property real gap: 5
    readonly property real inset: 10
    readonly property string viewMode: keyboard.options.view
    readonly property bool russianLayout: keyboard.layout === "RU"
    readonly property int panelHeight: Math.round(area.height * (heightSlider.pressed ? heightSlider.value : keyboard.options.height_percent) / 100)
    readonly property real toolbarHeight: customMode ? 0 : 56
    readonly property real keySide: (panelHeight - inset * 2 - toolbarHeight - (rows.length - 1) * gap) / rows.length
    readonly property bool desktopLayout: viewMode === "full" && !customMode
    readonly property bool splitLayout: viewMode === "split" && !customMode
    readonly property bool smallLayout: viewMode === "small" && !customMode
    readonly property bool mobileLayout: !customMode && ["touch","small","split","gamepad"].indexOf(viewMode) >= 0
    readonly property bool traditionalLayout: viewMode === "traditional" && !customMode
    readonly property real keysWidth: (mobileLayout && !smallLayout && !splitLayout ? .84 : .98) * (width - inset * 2)
    readonly property real splitHalfWidth: Math.min(keysWidth * .34, area.height * .5)
    readonly property var toolKeys: customMode ? [] : ["@settings", "@view", "@touchpads"].concat(desktopLayout ? [] : ["PrintScreen", "@language"], ["@hide"])
    width: smallLayout ? Math.min(area.width - 20, Math.max(360, Math.min(area.width * .55, area.height * .75))) : area.width
    height: panelHeight
    property real floatX: 0
    property real floatY: -20
    x: smallLayout ? Math.max(area.x, Math.min(area.x + area.width - width, area.x + (area.width - width) / 2 + floatX)) : area.x
    y: area.y + area.height - panelHeight * reveal + (smallLayout ? Math.max(panelHeight - area.height, Math.min(0, floatY)) : 0)
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
    transientParent: null
    readonly property bool presenting: !keyboard.remoteHost && keyboard.visible && !keyboard.touchpadMode
    visible: !keyboard.remoteHost && !keyboard.screenshotPending && !keyboard.touchpadMode && presenting
    color: "transparent"
    property real reveal: presenting ? 1 : 0
    Behavior on reveal { enabled: !root.keyboard.touchpadMode && !root.keyboard.screenshotPending; NumberAnimation { id: slideAnimation; duration: 260; easing.type: Easing.OutCubic } }
    readonly property real contentOpacity: opacitySlider.pressed ? opacitySlider.value : keyboard.options.keyboard_opacity
    property int selectedRow: 0
    property int selectedColumn: 0
    property bool symbols: false
    property bool functionKeys: false
    property bool menuOpen: false
    property int menuPage: 0
    property int menuIndex: 0
    property bool macroMenu: false
    property bool padThemePage: false
    property int macroIndex: 0
    onVisibleChanged: { menuOpen = false; macroMenu = false }
    onPresentingChanged: if (!presenting) { menuOpen = false; macroMenu = false }
    readonly property var touchRows: [
        ["Esc", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"].concat(russianLayout ? ["LeftBracket", "RightBracket"] : [], ["Backspace"]),
        ["Tab", "A", "S", "D", "F", "G", "H", "J", "K", "L"].concat(russianLayout ? ["Semicolon", "Quote"] : ["Quote"], ["Enter"]),
        ["Shift", "Z", "X", "C", "V", "B", "N", "M", "Comma", "Period"].concat(russianLayout ? ["Backtick"] : [], ["Slash", "RightShift"]),
        ["@symbols", "Ctrl", "Win", "Alt", "Space"].concat(splitLayout ? ["Space"] : [], ["@voice", "Left", "Right"])]
    readonly property var symbolRows: [
        ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "Backspace"],
        ["Backtick", "Minus", "Equal", "LeftBracket", "RightBracket", "Backslash", "Semicolon", "Quote", "Slash", "Enter"],
        ["Shift", "Esc", "Tab", "CapsLock", "Insert", "Delete", "Home", "End", "PageUp", "PageDown"],
        ["@letters", "Ctrl", "Win", "Alt", "Space"].concat(splitLayout ? ["Space"] : [], ["@voice", "Left", "Right"])]
    readonly property var fullRows: [
        (functionKeys ? ["Esc", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "Backspace"] : ["Esc", "Backtick", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "Minus", "Equal", "Backspace"]).concat(["Home", "PageUp"]),
        ["Tab", "Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P", "LeftBracket", "RightBracket", "Backslash", "Delete", "End", "PageDown"],
        ["CapsLock", "A", "S", "D", "F", "G", "H", "J", "K", "L", "Semicolon", "Quote", "Enter", "Insert", "Pause"],
        ["Shift", "Z", "X", "C", "V", "B", "N", "M", "Comma", "Period", "Slash", "Up", "RightShift", "PrintScreen", "ScrollLock"],
        ["@fn", "Ctrl", "Win", "Alt", "@voice", "Space", "RightAlt", "RightCtrl", "Left", "Down", "Right", "Menu", "@language", "@symbols"]]
    readonly property var numRows: [
        ["Num7", "Num8", "Num9", "Divide", "NumLock", "Home", "Up", "End", "Backspace"],
        ["Num4", "Num5", "Num6", "Multiply", "Shift", "Left", "Down", "Right", "PageUp"],
        ["Num1", "Num2", "Num3", "Subtract", "Ctrl", "Insert", "Delete", "NumEnter", "PageDown"],
        ["Num0", "Decimal", "Add", "@letters", "Win", "Alt", "@voice", "Esc", "Tab"]]
    readonly property var custom: keyboard.customLayout
    readonly property bool customMode: !!custom.rows && !symbols
    readonly property var traditionalRows: fullRows.map(row => row.slice(0,-2))
    readonly property var navigationRows: [["Tab","Enter","Space","Left","Right","Up","Down","PageUp","PageDown","F6","Esc","Ctrl","Win","Alt","@voice","@letters"]]
    readonly property var rows: customMode ? custom.rows.map(row => row.map(item => item.key)) : desktopLayout ? fullRows : traditionalLayout ? traditionalRows : viewMode === "navigation" ? navigationRows : viewMode === "numpad" ? numRows : symbols ? symbolRows : touchRows
    readonly property var labels: ({Backtick: "`", Minus: "−", Equal: "=", Backspace: "⌫", Tab: "Tab", CapsLock: "Caps",
        LeftBracket: "[", RightBracket: "]", Backslash: "\\", Semicolon: ";", Quote: "'", Comma: ",", Period: ".", Slash: "/",
        Space: "", Shift: "⇧", Delete: "Del", Left: "‹", Right: "›", Up: "↑", Down: "↓", NumLock: "Num", Divide: "/", Multiply: "×",
        Subtract: "−", Add: "+", Decimal: ".", NumEnter: "Enter", PageUp: "PgUp", PageDown: "PgDn", PrintScreen: "PrtSc", ScrollLock:"ScrLk", RightShift:"⇧", RightCtrl:"Ctrl", RightAlt:"Alt", Win:"⊞", Menu:"☰",
        "@fn":"Fn", "@voice":"", "@view": (keyboard.views.find(view => view.id === viewMode) || {}).name || qsTranslate("NGS", "Layout"), "@symbols": "&123", "@settings": "⚙", "@touchpads": qsTranslate("NGS", "Touchpads"), "@hide": "", "@language": russianLayout ? "РУС" : keyboard.layout === "?" ? qsTranslate("NGS", "Language") : keyboard.layout})
    readonly property var russian: ({Q:"Й", W:"Ц", E:"У", R:"К", T:"Е", Y:"Н", U:"Г", I:"Ш", O:"Щ", P:"З", LeftBracket:"Х", RightBracket:"Ъ",
        A:"Ф", S:"Ы", D:"В", F:"А", G:"П", H:"Р", J:"О", K:"Л", L:"Д", Semicolon:"Ж", Quote:"Э", Z:"Я", X:"Ч", C:"С", V:"М", B:"И", N:"Т", M:"Ь", Comma:"Б", Period:"Ю", Backtick:"Ё"})
    function keyLabel(key, shiftState) {
        const shifted = shiftState === undefined ? keyboard.held.indexOf("Shift") >= 0 : shiftState
        const upper = shifted !== (shiftState === undefined && keyboard.held.indexOf("CapsLock") >= 0)
        if (russianLayout && russian[key]) return upper ? russian[key] : russian[key].toLowerCase()
        if (/^[A-Z]$/.test(key)) return upper ? key : key.toLowerCase()
        if (key === "@letters") return custom.rows ? qsTranslate("NGS", "Letters") : russianLayout ? "АБВ" : "ABC"
        if (key === "Enter" && mobileLayout) return "↵"
        if (key.indexOf("Num") === 0 && key.length === 4) return key.slice(3)
        if (russianLayout && key === "Slash") return shifted ? "," : "."
        if (shifted && viewMode !== "numpad") {
            const punctuation = russianLayout ? {"1":"!", "2":"\"", "3":"№", "4":";", "5":"%", "6":":", "7":"?", "8":"*", "9":"(", "0":")"}
                : {"1":"!", "2":"@", "3":"#", "4":"$", "5":"%", "6":"^", "7":"&", "8":"*", "9":"(", "0":")", Minus:"_", Equal:"+", LeftBracket:"{", RightBracket:"}", Backslash:"|", Semicolon:":", Quote:"\"", Comma:"<", Period:">", Slash:"?", Backtick:"~"}
            if (punctuation[key]) return punctuation[key]
        }
        return key === "@language" && customMode ? qsTranslate("NGS", "Layouts") : labels[key] === undefined ? key : labels[key]
    }
    function keyWeight(key) {
        if (mobileLayout) {
            if (key === "Space") return 3
            if (key === "Backspace") return splitLayout ? 1.5 : 2
            if (key === "Tab") return splitLayout ? 1.75 : russianLayout ? 2.5 : 1.5
            if (key === "Enter") return splitLayout ? 1.75 : 1.5
            if (key === "Shift") return splitLayout ? 2 : russianLayout ? 3 : 2
            if (key === "RightShift") return splitLayout ? 2 : 1
            if (key === "Esc") return splitLayout ? 1.5 : 1
            return 1
        }
        if (viewMode === "navigation") return key === "Space" ? 1.5 : 1
        if (key === "Space") return desktopLayout || traditionalLayout ? 4 : 5
        if (key === "Num0") return 2
        if (key === "Backspace") return desktopLayout || traditionalLayout ? 1.75 : 1.65
        if (key === "Enter") return desktopLayout || traditionalLayout ? 2.2 : 1.5
        if (key === "CapsLock") return desktopLayout || traditionalLayout ? 2 : 1.3
        if (key === "Shift" || key === "RightShift") return desktopLayout || traditionalLayout ? 2.3 : 1.15
        if (key === "Tab") return 1.5
        if (["@symbols", "@letters", "@language", "@touchpads"].indexOf(key) >= 0) return 1.3
        return 1
    }
    function keyHint(key) {
        if (mobileLayout && !symbols) {
            const i = ["Q","W","E","R","T","Y","U","I","O","P"].indexOf(key)
            if (i >= 0) return String((i + 1) % 10)
        }
        return ""
    }
    function customLabel(item) {
        const base = item.states.base === undefined ? (/^[A-Z]$/.test(item.key) ? item.key : "") : item.states.base
        const shift = (keyboard.held.indexOf("Shift") >= 0) !== (keyboard.held.indexOf("CapsLock") >= 0 && base.toUpperCase() !== base.toLowerCase())
        const altgr = keyboard.held.indexOf("AltGr") >= 0
        const layer = altgr ? (shift ? "shift_altgr" : "altgr") : shift ? "shift" : "base"
        return item.states[layer] === undefined ? (item.states.base === undefined ? keyLabel(item.key) : item.states.base) : item.states[layer]
    }
    function rowWidth(row, index) { return customMode && custom.rows[index] ? custom.rows[index].reduce((sum, item) => sum + item.width, 0) : row.reduce((sum,key,i) => sum + (desktopLayout && i >= row.length - 2 ? 1 : keyWeight(key)),0) }
    function rowOffset(row, index) {
        if (customMode || desktopLayout || mobileLayout || traditionalLayout || viewMode === "navigation" || row.indexOf("Space") >= 0) return 0
        return Math.max(0,(keysWidth - rowWidth(row,index) * keyUnit - (row.length - 1) * gap) / 2)
    }
    function splitAt(index) { return index === 3 ? 5 : 6 }
    function groupWidth(row) { return row.reduce((sum,key) => sum + keyWeight(key) * (keyUnit + gap), 0) - gap }
    readonly property real keyUnit: mobileLayout ? Math.min(...rows.map((row,index) => (splitLayout
        ? (splitHalfWidth + gap) / Math.max(row.slice(0,splitAt(index)).reduce((sum,key) => sum + keyWeight(key),0), row.slice(splitAt(index)).reduce((sum,key) => sum + keyWeight(key),0))
        : (keysWidth + gap) / rowWidth(row,index)) - gap)) : Math.min(...rows.map((row,index) => desktopLayout
        ? (keysWidth - 16 - (row.length - 2) * gap) / rowWidth(row,index)
        : (keysWidth - (row.length - 1) * gap) / rowWidth(row,index)))
    function keyWidth(row, index, column) {
        if (customMode && custom.rows[index] && custom.rows[index][column]) return custom.rows[index][column].width * keyUnit
        if (mobileLayout) {
            const group = splitLayout ? (column < splitAt(index) ? row.slice(0,splitAt(index)) : row.slice(splitAt(index))) : row
            const pitch = symbols && !splitLayout ? (keysWidth + gap) / rowWidth(row,index) : keyUnit + gap
            return keyWeight(row[column]) * pitch - gap + (row[column] === "Space" && !(symbols && !splitLayout) ? Math.max(0,(splitLayout ? splitHalfWidth : keysWidth) - groupWidth(group)) : 0)
        }
        if (desktopLayout && column >= row.length - 2) return keyUnit
        if (desktopLayout) {
            const mainWidth = keysWidth - 2 * keyUnit - gap - 16
            return keyWeight(row[column]) * (mainWidth - (row.length - 3) * gap) / (rowWidth(row,index) - 2)
        }
        if (traditionalLayout || viewMode === "navigation") return keyWeight(row[column]) * (keysWidth - (row.length - 1) * gap) / rowWidth(row,index)
        const extra = Math.max(0,keysWidth - (desktopLayout ? 16 - gap : 0) - (row.length - 1) * gap - rowWidth(row,index) * keyUnit)
        return keyWeight(row[column]) * keyUnit + (row[column] === "Space" ? extra : 0)
    }
    function keyLeft(row, index, column) {
        if (splitLayout) {
            const start = column < splitAt(index) ? 0 : splitAt(index)
            const group = row.slice(start, start ? row.length : splitAt(index))
            const offset = start ? keysWidth - (group.indexOf("Space") >= 0 ? splitHalfWidth : groupWidth(group)) : 0
            return offset + row.slice(start,column).reduce((sum,key,i) => sum + keyWidth(row,index,start+i) + gap,0)
        }
        if (desktopLayout && column >= row.length - 2) return keysWidth - (row.length - column) * (keyUnit + gap) + gap
        return rowOffset(row,index) + row.slice(0,column).reduce((sum,key,i) => sum + keyWidth(row,index,i) + gap,0)
    }
    function keyCenter(row, column) {
        if (row === toolKeys) return toolLeft(column) + toolWidth(toolKeys[column]) / 2 - (root.width - keysWidth) / 2
        const index = rows.indexOf(row)
        return keyLeft(row,index,column) + keyWidth(row,index,column) / 2
    }
    function toolWidth(key) {
        if (smallLayout) return (root.width - inset * 2 - (toolKeys.length - 1) * 4) / toolKeys.length
        if (key === "@settings" || key === "@hide") return 52
        const available = root.width - inset * 2 - 104 - (toolKeys.length - 1) * 4
        const total = desktopLayout ? 236 : 380
        return Math.max(48, (key === "@view" ? 124 : key === "@touchpads" ? 112 : key === "PrintScreen" ? 80 : 64) * Math.min(1,available / total))
    }
    function toolLeft(index) { return toolKeys[index] === "@hide" ? root.width - inset - toolWidth("@hide") : inset + toolKeys.slice(0,index).reduce((sum,key) => sum + toolWidth(key) + 4,0) }
    function moveRow(delta) {
        const center = keyCenter(selectedRow < 0 ? toolKeys : rows[selectedRow], selectedColumn)
        selectedRow = Math.max(toolKeys.length ? -1 : 0, Math.min(rows.length - 1, selectedRow + delta))
        let nearest = 0, distance = Infinity
        const target = selectedRow < 0 ? toolKeys : rows[selectedRow]
        target.forEach((key, i) => { const d = Math.abs(keyCenter(target, i) - center); if (d < distance) { nearest = i; distance = d } })
        selectedColumn = nearest
    }
    onRowsChanged: { selectedRow = Math.max(0,Math.min(selectedRow, rows.length - 1)); selectedColumn = Math.min(selectedColumn, rows[selectedRow].length - 1) }
    function pressKey(key, row, column) {
        if (key === "@symbols" || key === "@letters") {
            if (!custom.rows && viewMode !== "touch") keyboard.setView("touch")
            symbols = key === "@symbols"; selectedRow = 0; selectedColumn = 0; keyboard.feedback()
        } else if (key === "@settings") toggleMenu()
        else if (key === "@voice") keyboard.voiceInput()
        else if (key === "@fn") { functionKeys = !functionKeys; keyboard.feedback() }
        else if (key === "@view") { toggleMenu() }
        else if (key === "@touchpads") { keyboard.feedback(); keyboard.setTouchpads(true) }
        else if (key === "@hide") keyboard.hide()
        else if (key === "@language") {
            if (customMode) { toggleMenu(); menuPage = 2; menuIndex = keyboard.layouts.findIndex(item => 'custom-' + item.id === viewMode) }
            else keyboard.switchLayout()
        }
        else if (customMode) keyboard.pressCustom(row, column)
        else keyboard.press(key)
    }
    function editCurrentLayout() {
        keyboard.clearModifiers()
        const draft = custom.rows ? JSON.parse(JSON.stringify(custom)) : {id:"", name:russianLayout ? qsTranslate("NGS", "My Russian layout") : qsTranslate("NGS", "My English layout"), rows:rows.map((row, r) => row.map((key, c) => {
            const base = keyLabel(key, false), shift = keyLabel(key, true)
            const printable = /^[A-Z0-9]$/.test(key) || /^Num\d$/.test(key) || ["Backtick", "Minus", "Equal", "LeftBracket", "RightBracket", "Backslash", "Semicolon", "Quote", "Comma", "Period", "Slash", "Decimal", "Add", "Subtract", "Divide", "Multiply"].indexOf(key) >= 0
            const states = printable ? {base:base, shift:shift} : {}
            return {key:key === "@fn" ? "@symbols" : key, width:Math.max(.5, Math.min(8, keyWidth(row, r, c) / keyUnit)), states:states}
        }))}
        if (!custom.rows) draft.rows[draft.rows.length-1] = draft.rows[draft.rows.length-1].concat(toolKeys.filter(key => key !== "@view").map(key => ({key:key,width:1,states:{}})))
        keyboard.editLayout(draft)
    }
    function toggleMenu() { menuPage = 0; menuIndex = 0; macroMenu = false; menuOpen = !menuOpen; keyboard.feedback() }
    readonly property var menuItems: menuPage === 3 ? [
        {id:40,text:qsTranslate("NGS", "Keyboard"),note:qsTranslate("NGS", "Key theme")}, {id:41,text:qsTranslate("NGS", "Touchpads"),note:qsTranslate("NGS", "Separate theme")}
    ].concat(keyboard.themes.map((theme,i) => ({id:200+i,text:theme.name,note:"Aa",palette:theme}))) : menuPage === 2 ? keyboard.layouts.map((item, i) => ({id:100+i, text:item.name, note:qsTranslate("NGS", "Custom layout")})) : menuPage === 0 ? keyboard.views.map(view => ({id:view.menu_id,text:view.name,note:view.note})).concat([
        {id:3, text:qsTranslate("NGS", "Touchpads"), note:qsTranslate("NGS", "Cursor control")}, {id:40, text:qsTranslate("NGS", "Themes"), note:qsTranslate("NGS", "Keyboard and touchpads")},
        {id:19, text:qsTranslate("NGS", "Layout editor", "Редактор раскладок"), note:qsTranslate("NGS", "Keys and layers")}, {id:17, text:qsTranslate("NGS", "More"), note:qsTranslate("NGS", "My layouts and bindings")}])
        : [{id:0, text:qsTranslate("NGS", "Change keyboard language"), note:keyboard.layout}, {id:9, text:qsTranslate("NGS", "Haptic feedback"), note:keyboard.options.haptics ? qsTranslate("NGS", "Enabled") : qsTranslate("NGS", "Disabled")},
           {id:6, text:qsTranslate("NGS", "Release modifiers"), note:"Ctrl / Shift / Alt / Win"}, {id:5, text:qsTranslate("NGS", "Configure gamepad"), note:qsTranslate("NGS", "Bindings and presets")},
           {id:7, text:keyboard.running ? qsTranslate("NGS", "Stop macro") : qsTranslate("NGS", "Macros"), note:keyboard.macros.length + qsTranslate("NGS", " saved")},
           {id:4, text:qsTranslate("NGS", "Gamepad to keys"), note:keyboard.padEnabled ? qsTranslate("NGS", "Enabled") : qsTranslate("NGS", "Disabled")},
           {id:21, text:qsTranslate("NGS", "Auto-show"), note:keyboard.options.auto_show ? qsTranslate("NGS", "When tapping a text field") : qsTranslate("NGS", "Disabled")},
           {id:22, text:qsTranslate("NGS", "Windows keyboard"), note:qsTranslate("NGS", "Disable its auto-show")}, {id:20, text:qsTranslate("NGS", "My layouts"), note:keyboard.layouts.length + qsTranslate("NGS", " saved")}, {id:18, text:qsTranslate("NGS", "Back"), note:qsTranslate("NGS", "View and size")}]
    function menuSelected(id) {
        if (menuPage === 3) return id === 40 ? !padThemePage : id === 41 ? padThemePage : id >= 200 && keyboard.themes[id-200].id === (padThemePage ? keyboard.padPalette.id : root.inputColors.id)
        const view = keyboard.views.find(view => view.menu_id === id)
        return view ? view.id === viewMode : id >= 100 && keyboard.layouts[id - 100] ? 'custom-' + keyboard.layouts[id - 100].id === viewMode : false
    }
    function chooseMenu(id) {
        keyboard.feedback()
        const view = keyboard.views.find(view => view.menu_id === id)
        if (view) { keyboard.setView(view.id); symbols = false; menuOpen = false }
        else if (id === 40 || id === 41) { menuPage = 3; padThemePage = id === 41; menuIndex = padThemePage ? 1 : 0 }
        else if (id >= 200 && keyboard.themes[id-200]) keyboard.setTheme(keyboard.themes[id-200].id,padThemePage)
        else if (id === 17 || id === 18) { menuPage = id === 17 ? 1 : 0; menuIndex = 0 }
        else if (id === 19) editCurrentLayout()
        else if (id === 20 && keyboard.layouts.length) { menuPage = 2; menuIndex = 0 }
        else if (id === 21) keyboard.setAutoShow(!keyboard.options.auto_show)
        else if (id === 22) keyboard.windowsTypingSettings()
        else if (id === 23) { menuOpen = false; keyboard.press("PrintScreen") }
        else if (id >= 100 && keyboard.layouts[id - 100]) { keyboard.setView('custom-' + keyboard.layouts[id - 100].id); menuOpen = false }
        else if (id === 0) keyboard.switchLayout()
        else if (id === 3) { menuOpen = false; keyboard.setTouchpads(true) }
        else if (id === 4) keyboard.setPadEnabled(!keyboard.padEnabled)
        else if (id === 5) { menuOpen = false; keyboard.editPad() }
        else if (id === 6) keyboard.clearModifiers()
        else if (id === 7) {
            if (keyboard.running) keyboard.cancel()
            else if (keyboard.macros.length) { macroMenu = true; macroIndex = 0; menuOpen = false }
        } else if (id === 9) keyboard.setHaptics(!keyboard.options.haptics)
    }
    function handleAction(action) {
        if (macroMenu) {
            if (action === "back" || action === "menu" || !keyboard.macros.length) macroMenu = false
            else if (action === "up" || action === "down") macroIndex = (macroIndex + (action === "down" ? 1 : keyboard.macros.length - 1)) % keyboard.macros.length
            else if (action === "activate") { macroMenu = false; keyboard.runMacro(keyboard.macros[macroIndex].name) }
            return
        }
        if (menuOpen) {
            if (action === "back" || action === "menu") { if (menuPage) { menuPage = 0; menuIndex = 0 } else menuOpen = false }
            else if (menuIndex >= menuItems.length) {
                if (action === "up") menuIndex = Math.floor((menuItems.length - 1) / 3) * 3
                else if (action === "down") menuIndex = menuItems.length
                else if (action === "left" || action === "right") {
                    if (menuIndex === menuItems.length) keyboard.setHeight(Math.max(keyboard.options.height_min, Math.min(keyboard.options.height_max, keyboard.options.height_percent + (action === "right" ? 1 : -1))))
                    else keyboard.setKeyboardOpacity(Math.max(0, Math.min(1, keyboard.options.keyboard_opacity + (action === "right" ? .05 : -.05))))
                } else if (action === "next_control" || action === "previous_control") menuIndex = menuIndex === menuItems.length ? menuItems.length + 1 : menuItems.length
            }
            else if (action === "left" || action === "right") menuIndex = Math.max(Math.floor(menuIndex / 3) * 3, Math.min(menuItems.length - 1, Math.floor(menuIndex / 3) * 3 + 2, menuIndex + (action === "right" ? 1 : -1)))
            else if (action === "up" || action === "down") menuIndex = Math.max(0, Math.min(menuItems.length, menuIndex + (action === "down" ? 3 : -3)))
            else if (action === "next_control" || action === "previous_control") menuIndex = menuItems.length
            else if (action === "activate") chooseMenu(menuItems[menuIndex].id)
            return
        }
        if (action === "back" || action === "menu") { keyboard.hide(); return }
        if (!rows.length) return
        selectedRow = Math.max(toolKeys.length ? -1 : 0, Math.min(rows.length - 1, selectedRow))
        selectedColumn = Math.max(0, Math.min((selectedRow < 0 ? toolKeys : rows[selectedRow]).length - 1, selectedColumn))
        if (action === "previous_control" || action === "next_control") {
            if (toolKeys.length) { selectedRow = -1; selectedColumn = toolKeys.indexOf("@settings") }
            else { selectedRow = rows.findIndex(row => row.indexOf("@settings") >= 0); selectedColumn = rows[selectedRow].indexOf("@settings") }
            return
        }
        if (action === "previous_section" || action === "next_section") {
            const views = keyboard.views.map(view => view.id).concat(keyboard.layouts.map(item => 'custom-' + item.id))
            const index = views.indexOf(viewMode)
            keyboard.setView(views[(index + (action === "next_section" ? 1 : views.length - 1)) % views.length]); symbols = false
        } else if (action === "up" || action === "down") moveRow(action === "down" ? 1 : -1)
        else if (action === "left" || action === "right") selectedColumn = Math.max(0, Math.min((selectedRow < 0 ? toolKeys : rows[selectedRow]).length - 1, selectedColumn + (action === "right" ? 1 : -1)))
        else if (action === "activate") pressKey((selectedRow < 0 ? toolKeys : rows[selectedRow])[selectedColumn], selectedRow, selectedColumn)
        else if (action === "toggle_installed") keyboard.press("Backspace")
        else if (action === "refresh") keyboard.press("Space")
    }
    Connections {
        target: root.keyboard
        function onAction(action) { if (root.presenting) root.handleAction(action) }
        function onChanged() { if (root.keyboard.screenshotPending) slideAnimation.complete() }
    }
    Rectangle { anchors.fill: parent; color: root.inputColors.panel; radius: 8; opacity: root.contentOpacity }
    Item {
        visible: root.toolKeys.length > 0; width: parent.width; height: root.toolbarHeight; y: 2
        Repeater {
            model: root.toolKeys
            FluidButton {
                id: toolKey
                required property string modelData; required property int index
                property bool screenshotHeld: false
                objectName: modelData === "@settings" ? "keyboardViewMenuButton" : "keyboardTool-" + modelData
                inputSource: root.keyboard; focusPolicy: Qt.NoFocus; padding: 0
                x: root.toolLeft(index); width: root.toolWidth(modelData); height: root.toolbarHeight - 2
                text: root.smallLayout && modelData === "@view" ? qsTranslate("NGS", "View") : root.smallLayout && modelData === "@touchpads" ? qsTranslate("NGS", "Touch") : root.keyLabel(modelData); font.pixelSize: 18
                opacity: Math.max(.45,root.contentOpacity)
                highlighted: root.selectedRow === -1 && root.selectedColumn === index && !root.menuOpen
                Accessible.name: modelData === "@settings" ? qsTranslate("NGS", "Keyboard settings") : modelData === "@hide" ? qsTranslate("NGS", "Hide keyboard") : modelData === "@view" ? qsTranslate("NGS", "Change keyboard view") : text
                background: Rectangle { radius: 4; border.width: 0; color: toolKey.pressing ? root.inputColors.pressed : toolKey.hovered || toolKey.highlighted && root.hints.device === "gamepad" ? root.inputColors.hover : "transparent" }
                contentItem: Text { text: ["@settings","@hide"].indexOf(toolKey.modelData) >= 0 ? "" : toolKey.text; font: toolKey.font; fontSizeMode: Text.HorizontalFit; minimumPixelSize: 14; color: root.inputColors.panel_text; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
                KeyboardGlyph { keyName: toolKey.modelData; ink: root.inputColors.panel_text; visible: ["@settings","@hide"].indexOf(keyName) >= 0 }
                Timer { interval: 700; running: toolKey.modelData === "PrintScreen" && toolKey.down; onTriggered: { toolKey.screenshotHeld = true; root.keyboard.takeScreenshot(true) } }
                onPressed: screenshotHeld = false
                onClicked: if (!screenshotHeld) root.pressKey(modelData,-1,index)
            }
        }
        DragHandler {
            enabled: root.smallLayout; target: null
            property point grabOffset
            onActiveChanged: if (active) {
                const p = parent.mapToGlobal(centroid.position)
                grabOffset = Qt.point(root.x - p.x, root.y - p.y)
            }
            onCentroidChanged: if (active) {
                const p = parent.mapToGlobal(centroid.position)
                root.floatX = p.x + grabOffset.x - root.area.x - (root.area.width - root.width) / 2
                root.floatY = p.y + grabOffset.y - root.area.y - root.area.height + root.panelHeight * root.reveal
            }
        }
    }
    Item {
        id: keys; objectName: "keyboardKeys"
        x: (root.width-root.keysWidth)/2; y: root.inset + root.toolbarHeight; width: root.keysWidth; height: root.height - y - root.inset
        Column {
            width: parent.width; spacing: root.gap
            Repeater {
                model: root.rows
                Item {
                    id: keyRow
                    required property var modelData
                    required property int index
                    width: keys.width; height: root.keySide
                    Repeater {
                        model: parent.modelData
                        FluidButton {
                            id: keyControl
                            required property string modelData
                            required property int index
                            property bool screenshotHeld: false
                            onPressed: screenshotHeld = false
                            Timer {
                                interval: keyControl.modelData === "PrintScreen" ? 700 : 500
                                running: (keyControl.modelData === "PrintScreen" || root.keyHint(keyControl.modelData) !== "") && keyControl.down
                                onTriggered: { keyControl.screenshotHeld = true; if (keyControl.modelData === "PrintScreen") root.keyboard.takeScreenshot(true); else root.keyboard.press(root.keyHint(keyControl.modelData)) }
                            }
                            inputSource: root.keyboard
                            opacity: ["@settings", "@hide", "@touchpads"].indexOf(modelData) >= 0 ? Math.max(.45, root.contentOpacity) : root.contentOpacity
                            readonly property bool chosen: root.selectedRow === keyRow.index && root.selectedColumn === index && !root.menuOpen && !root.macroMenu
                            readonly property bool held: root.keyboard.held.indexOf(modelData) >= 0 || modelData === "@fn" && root.functionKeys
                            objectName: modelData === "@settings" ? "keyboardViewMenuButton" : "keyboardKey-" + keyRow.index + "-" + index
                            highlighted: chosen
                            x: root.keyLeft(keyRow.modelData,keyRow.index,index)
                            width: root.keyWidth(keyRow.modelData, keyRow.index, index); height: root.keySide
                            padding: 2; focusPolicy: Qt.NoFocus
                            transform: Scale { origin.x: keyControl.width / 2; origin.y: keyControl.height / 2; xScale: 1 - keyControl.compression * .02; yScale: 1 - keyControl.compression * .07 }
                            text: root.customMode && root.custom.rows[keyRow.index] && root.custom.rows[keyRow.index][index] ? root.customLabel(root.custom.rows[keyRow.index][index]) : root.keyLabel(modelData)
                            Accessible.name: modelData === "Space" ? qsTranslate("NGS", "Space") : modelData === "@voice" ? qsTranslate("NGS", "Windows voice typing") : modelData === "@settings" ? qsTranslate("NGS", "Keyboard settings") : modelData === "@touchpads" ? qsTranslate("NGS", "Open touchpads") : modelData === "PrintScreen" ? qsTranslate("NGS", "Screenshot — Print Screen") : modelData === "@hide" ? qsTranslate("NGS", "Hide keyboard") : modelData === "@language" ? qsTranslate("NGS", "Change keyboard language") : modelData === "@symbols" ? qsTranslate("NGS", "Numbers and symbols") : modelData === "@letters" ? qsTranslate("NGS", "Letters") : modelData
                            autoRepeat: ["Backspace", "Left", "Right", "Up", "Down"].indexOf(modelData) >= 0
                            font.pixelSize: Math.min(modelData.indexOf("@") === 0 || text.length > 2 ? 20 : 34, root.keySide * (text.length > 3 ? .32 : .47))
                            background: KeyboardSurface {
                                inputColors: root.inputColors
                                pressed: keyControl.pressing; held: keyControl.held; hovered: keyControl.hovered
                                selected: keyControl.chosen && root.hints.device === "gamepad"
                            }
                            contentItem: Text {
                                textFormat: Text.PlainText
                                text: ["Backspace", "Shift", "RightShift", "Win", "Menu", "@voice", "@hide", "@settings"].indexOf(keyControl.modelData) >= 0 ? "" : keyControl.text; font: keyControl.font; color: root.inputColors.text
                                horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                                fontSizeMode: Text.HorizontalFit; minimumPixelSize: 9
                                elide: Text.ElideRight
                            }
                            KeyboardGlyph { keyName: keyControl.modelData; ink: root.inputColors.text; locked: root.keyboard.shiftLocked; visible: ["Backspace","Shift","RightShift","Win","Menu","@voice","@hide","@settings"].indexOf(keyName) >= 0 }
                            Text { x: 7; y: 3; text: root.keyHint(keyControl.modelData); visible: text !== "" && root.keySide >= 32; color: root.inputColors.hint; font.pixelSize: Math.min(13,root.keySide*.22) }
                            Text { anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 4; color: root.inputColors.text; font.pixelSize: 11; text: keyControl.modelData === "Backspace" ? "X" : keyControl.modelData === "Space" ? "Y" : keyControl.chosen ? "A" : ""; visible: root.viewMode === "gamepad" && text !== "" }
                            onClicked: if (!screenshotHeld) root.pressKey(modelData, keyRow.index, index)
                        }
                    }
                }
            }
        }
    }
    DragHandler {
        target: null; enabled: !root.smallLayout; acceptedDevices: PointerDevice.TouchScreen; xAxis.enabled: false
        onActiveTranslationChanged: if (active && activeTranslation.y > Math.max(48, root.height * .18)) root.keyboard.hide()
    }
    Window {
        id: settingsWindow
        objectName: "keyboardSettingsWindow"
        width: Math.min(720, root.area.width - 24); height: Math.min(560, root.area.height - 24)
        x: root.area.x + (root.area.width - width) / 2
        y: root.area.y + (root.area.height - height) / 2
        flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        transientParent: null
        visible: root.presenting && (root.menuOpen || root.macroMenu)
        screen: root.screen
        color: "transparent"
        FluidSurface { anchors.fill: parent; color: root.inputColors.panel; border.color: root.inputColors.edge; radius: 24 }
        Text { x: 16; y: 14; text: root.macroMenu ? qsTranslate("NGS", "Macros") : qsTranslate("NGS", "Keyboard"); font.pixelSize: 20; color: root.inputColors.panel_text }
        FluidButton {
            objectName: "keyboardSettingsClose"; x: parent.width - 50; y: 6; width: 40; height: 36; padding: 0
            text: "×"; focusPolicy: Qt.NoFocus; inputSource: root.keyboard
            background: Rectangle { radius: 12; color: parent.hovered ? root.inputColors.hover : "transparent" }
            contentItem: Text { text: parent.text; color: root.inputColors.panel_text; font.pixelSize: 26; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            onClicked: { root.menuOpen = false; root.macroMenu = false }
        }
        Grid {
            id: viewMenu; objectName: "keyboardViewMenu"
            visible: root.menuOpen
            x: 12; y: 50; width: parent.width - 24; height: parent.height - 188; columns: 3; spacing: 6
            Repeater {
                model: root.menuItems
                FluidButton {
                    id: menuButton
                    required property var modelData; required property int index
                    objectName: "keyboardMenu-" + modelData.id; inputSource: root.keyboard
                    width: (viewMenu.width - 12) / 3; height: (viewMenu.height - 6 * (Math.ceil(root.menuItems.length / 3) - 1)) / Math.ceil(root.menuItems.length / 3)
                    focusPolicy: Qt.NoFocus; padding: 5; highlighted: root.menuIndex === index
                    background: Rectangle { radius: 14; color: menuButton.modelData.palette ? menuButton.modelData.palette.panel : menuButton.down ? root.inputColors.pressed : root.menuSelected(menuButton.modelData.id) || menuButton.highlighted || menuButton.hovered ? root.inputColors.hover : root.inputColors.key; border.width: menuButton.highlighted || root.menuSelected(menuButton.modelData.id) ? 2 : 0; border.color: menuButton.modelData.palette ? menuButton.modelData.palette.accent : root.inputColors.accent }
                    contentItem: Column {
                        spacing: 5
                        Text { width: parent.width; text: menuButton.modelData.text; textFormat: Text.PlainText; color: menuButton.modelData.palette ? menuButton.modelData.palette.panel_text : root.inputColors.text; font.pixelSize: 16; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight }
                        Rectangle { visible: !!menuButton.modelData.palette; width: 50; height: visible ? 25 : 0; radius: 4; anchors.horizontalCenter: parent.horizontalCenter; color: menuButton.modelData.palette ? menuButton.modelData.palette.key : "transparent"; Text { anchors.centerIn: parent; text: "Aa"; font.pixelSize: 16; color: menuButton.modelData.palette ? menuButton.modelData.palette.text : "white" } }
                        Text { visible: !menuButton.modelData.palette; width: parent.width; text: menuButton.modelData.note; color: root.inputColors.text; opacity: .75; font.pixelSize: 12; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight }
                    }
                    onClicked: root.chooseMenu(modelData.id)
                }
            }
        }
        Column {
            visible: root.menuOpen; x: 18; y: parent.height - 128; width: parent.width - 36; spacing: 8
            Row {
                width: parent.width; height: 46; spacing: 12
                Text { text: qsTranslate("NGS", "Height ") + Math.round(heightSlider.value) + "%"; width: 140; height: parent.height; color: root.inputColors.panel_text; font.pixelSize: 15; verticalAlignment: Text.AlignVCenter }
                Slider {
                    id: heightSlider; objectName: "keyboardHeightSlider"
                    width: parent.width - 152; height: parent.height; from: root.keyboard.options.height_min; to: root.keyboard.options.height_max; stepSize: 1
                    value: root.keyboard.options.height_percent; focusPolicy: Qt.NoFocus
                    Accessible.name: qsTranslate("NGS", "Keyboard height as a percentage of the screen")
                    onPressedChanged: if (!pressed) root.keyboard.setHeight(value)
                }
            }
            Row {
                id: opacityControls; objectName: "keyboardOpacityControl"
                width: parent.width; height: 46; spacing: 12
                Text { text: qsTranslate("NGS", "Opacity ") + Math.round(opacitySlider.value * 100) + "%"; width: 140; height: parent.height; color: root.inputColors.panel_text; font.pixelSize: 15; verticalAlignment: Text.AlignVCenter }
                Slider {
                    id: opacitySlider; objectName: "keyboardOpacitySlider"
                    width: parent.width - 152; height: parent.height; from: 0; to: 1; stepSize: .05
                    value: root.keyboard.options.keyboard_opacity; focusPolicy: Qt.NoFocus
                    Accessible.name: qsTranslate("NGS", "Keyboard opacity")
                    onPressedChanged: if (!pressed) root.keyboard.setKeyboardOpacity(value)
                }
            }
        }
        ListView {
            visible: root.macroMenu
            x: 12; y: 50; width: parent.width - 24; height: parent.height - 62
            clip: true; model: root.keyboard.macros; currentIndex: root.macroIndex
            delegate: FluidButton {
                required property var modelData; required property int index
                width: ListView.view.width; height: 48; focusPolicy: Qt.NoFocus; inputSource: root.keyboard
                text: modelData.name; highlighted: root.macroIndex === index
                onClicked: { root.macroMenu = false; root.keyboard.runMacro(modelData.name) }
            }
        }
    }
    Rectangle {
        visible: root.keyboard.status.length > 0; opacity: root.contentOpacity; anchors.right: parent.right; anchors.top: parent.top
        width: Math.min(parent.width, statusText.implicitWidth + 20); height: statusText.implicitHeight + 12; radius: 10; color: root.inputColors.panel; z: 4
        Text { id: statusText; anchors.centerIn: parent; text: root.keyboard.status; color: root.inputColors.panel_text; font.pixelSize: 13 }
    }
}
