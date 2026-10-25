import QtQuick
import QtQuick.Controls.Basic

MotionPopup {
    id: root
    objectName: "gameSearchPane"
    required property QtObject bridge
    property real uiScale: 1
    property string query: ""
    property string sectionName: ""
    property int selectedField: 0
    signal submitted(string query)
    x: (parent.width - width) / 2
    y: Math.max(16, (parent.height - height) / 2 - (bridge.keyboard.visible ? Math.min(parent.height * .18, 140) : 0))
    width: Math.min(parent.width - 32, 680 * uiScale)
    height: contentItem.implicitHeight + topPadding + bottomPadding
    padding: 24 * uiScale; modal: true; focus: true; closePolicy: Popup.NoAutoClose
    font.pixelSize: 16 * uiScale

    background: FluidSurface { elevation: 1; shadowBlur: 26; color: uiTheme.themeColors.glass; radius: 26; border.color: uiTheme.themeColors.border }
    Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim }
    onAboutToShow: {
        selectedField = 0; search.text = query; search.forceActiveFocus()
        if (bridge.inputHints.device !== "keyboard") bridge.keyboard.show()
    }
    onAboutToHide: bridge.keyboard.hide()
    function submit(value) { submitted(value.trim()); close() }
    function handleAction(action) {
        if (action === "back" || action === "menu") close()
        else if (["up", "down", "left", "right"].indexOf(action) >= 0) {
            search.focus = false
            selectedField = (selectedField + (action === "up" || action === "left" ? 3 : 1)) % 4
        } else if (action === "activate") {
            if (selectedField === 0) { search.forceActiveFocus(); bridge.keyboard.show() }
            else if (selectedField === 1) submit(search.text)
            else if (selectedField === 2) submit("")
            else close()
        }
    }
    contentItem: Column {
        spacing: 16 * root.uiScale
        Label { text: qsTranslate("NGS", "Search · ", "Поиск · ") + root.sectionName; color: uiTheme.themeColors.text; font.pixelSize: 25 * root.uiScale }
        TextField {
            id: search; objectName: "gameSearchInput"
            width: parent.width; height: 50 * root.uiScale; maximumLength: 160; selectByMouse: true
            placeholderText: qsTranslate("NGS", "Game title or part of a title"); font.pixelSize: 18 * root.uiScale
            background: FluidSurface { radius: 8; color: uiTheme.themeColors.button; border.color: root.selectedField === 0 ? uiTheme.themeColors.accent : uiTheme.themeColors.border }
            onAccepted: root.submit(text)
        }
        Label { width: parent.width; text: qsTranslate("NGS", "Search titles in this section. Case and punctuation do not matter."); wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale }
        Row {
            width: parent.width; spacing: 12 * root.uiScale
            FluidButton { objectName: "applyGameSearch"; text: qsTranslate("NGS", "Search", "Найти"); width: (parent.width - 2 * parent.spacing) / 3; height: 46 * root.uiScale; focusPolicy: Qt.NoFocus; highlighted: root.selectedField === 1; onClicked: root.submit(search.text) }
            FluidButton { objectName: "clearGameSearch"; text: qsTranslate("NGS", "Reset"); width: (parent.width - 2 * parent.spacing) / 3; height: 46 * root.uiScale; focusPolicy: Qt.NoFocus; highlighted: root.selectedField === 2; onClicked: root.submit("") }
            FluidButton { text: qsTranslate("NGS", "Back · ") + root.bridge.inputHints.back; width: (parent.width - 2 * parent.spacing) / 3; height: 46 * root.uiScale; focusPolicy: Qt.NoFocus; highlighted: root.selectedField === 3; onClicked: root.close() }
        }
    }
}
