import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    property var tokens

    ColumnLayout {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.leftMargin: 42
        anchors.rightMargin: 42
        anchors.topMargin: 24
        spacing: 16

        ColumnLayout {
            spacing: 3
            Label { text: qsTr("关于"); font.pixelSize: tokens.fontSizePageTitle; font.bold: true; color: tokens.textPrimary }
            Label { text: qsTr("Windows 客户端信息"); font.pixelSize: tokens.fontSizeBody; color: tokens.textSecondary }
        }
        UiCard {
            objectName: "aboutInfoCard"
            Layout.fillWidth: true
            Layout.maximumWidth: 650
            implicitHeight: aboutColumn.implicitHeight + tokens.spacingLarge * 2
            fillColor: tokens.surface
            strokeColor: tokens.border
            cornerRadius: tokens.cornerRadiusLarge
            ColumnLayout {
                id: aboutColumn
                anchors.fill: parent
                anchors.margins: 20
                spacing: 12
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 14
                    Image {
                        Layout.preferredWidth: 72
                        Layout.preferredHeight: 72
                        source: SettingsController.appLogoSource
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                        sourceSize.width: 144
                        sourceSize.height: 144
                    }
                    Label { text: qsTr("Remo"); font.pixelSize: 20; font.bold: true; color: tokens.textPrimary }
                }
                Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: qsTr("连接、音频路由和按键映射保持清晰可控，设置只保存在本机。"); color: tokens.textSecondary; font.pixelSize: tokens.fontSizeBody }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    UiButton {
                        objectName: "checkForUpdatesButton"
                        text: SettingsController.updateAvailable ? qsTr("下载并安装") : qsTr("检查更新")
                        onClicked: SettingsController.updateAvailable
                            ? SettingsController.installUpdate()
                            : SettingsController.checkForUpdates()
                    }
                    Label {
                        Layout.fillWidth: true
                        text: SettingsController.updateStatus
                        color: SettingsController.updateAvailable ? tokens.accent : tokens.textSecondary
                        font.pixelSize: tokens.fontSizeBody
                        elide: Text.ElideRight
                    }
                }
                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: 10
                    rowSpacing: 10
                    Repeater {
                        model: [
                            { title: qsTr("版本"), value: SettingsController.appVersion },
                            { title: qsTr("设备"), value: qsTr("RC003 / Bluetooth LE") },
                            { title: qsTr("音频"), value: qsTr("WASAPI · VB-CABLE") },
                            { title: qsTr("许可"), value: qsTr("GPL-3.0-only") }
                        ]
                        delegate: Rectangle {
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.preferredHeight: 62
                            radius: 11
                            color: tokens.fieldBackground
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 11
                                spacing: 3
                                Label { text: modelData.title; color: tokens.textPrimary; font.bold: true; font.pixelSize: 11 }
                                Label { text: modelData.value; color: tokens.textSecondary; font.pixelSize: 10 }
                            }
                        }
                    }
                }
            }
        }
    }
}
