import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    objectName: "systemPageRoot"
    property var tokens

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 42
            Layout.rightMargin: 42
            Layout.topMargin: 24
            Layout.bottomMargin: 12
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Label {
                    text: systemTabs.currentIndex === 0 ? qsTr("日志与诊断") : qsTr("权限")
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizePageTitle
                    font.bold: true
                }
                Label {
                    text: systemTabs.currentIndex === 0
                          ? qsTr("巡检有问题，就按提示去修")
                          : qsTr("看遥控器有没有连上，语音有没有在传")
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeBody
                }
            }
            TabBar {
                id: systemTabs
                objectName: "systemTabs"
                Layout.preferredWidth: 270
                TabButton {
                    objectName: "diagnosticsTabButton"
                    text: qsTr("日志与诊断")
                }
                TabButton {
                    objectName: "permissionsTabButton"
                    text: qsTr("权限")
                }
            }
        }

        StackLayout {
            objectName: "systemPageStack"
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: systemTabs.currentIndex
            DiagnosticsPage { tokens: root.tokens }
            PermissionsPage { tokens: root.tokens }
        }
    }
}
