import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Effects

Column {
    id: root
    required property string title
    required property url cover
    required property url background
    property string coverObjectName: "presentationCover"
    property string backgroundObjectName: "presentationBackground"
    property string titleObjectName: "presentationTitle"
    property bool coverVisible: true
    property alias posterItem: coverFrame
    property real uiScale: 1
    property bool presenting: visible
    property real reveal: 0
    property real artworkHeight: Math.max(200, Math.min(390 * uiScale, width * .34))
    spacing: 22 * uiScale
    function replay() { if (presenting) entrance.restart() }
    onPresentingChanged: {
        if (presenting) entrance.restart()
        else { entrance.stop(); reveal = 0 }
    }
    Component.onCompleted: if (presenting) entrance.start()
    NumberAnimation {
        id: entrance; target: root; property: "reveal"; from: 0; to: 1
        duration: 460; easing.type: Easing.OutBack; easing.overshoot: .55
    }
    opacity: Math.min(1, reveal)
    transform: Translate { y: (1 - root.reveal) * 22 * root.uiScale }

    Text {
        objectName: root.titleObjectName
        width: parent.width
        text: root.title; textFormat: Text.PlainText; wrapMode: Text.WordWrap
        color: uiTheme.themeColors.text
        font.pixelSize: Math.min(54 * root.uiScale, root.width * .065)
        font.weight: Font.DemiBold; font.letterSpacing: -1 * root.uiScale
        lineHeight: 1.06
    }
    component Artwork: Item {
        id: frame
        required property url source
        required property string imageName
        property bool leading: false
        Image {
            id: art
            objectName: frame.imageName
            anchors.fill: parent
            source: root.presenting && frame.visible ? frame.source : ""; asynchronous: true; fillMode: Image.PreserveAspectFit; clip: true
            horizontalAlignment: frame.leading ? Image.AlignLeft : Image.AlignHCenter
            sourceSize.width: 1920; sourceSize.height: 1080
            opacity: status === Image.Ready ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 240; easing.type: Easing.OutCubic } }
            layer.enabled: GraphicsInfo.api !== GraphicsInfo.Software
            layer.smooth: true
            layer.effect: MultiEffect {
                maskEnabled: true; maskThresholdMin: .5; maskSpreadAtMin: 1
                maskSource: Item {
                    parent: frame; width: art.width; height: art.height
                    layer.enabled: true; layer.smooth: true; visible: false
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        x: frame.leading ? 0 : (parent.width - width) / 2
                        width: art.paintedWidth; height: art.paintedHeight
                        radius: 12 * root.uiScale; color: "white"
                    }
                }
            }
        }
        Label {
            anchors.centerIn: parent; width: parent.width; horizontalAlignment: Text.AlignHCenter
            visible: art.status !== Image.Ready; wrapMode: Text.WordWrap
            text: art.status === Image.Loading ? qsTranslate("NGS", "Opening image…")
                : art.status === Image.Error ? qsTranslate("NGS", "Image unavailable") : qsTranslate("NGS", "No image selected")
            color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale
        }
    }
    Row {
        width: parent.width; height: root.artworkHeight; spacing: 24 * root.uiScale
        Artwork {
            id: coverFrame
            opacity: root.coverVisible ? 1 : 0
            width: Math.min(parent.width * .26, parent.height * 2 / 3); height: parent.height
            source: root.cover; imageName: root.coverObjectName
        }
        Artwork {
            width: parent.width - coverFrame.width - parent.spacing; height: parent.height
            leading: true
            source: String(root.background) ? root.background : root.cover; imageName: root.backgroundObjectName
        }
    }
}
