import QtQuick
import QtQuick.Window

Window {
    id: root
    objectName: "startupWindow"
    required property var colors
    property url logoSource: "../assets/ngs.svg"
    property string statusText: qsTranslate("NGS", "Starting NGS")
    width: 1280; height: 800
    color: colors.background
    title: "NGS"
    flags: Qt.Window | Qt.FramelessWindowHint
    Column {
        anchors.centerIn: parent; spacing: 24; width: Math.min(420, parent.width - 60)
        Image {
            id: logo; objectName: "startupLogo"
            anchors.horizontalCenter: parent.horizontalCenter
            width: 96; height: 96; source: root.logoSource
            sourceSize.width: 192; sourceSize.height: 192
            SequentialAnimation {
                objectName: "startupPulse"
                running: root.visible; loops: Animation.Infinite
                OpacityAnimator { target: logo; from: .55; to: 1; duration: 850; easing.type: Easing.InOutSine }
                OpacityAnimator { target: logo; from: 1; to: .55; duration: 850; easing.type: Easing.InOutSine }
            }
        }
        Text {
            objectName: "startupTitle"
            width: parent.width; text: "NGS"; color: root.colors.accent
            font.pixelSize: 52; font.weight: Font.DemiBold; font.letterSpacing: 8
            horizontalAlignment: Text.AlignHCenter
        }
        Text {
            objectName: "startupStatus"
            width: parent.width; text: root.statusText; color: root.colors.muted
            font.pixelSize: 16; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
        }
    }
}
