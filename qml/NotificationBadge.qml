import QtQuick

Rectangle {
    property int count: 0
    property bool error: false
    visible: count > 0
    width: Math.max(18, badgeText.implicitWidth + 8); height: 18; radius: 9
    color: error ? "#c83f50" : uiTheme.themeColors.accent
    border.width: 1; border.color: uiTheme.themeColors.surface
    Text {
        id: badgeText
        anchors.centerIn: parent
        text: parent.count
        color: parent.error ? "#ffffff" : uiTheme.themeColors.accentText
        font.pixelSize: 11; font.weight: Font.DemiBold
    }
}
