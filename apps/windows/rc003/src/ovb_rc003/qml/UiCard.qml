import QtQuick

Item {
    id: root

    property color fillColor: "#fdfefe"
    property color strokeColor: "#e3e7ed"
    property real cornerRadius: 21
    property bool elevated: true
    default property alias contentData: surface.data

    Rectangle {
        visible: root.elevated
        x: 1
        y: 2
        width: parent.width - 2
        height: parent.height - 1
        radius: root.cornerRadius
        color: "#08000000"
    }

    Rectangle {
        id: surface
        anchors.fill: parent
        radius: root.cornerRadius
        color: root.fillColor
        border.color: root.strokeColor
        border.width: 1
        clip: true
    }
}
