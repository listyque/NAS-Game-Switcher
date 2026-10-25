import QtQuick
import QtQuick.Controls.Basic

ComboBox {
    id: control
    implicitHeight: 48
    font.pixelSize: 16
    padding: 12; rightPadding: 32
    palette.text: uiTheme.themeColors.text
    palette.buttonText: uiTheme.themeColors.text
    palette.highlightedText: uiTheme.themeColors.text
    contentItem: Text {
        text: control.displayText; font: control.font; color: uiTheme.themeColors.text
        verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
    }
    FocusFrame {
        anchors.fill: parent; z: 100
        selected: control.enabled && (control.activeFocus || control.hovered || control.popup.visible)
        radius: control.background && control.background.radius !== undefined ? control.background.radius : 8
    }
    background: FluidSurface { color: uiTheme.themeColors.button; border.color: control.activeFocus ? uiTheme.themeColors.accent : uiTheme.themeColors.border }
    delegate: ItemDelegate {
        id: choiceItem
        required property int index
        width: control.width - 12; height: 48
        highlighted: control.highlightedIndex === index
        contentItem: Text { text: control.textAt(index); color: uiTheme.themeColors.text; font: control.font; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
        background: FluidSurface {
            id: choiceSurface
            color: choiceItem.highlighted || choiceItem.hovered ? uiTheme.themeColors.soft : uiTheme.themeColors.glass
            FocusFrame { anchors.fill: parent; radius: choiceSurface.radius; selected: choiceItem.highlighted || choiceItem.hovered }
        }
    }
    popup: MotionPopup {
        x: 0; y: control.height + 4; width: control.width; padding: 6
        implicitHeight: Math.min(320, control.count * 48 + 12)
        background: FluidSurface { color: uiTheme.themeColors.glass; border.color: uiTheme.themeColors.border }
        contentItem: ListView {
            clip: true; model: control.delegateModel; currentIndex: control.highlightedIndex
            ScrollBar.vertical: ScrollBar {}
        }
    }
}
