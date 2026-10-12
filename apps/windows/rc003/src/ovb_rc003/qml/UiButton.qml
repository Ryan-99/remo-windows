import QtQuick
import QtQuick.Controls

Button {
    id: control

    property bool primary: false
    property bool quiet: false
    property bool large: false
    property color accentColor: "#087cff"

    implicitHeight: large ? 48 : 34
    implicitWidth: Math.max(large ? 168 : 72, contentLabel.implicitWidth + (large ? 44 : 26))
    leftPadding: 13
    rightPadding: 13
    topPadding: 7
    bottomPadding: 7
    hoverEnabled: true

    contentItem: Label {
        id: contentLabel
        text: control.text
        font.family: control.font.family
        font.bold: control.font.bold
        font.pixelSize: control.large ? 15 : 11
        color: !control.enabled
            ? "#a4a6ab"
            : (control.primary ? "#ffffff" : "#30343a")
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        radius: 8
        color: !control.enabled
            ? "#eef0f3"
            : (control.primary
                ? (control.down ? Qt.darker(control.accentColor, 1.12)
                    : control.hovered ? Qt.lighter(control.accentColor, 1.08)
                    : control.accentColor)
                : (control.quiet
                    ? (control.hovered ? "#e9edf2" : "#eff1f4")
                    : (control.hovered ? "#f6f8fb" : "#ffffff")))
        border.width: control.primary || control.quiet ? 0 : 1
        border.color: "#dfe3e8"

        Behavior on color { ColorAnimation { duration: 110 } }
    }
}
