import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    objectName: "statisticsPageRoot"
    property var tokens

    function formatDuration(seconds) {
        var total = Math.max(0, Math.round(Number(seconds) || 0))
        var minutes = Math.floor(total / 60)
        var remainder = total % 60
        return minutes + ":" + (remainder < 10 ? "0" : "") + remainder
    }

    function dailyMaximum(metric) {
        var maximum = 1
        var rows = SettingsController.statisticsDailyRows
        for (var i = 0; i < rows.length; i++)
            maximum = Math.max(maximum, Number(rows[i][metric]) || 0)
        return maximum
    }

    function dailyBarHeight(value, metric, availableHeight) {
        var amount = Number(value) || 0
        if (amount <= 0)
            return 0
        return Math.max(4, (availableHeight - 38) * amount / dailyMaximum(metric))
    }

    function dailyValue(value, metric) {
        var amount = Number(value) || 0
        if (metric === "voice_seconds")
            return formatDuration(amount)
        return String(Math.round(amount))
    }

    function periodLabel() {
        if (SettingsController.statisticsPeriod === "day")
            return qsTr("最近 24 小时")
        if (SettingsController.statisticsPeriod === "week")
            return qsTr("最近 7 天")
        return qsTr("全部记录")
    }

    ScrollView {
        anchors.fill: parent
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

        ColumnLayout {
            width: root.width - 84
            x: 42
            y: 34
            spacing: 18

            RowLayout {
                Layout.fillWidth: true
                spacing: 14
                Label {
                    text: qsTr("统计")
                    color: tokens.textPrimary
                    font.pixelSize: tokens.fontSizePageTitle
                    font.bold: true
                }
                Label {
                    Layout.fillWidth: true
                    text: qsTr("Remo 语音脉冲 · ") + root.periodLabel()
                    color: tokens.textSecondary
                    font.pixelSize: tokens.fontSizeBody
                    verticalAlignment: Text.AlignVCenter
                }
                RowLayout {
                    spacing: 6
                    UiButton {
                        objectName: "statisticsRefreshButton"
                        text: qsTr("刷新")
                        quiet: true
                        implicitWidth: 64
                        onClicked: SettingsController.refreshStatistics()
                    }
                    UiButton {
                        objectName: "statisticsDayButton"
                        text: qsTr("日")
                        Accessible.name: text
                        primary: SettingsController.statisticsPeriod === "day"
                        implicitWidth: 70
                        onClicked: SettingsController.setStatisticsPeriod("day")
                    }
                    UiButton {
                        objectName: "statisticsWeekButton"
                        text: qsTr("周")
                        Accessible.name: text
                        primary: SettingsController.statisticsPeriod === "week"
                        implicitWidth: 70
                        onClicked: SettingsController.setStatisticsPeriod("week")
                    }
                    UiButton {
                        objectName: "statisticsAllButton"
                        text: qsTr("全部")
                        Accessible.name: text
                        primary: SettingsController.statisticsPeriod === "all"
                        implicitWidth: 70
                        onClicked: SettingsController.setStatisticsPeriod("all")
                    }
                    Rectangle {
                        Layout.preferredWidth: localOnlyLabel.implicitWidth + 20
                        Layout.preferredHeight: 28
                        radius: 14
                        color: "#e8faef"
                        Label {
                            id: localOnlyLabel
                            anchors.centerIn: parent
                            text: qsTr("仅保存在本机")
                            color: "#208f5c"
                            font.pixelSize: 10
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 266
                Layout.minimumHeight: 220
                spacing: 16

                UiCard {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    property string metric: "button_count"
                    property color barColor: tokens.accent
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 18
                        spacing: 4
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Rectangle {
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                                radius: 12
                                color: Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.14)
                                Label { anchors.centerIn: parent; text: qsTr("●"); color: tokens.accent; font.pixelSize: 12 }
                            }
                            Label { text: qsTr("按键脉冲"); color: tokens.textPrimary; font.pixelSize: 14; font.bold: true }
                            Item { Layout.fillWidth: true }
                        }
                        Label {
                            text: qsTr("设备响应次数 · ") + root.periodLabel() + "  ·  " + qsTr("累计 ") + SettingsController.statisticsButtonTotal + "  ·  " + qsTr("记录 ") + SettingsController.statisticsButtonRows.length
                            color: tokens.textSecondary
                            font.pixelSize: 10
                        }
                        Item {
                            id: buttonChart
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 130
                            Repeater {
                                model: 4
                                delegate: Rectangle {
                                    required property int index
                                    width: parent.width
                                    height: 1
                                    y: (parent.height - 28) * index / 3
                                    color: "#edf0f3"
                                }
                            }
                            Rectangle {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.bottomMargin: 22
                                height: 1
                                color: tokens.border
                            }
                            Row {
                                anchors.fill: parent
                                anchors.leftMargin: 6
                                anchors.rightMargin: 6
                                anchors.bottomMargin: 22
                                spacing: 5
                                Repeater {
                                    model: SettingsController.statisticsDailyRows
                                    delegate: Column {
                                        required property var modelData
                                        width: (parent.width - 30) / 7
                                        anchors.bottom: parent.bottom
                                        spacing: 4
                                        Label {
                                            width: parent.width
                                            visible: Number(modelData.button_count) > 0
                                            text: root.dailyValue(modelData.button_count, "button_count")
                                            color: tokens.textSecondary
                                            font.pixelSize: 9
                                            horizontalAlignment: Text.AlignHCenter
                                        }
                                        Rectangle {
                                            width: parent.width
                                            height: root.dailyBarHeight(modelData.button_count, "button_count", buttonChart.height)
                                            radius: 4
                                            color: tokens.accent
                                        }
                                        Label { width: parent.width; text: modelData.label; color: tokens.textSecondary; font.pixelSize: 9; horizontalAlignment: Text.AlignHCenter }
                                    }
                                }
                            }
                            Label {
                                anchors.centerIn: parent
                                visible: SettingsController.statisticsButtonTotal === 0
                                text: qsTr("暂无按键记录 · 连接遥控器后开始统计")
                                color: tokens.textSecondary
                                font.pixelSize: 11
                            }
                        }
                    }
                }

                UiCard {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 18
                        spacing: 4
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Rectangle {
                                Layout.preferredWidth: 24
                                Layout.preferredHeight: 24
                                radius: 12
                                color: Qt.rgba(tokens.voiceAccent.r, tokens.voiceAccent.g, tokens.voiceAccent.b, 0.16)
                                Label { anchors.centerIn: parent; text: qsTr("◌"); color: tokens.voiceAccent; font.pixelSize: 14 }
                            }
                            Label { text: qsTr("语音热度"); color: tokens.textPrimary; font.pixelSize: 14; font.bold: true }
                            Item { Layout.fillWidth: true }
                        }
                        Label {
                            text: qsTr("累计语音时长 · ") + root.periodLabel() + "  ·  " + qsTr("累计 ") + root.formatDuration(SettingsController.statisticsVoiceSeconds)
                            color: tokens.textSecondary
                            font.pixelSize: 10
                        }
                        Item {
                            id: voiceChart
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 130
                            Repeater {
                                model: 4
                                delegate: Rectangle {
                                    required property int index
                                    width: parent.width
                                    height: 1
                                    y: (parent.height - 28) * index / 3
                                    color: "#edf0f3"
                                }
                            }
                            Rectangle {
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.bottom: parent.bottom
                                anchors.bottomMargin: 22
                                height: 1
                                color: tokens.border
                            }
                            Row {
                                anchors.fill: parent
                                anchors.leftMargin: 6
                                anchors.rightMargin: 6
                                anchors.bottomMargin: 22
                                spacing: 5
                                Repeater {
                                    model: SettingsController.statisticsDailyRows
                                    delegate: Column {
                                        required property var modelData
                                        width: (parent.width - 30) / 7
                                        anchors.bottom: parent.bottom
                                        spacing: 4
                                        Label {
                                            width: parent.width
                                            visible: Number(modelData.voice_seconds) > 0
                                            text: root.dailyValue(modelData.voice_seconds, "voice_seconds")
                                            color: tokens.textSecondary
                                            font.pixelSize: 9
                                            horizontalAlignment: Text.AlignHCenter
                                        }
                                        Rectangle {
                                            width: parent.width
                                            height: root.dailyBarHeight(modelData.voice_seconds, "voice_seconds", voiceChart.height)
                                            radius: 4
                                            color: tokens.voiceAccent
                                        }
                                        Label { width: parent.width; text: modelData.label; color: tokens.textSecondary; font.pixelSize: 9; horizontalAlignment: Text.AlignHCenter }
                                    }
                                }
                            }
                            Label {
                                anchors.centerIn: parent
                                visible: SettingsController.statisticsVoiceSeconds <= 0
                                text: qsTr("暂无语音记录 · 按下麦克风键开始统计")
                                color: tokens.textSecondary
                                font.pixelSize: 11
                            }
                        }
                    }
                }
            }

            UiCard {
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(126, longestColumn.implicitHeight + 36)
                ColumnLayout {
                    id: longestColumn
                    anchors.fill: parent
                    anchors.margins: 18
                    spacing: 5
                    RowLayout {
                        Layout.fillWidth: true
                        Label { text: qsTr("最长语音片段"); color: tokens.textPrimary; font.pixelSize: 14; font.bold: true }
                        Label { Layout.fillWidth: true; text: qsTr("本机保存的最近记录"); color: tokens.textSecondary; font.pixelSize: 10 }
                    }
                    Repeater {
                        model: SettingsController.statisticsLongestRows
                        delegate: Rectangle {
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.preferredHeight: 30
                            color: "transparent"
                            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: tokens.border }
                            RowLayout {
                                anchors.fill: parent
                                spacing: 12
                                Label { text: "#" + modelData.rank; color: modelData.rank <= 3 ? tokens.voiceAccent : tokens.textSecondary; font.bold: true; Layout.preferredWidth: 30 }
                                Label { text: root.formatDuration(modelData.seconds); color: tokens.textPrimary; font.bold: true; Layout.preferredWidth: 54 }
                                Label { text: qsTr("语音输入"); color: tokens.textSecondary; Layout.fillWidth: true }
                                Label { text: modelData.startedAt; color: tokens.textSecondary; font.pixelSize: 10; horizontalAlignment: Text.AlignRight; Layout.preferredWidth: 132 }
                            }
                        }
                    }
                    Label { visible: SettingsController.statisticsLongestRows.length === 0; text: qsTr("暂无语音记录"); color: tokens.textSecondary; font.pixelSize: 10 }
                }
            }

            Item { Layout.preferredHeight: 12 }
        }
    }
}
