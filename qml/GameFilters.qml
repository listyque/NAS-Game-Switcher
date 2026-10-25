import QtQuick
import QtQuick.Controls.Basic

MotionPopup {
    id: root
    objectName: "gameFiltersPane"
    property real uiScale: 1
    property string backHint: "B"
    property string sectionName: ""
    property var statusKeys: []
    property var statusLabels: []
    property var genres: []
    property int statusIndex: 0
    property int selectedField: 0
    property int resultCount: 0
    readonly property string mode: statusKeys[statusIndex] || "all"
    readonly property string genre: genres[genreChoice.currentIndex - 1] || ""
    signal submitted(string mode, string genre)
    x: (parent.width - width) / 2; y: (parent.height - height) / 2
    width: Math.min(parent.width - 32, 680 * uiScale)
    height: contentItem.implicitHeight + topPadding + bottomPadding
    padding: 24 * uiScale; modal: true; focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: FluidSurface { elevation: 1; shadowBlur: 26; color: uiTheme.themeColors.glass; radius: 26; border.color: uiTheme.themeColors.border }
    Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim }
    function openFilters(currentMode, currentGenre) {
        statusIndex = Math.max(0, statusKeys.indexOf(currentMode))
        genreChoice.currentIndex = Math.max(0, genres.indexOf(currentGenre) + 1)
        selectedField = 0
        open()
    }
    function apply(mode, genre) { submitted(mode, genre); close() }
    function handleAction(action) {
        if (genreChoice.popup.visible) {
            if (action === "back" || action === "menu" || action === "activate") genreChoice.popup.close()
            else if (["up", "down", "left", "right"].indexOf(action) >= 0) {
                const delta = action === "up" || action === "left" ? -1 : 1
                genreChoice.currentIndex = (genreChoice.currentIndex + delta + genreChoice.count) % genreChoice.count
                genreChoice.popup.contentItem.positionViewAtIndex(genreChoice.currentIndex, ListView.Contain)
            }
            return
        }
        if (action === "back" || action === "menu") close()
        else if (["up", "down", "left", "right"].indexOf(action) >= 0)
            selectedField = (selectedField + (action === "up" || action === "left" ? 6 : 1)) % 7
        else if (action === "activate") {
            if (selectedField < 3) statusIndex = selectedField
            else if (selectedField === 3) genreChoice.popup.open()
            else if (selectedField === 4) apply(mode, genre)
            else if (selectedField === 5) apply("all", "")
            else close()
        }
    }
    component FilterButton: FluidButton {
        focusPolicy: Qt.NoFocus
        height: 52 * root.uiScale
        font.pixelSize: 16 * root.uiScale
    }
    contentItem: Column {
        spacing: 16 * root.uiScale
        Text { text: qsTranslate("NGS", "Filters · ") + root.sectionName; color: uiTheme.themeColors.text; font.pixelSize: 25 * root.uiScale }
        Text { text: qsTranslate("NGS", "Status"); color: uiTheme.themeColors.muted; font.pixelSize: 16 * root.uiScale }
        Row {
            width: parent.width; spacing: 8 * root.uiScale
            Repeater {
                model: root.statusLabels
                FilterButton {
                    required property int index
                    required property string modelData
                    objectName: "gameFilterStatus-" + index
                    text: modelData; width: (parent.width - 2 * parent.spacing) / 3
                    checkable: true; checked: root.statusIndex === index
                    highlighted: root.selectedField === index
                    onClicked: { root.statusIndex = index; root.selectedField = index }
                }
            }
        }
        Text { text: qsTranslate("NGS", "Genre", "Жанр"); color: uiTheme.themeColors.muted; font.pixelSize: 16 * root.uiScale }
        FluidComboBox {
            id: genreChoice; objectName: "gameFilterGenre"
            width: parent.width; height: 52 * root.uiScale
            model: [qsTranslate("NGS", "All genres")].concat(root.genres)
            focusPolicy: Qt.NoFocus
            font.pixelSize: 16 * root.uiScale
            FocusFrame { anchors.fill: parent; selected: root.selectedField === 3; radius: 8 }
            onPressedChanged: if (pressed) root.selectedField = 3
            delegate: FilterButton {
                required property int index
                width: genreChoice.width - 12
                text: genreChoice.textAt(index)
                highlighted: genreChoice.currentIndex === index
                onClicked: { genreChoice.currentIndex = index; genreChoice.popup.close() }
            }
        }
        Text { objectName: "gameFilterResultCount"; text: qsTranslate("NGS", "Games found: ") + root.resultCount; color: uiTheme.themeColors.muted; font.pixelSize: 16 * root.uiScale }
        Row {
            width: parent.width; spacing: 10 * root.uiScale
            FilterButton { objectName: "applyGameFilters"; text: qsTranslate("NGS", "Apply"); width: (parent.width - 2 * parent.spacing) / 3; highlighted: root.selectedField === 4; onClicked: root.apply(root.mode, root.genre) }
            FilterButton { objectName: "resetGameFilters"; text: qsTranslate("NGS", "Reset"); width: (parent.width - 2 * parent.spacing) / 3; highlighted: root.selectedField === 5; onClicked: root.apply("all", "") }
            FilterButton { objectName: "closeGameFilters"; text: qsTranslate("NGS", "Back · ") + root.backHint; width: (parent.width - 2 * parent.spacing) / 3; highlighted: root.selectedField === 6; onClicked: root.close() }
        }
    }
}
