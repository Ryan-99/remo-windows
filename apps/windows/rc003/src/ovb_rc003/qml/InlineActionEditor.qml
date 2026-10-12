import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

// Compact editor rendered inside the selected gray mapping card. Keeping this
// as a small reusable component prevents the left/right columns from drifting
// apart while preserving the same model-writing behavior as the full editor.
Item {
    id: root
    property var tokens
    property int rowIndex: -1
    property string buttonId: ""
    property string actionText: ""
    property string doubleClickText: ""
    property string longPressText: ""
    property bool expanded: false
    signal recordRequested(string trigger)
    implicitHeight: expanded ? 104 : 0
    opacity: expanded ? 1 : 0
    enabled: expanded
    clip: true

    Behavior on opacity {
        NumberAnimation {
            duration: 180
            easing.type: Easing.OutCubic
        }
    }

    Behavior on implicitHeight {
        NumberAnimation {
            duration: 240
            easing.type: Easing.OutCubic
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 3

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 30
            spacing: 4
            Label { text: qsTr("单击"); Layout.preferredWidth: 34; color: root.tokens.textSecondary; font.pixelSize: 10 }
            ComboBox {
                id: singleCombo
                objectName: "inlineActionCombo_" + root.buttonId
                Layout.fillWidth: true
                Layout.preferredHeight: 28
                editable: true
                model: SettingsController.presetActionOptions
                property bool initialized: false
                Component.onCompleted: { editText = root.actionText; initialized = true }
                onEditTextChanged: if (initialized) ButtonMappingModel.setActionTextAt(root.rowIndex, editText)
                onAccepted: ButtonMappingModel.setActionTextAt(root.rowIndex, editText)
                onActivated: ButtonMappingModel.setActionTextAt(root.rowIndex, currentText)
            }
            ToolButton {
                Layout.preferredWidth: 52
                Layout.preferredHeight: 28
                display: AbstractButton.TextOnly
                font.pixelSize: 9
                text: qsTr("录入")
                onClicked: root.recordRequested("single_click")
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 30
            spacing: 4
            Label { text: qsTr("双击"); Layout.preferredWidth: 34; color: root.tokens.textSecondary; font.pixelSize: 10 }
            ComboBox {
                id: doubleCombo
                objectName: "inlineDoubleActionCombo_" + root.buttonId
                Layout.fillWidth: true
                Layout.preferredHeight: 28
                editable: true
                model: SettingsController.presetActionOptions
                property bool initialized: false
                Component.onCompleted: { editText = root.doubleClickText; initialized = true }
                onEditTextChanged: if (initialized) ButtonMappingModel.setSecondaryActionTextAt(root.rowIndex, "double_click", editText)
                onAccepted: ButtonMappingModel.setSecondaryActionTextAt(root.rowIndex, "double_click", editText)
                onActivated: ButtonMappingModel.setSecondaryActionTextAt(root.rowIndex, "double_click", currentText)
            }
            ToolButton {
                Layout.preferredWidth: 52
                Layout.preferredHeight: 28
                display: AbstractButton.TextOnly
                font.pixelSize: 9
                text: qsTr("录入")
                onClicked: root.recordRequested("double_click")
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 30
            spacing: 4
            Label { text: qsTr("长按"); Layout.preferredWidth: 34; color: root.tokens.textSecondary; font.pixelSize: 10 }
            ComboBox {
                id: longCombo
                objectName: "inlineLongActionCombo_" + root.buttonId
                Layout.fillWidth: true
                Layout.preferredHeight: 28
                editable: true
                model: SettingsController.presetActionOptions
                property bool initialized: false
                Component.onCompleted: { editText = root.longPressText; initialized = true }
                onEditTextChanged: if (initialized) ButtonMappingModel.setSecondaryActionTextAt(root.rowIndex, "long_press", editText)
                onAccepted: ButtonMappingModel.setSecondaryActionTextAt(root.rowIndex, "long_press", editText)
                onActivated: ButtonMappingModel.setSecondaryActionTextAt(root.rowIndex, "long_press", currentText)
            }
            ToolButton {
                Layout.preferredWidth: 52
                Layout.preferredHeight: 28
                display: AbstractButton.TextOnly
                font.pixelSize: 9
                text: qsTr("录入")
                onClicked: root.recordRequested("long_press")
            }
        }
    }

    onActionTextChanged: singleCombo.editText = actionText
    onDoubleClickTextChanged: doubleCombo.editText = doubleClickText
    onLongPressTextChanged: longCombo.editText = longPressText
}
