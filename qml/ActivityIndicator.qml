import QtQuick
import QtQuick.Controls.Basic

Control {
    id: control
    property bool running: true
    implicitWidth: 60; implicitHeight: 60
    // Destroy Basic's render-thread animator when hidden, including its opacity fade.
    contentItem: Loader {
        objectName: "activityLoader"
        active: control.running && control.visible
        sourceComponent: BusyIndicator { running: true; palette: control.palette }
    }
}
