import QtQuick

Flow {
    id: root
    property var steps: []
    property int current: 0
    property real uiScale: 1
    spacing: 12 * uiScale
    Repeater {
        model: root.steps
        Row {
            required property int index
            required property string modelData
            objectName: "workflowStep-" + index
            readonly property bool currentStep: index === root.current
            spacing: 8 * root.uiScale
            height: 32 * root.uiScale
            Rectangle {
                width: 28 * root.uiScale; height: width; radius: width / 2
                anchors.verticalCenter: parent.verticalCenter
                color: parent.currentStep ? uiTheme.themeColors.soft : uiTheme.themeColors.button
                Text { anchors.centerIn: parent; text: index + 1; color: parent.parent.currentStep ? uiTheme.themeColors.text : uiTheme.themeColors.muted; font.pixelSize: 14 * root.uiScale }
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: modelData; color: parent.currentStep ? uiTheme.themeColors.text : uiTheme.themeColors.muted
                font.pixelSize: 16 * root.uiScale; font.weight: parent.currentStep ? Font.DemiBold : Font.Normal
            }
        }
    }
}
