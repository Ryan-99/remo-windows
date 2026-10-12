import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

Item {
    id: root
    objectName: "buttonsPageRoot"
    property var tokens
    // The normal interaction expands the selected gray key in place. This
    // property and the legacy editor below remain as compatibility hooks for
    // existing automation and keyboard-focused probes.
    property bool advancedExpanded: false
    property string inlineEditorButtonId: ""
    property string activationButtonId: ""
    property int activationNonce: 0
    readonly property var voicePresetIds: SettingsController.voicePresetIds

    function revealButton(buttonId, toggleExisting) {
        SettingsController.selectButton(buttonId)
        inlineEditorButtonId = (toggleExisting && inlineEditorButtonId === buttonId) ? "" : buttonId
        activationButtonId = buttonId
        activationNonce += 1
    }

    function openShortcutRecorder(buttonId, rowIndex, isMic, trigger) {
        shortcutRecorder.buttonId = buttonId
        shortcutRecorder.rowIndex = rowIndex
        shortcutRecorder.isMic = isMic
        shortcutRecorder.trigger = trigger || "single_click"
        shortcutRecorder.previewText = qsTr("请按下要映射的真实按键")
        shortcutRecorder.open()
    }

    Dialog {
        id: shortcutRecorder
        objectName: "shortcutRecorderDialog"
        modal: true
        anchors.centerIn: parent
        width: 430
        title: qsTr("录制键盘快捷键")
        standardButtons: Dialog.Cancel
        property string buttonId: ""
        property int rowIndex: -1
        property bool isMic: false
        property string trigger: "single_click"
        property string previewText: ""
        function commitShortcut(chord) {
            previewText = chord
            if (isMic)
                SettingsController.hotkeyText = chord
            else if (trigger === "single_click")
                ButtonMappingModel.setActionTextAt(rowIndex, chord)
            else
                ButtonMappingModel.setSecondaryActionTextAt(rowIndex, trigger, chord)
            close()
        }
        onOpened: { captureArea.forceActiveFocus(); SettingsController.startHotkeyCapture() }
        onClosed: SettingsController.stopHotkeyCapture()
        Connections {
            target: SettingsController
            function onHotkeyCaptured(chord) { if (shortcutRecorder.visible) shortcutRecorder.commitShortcut(chord) }
            function onHotkeyCaptureError(message) { if (shortcutRecorder.visible) shortcutRecorder.previewText = message }
        }
        contentItem: FocusScope {
            id: captureArea
            implicitHeight: 140
            focus: true
            ColumnLayout {
                anchors.fill: parent
                spacing: 10
                Label { Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter; text: shortcutRecorder.previewText; font.pixelSize: tokens.fontSizeTitle; color: tokens.accent }
                Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter; text: qsTr("直接按下键盘组合键。左右修饰键会分别记录，录制期间不会执行该快捷键。"); color: tokens.textSecondary; font.pixelSize: tokens.fontSizeSmall }
            }
        }
    }

    // Compatibility hook retained for existing probes; it is not scrollable.
    Item {
        id: rc003ScrollView
        objectName: "rc003ScrollView"
        anchors.fill: parent
        visible: SettingsController.isRc003Device
        property real contentY: 0

        ColumnLayout {
            id: rc003MappingLayout
            objectName: "rc003MappingLayout"
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.leftMargin: 42
            anchors.rightMargin: 42
            anchors.topMargin: 24
            anchors.bottomMargin: 24
            spacing: 16

            RowLayout {
                id: mappingHeader
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                Layout.fillHeight: false
                spacing: 12
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Label { text: qsTr("按键"); font.pixelSize: tokens.fontSizePageTitle; font.bold: true; color: tokens.textPrimary }
                    Label { text: qsTr("选择遥控器按键，然后配置单击、双击和长按行为"); font.pixelSize: tokens.fontSizeBody; color: tokens.textSecondary }
                }
                UiButton { objectName: "restoreMappingDefaultsButton"; text: qsTr("恢复默认"); quiet: true; onClicked: SettingsController.restoreDefaults() }
                UiButton { id: saveMappingButton; objectName: "saveMappingButton"; text: qsTr("保存映射"); onClicked: SettingsController.saveSettings() }
                Rectangle {
                    implicitWidth: 78; implicitHeight: 28; radius: 8; color: "#eaf7ef"
                    Label { anchors.centerIn: parent; text: qsTr("映射已启用"); color: "#237a4b"; font.pixelSize: 9 }
                }
            }

            Label {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                Layout.fillHeight: false
                Layout.preferredHeight: visible ? implicitHeight : 0
                visible: text.length > 0
                text: SettingsController.errorMessage.length > 0 ? SettingsController.errorMessage : SettingsController.statusMessage
                color: SettingsController.errorMessage.length > 0 ? tokens.errorColor : tokens.successColor
                font.pixelSize: 10
            }

            UiCard {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                Layout.fillHeight: false
                Layout.preferredHeight: Math.min(visualMapping.implicitHeight + 24, Math.max(320, root.height - mappingHeader.implicitHeight - 80))
                Layout.maximumHeight: Math.max(320, root.height - mappingHeader.implicitHeight - 80)
                elevated: false
                cornerRadius: 14
                clip: true

                RowLayout {
                    id: visualMapping
                    objectName: "visualMapping"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 12
                    spacing: 12

                    ColumnLayout {
                        Layout.fillWidth: true; Layout.minimumWidth: 165; Layout.alignment: Qt.AlignVCenter; spacing: 5
                        Repeater {
                            model: ButtonMappingModel
                            delegate: Button {
                                id: leftSummary
                                required property int index
                                required property string buttonId
                                required property string displayName
                                required property string actionText
                                required property string doubleClickText
                                required property string longPressText
                                required property bool isSelected
                                readonly property bool belongsHere: [
                            "back", "menu", "volume_up", "up", "left", "home"
                        ].indexOf(buttonId) >= 0
                                visible: belongsHere
                                objectName: belongsHere ? "summaryButton_" + buttonId : ""
                                Layout.fillWidth: true
                                readonly property real targetCardHeight: belongsHere ? (root.inlineEditorButtonId === buttonId ? 166 : 54) : 0
                                // Bind the layout to a named property.  This
                                // makes the Repeater delegate recalculate its
                                // geometry when a physical/software event
                                // changes inlineEditorButtonId.
                                implicitHeight: targetCardHeight
                                Layout.preferredHeight: targetCardHeight
                                Layout.minimumHeight: targetCardHeight
                                Layout.maximumHeight: targetCardHeight
                                clip: true
                                property real feedbackOpacity: 0
                                Connections {
                                    target: root
                                    function onActivationNonceChanged() {
                                        if (root.activationButtonId === leftSummary.buttonId)
                                            feedbackAnimation.restart()
                                    }
                                }
                                SequentialAnimation {
                                    id: feedbackAnimation
                                    NumberAnimation { target: leftSummary; property: "feedbackOpacity"; to: 0.22; duration: 90; easing.type: Easing.OutCubic }
                                    NumberAnimation { target: leftSummary; property: "feedbackOpacity"; to: 0; duration: 260; easing.type: Easing.InCubic }
                                }
                                Behavior on Layout.preferredHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                Behavior on Layout.minimumHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                Behavior on Layout.maximumHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                leftPadding: 9; rightPadding: 9
                                onClicked: root.revealButton(buttonId, true)
                                Accessible.name: displayName
                                background: Rectangle {
                                    radius: 8
                                    color: leftSummary.isSelected ? "#eaf3ff" : tokens.fieldBackground
                                    border.width: 1
                                    border.color: leftSummary.isSelected ? tokens.accent : "#edf0f3"
                                    Rectangle { anchors.fill: parent; radius: parent.radius; color: tokens.accent; opacity: leftSummary.feedbackOpacity; z: -1 }
                                }
                                contentItem: ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 8; spacing: 4
                                    Label {
                                        Layout.fillWidth: true
                                        text: leftSummary.displayName
                                        color: tokens.textPrimary
                                        font.bold: true
                                        font.pixelSize: 10
                                        elide: Text.ElideRight
                                    }
                                    Label {
                                        id: leftActionSummary
                                        Layout.fillWidth: true
                                        text: qsTr("单击：%1 · 双击：%2 · 长按：%3")
                                            .arg(leftSummary.actionText || qsTr("未设置"))
                                            .arg(leftSummary.doubleClickText || qsTr("未设置"))
                                            .arg(leftSummary.longPressText || qsTr("未设置"))
                                        color: leftSummary.isSelected ? tokens.accent : tokens.textSecondary
                                        font.pixelSize: 9
                                        elide: Text.ElideRight
                                        visible: root.inlineEditorButtonId !== leftSummary.buttonId
                                    }
                                    InlineActionEditor {
                                        visible: leftSummary.belongsHere
                                        expanded: root.inlineEditorButtonId === leftSummary.buttonId
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: expanded ? 104 : 0
                                        tokens: root.tokens; rowIndex: index; buttonId: leftSummary.belongsHere ? leftSummary.buttonId : ""
                                        actionText: leftSummary.actionText; doubleClickText: leftSummary.doubleClickText; longPressText: leftSummary.longPressText
                                        onRecordRequested: root.openShortcutRecorder(leftSummary.buttonId, index, false, trigger)
                                    }
                                }
                            }
                        }
                    }

                    ColumnLayout {
                        Layout.preferredWidth: 224; Layout.alignment: Qt.AlignHCenter | Qt.AlignVCenter; spacing: 7
                        Rectangle {
                            id: photoFrame
                            Layout.preferredWidth: 176; Layout.preferredHeight: 232; Layout.alignment: Qt.AlignHCenter
                            radius: 13; color: "#fafbfc"; border.color: "#e7eaee"; border.width: 1; clip: true
                            Image {
                                id: photoImage
                                objectName: "photoImage"
                                anchors.fill: parent; anchors.margins: 5; fillMode: Image.PreserveAspectFit
                                source: SettingsController.photoAvailable ? SettingsController.photoSource : ""
                                visible: SettingsController.photoAvailable; smooth: true; asynchronous: false
                            }
                            Label { anchors.centerIn: parent; visible: !SettingsController.photoAvailable; text: qsTr("实物图资源缺失"); color: tokens.textSecondary; font.pixelSize: 10 }
                            Repeater {
                                model: ButtonMappingModel
                                delegate: Item {
                                    id: hotspot
                                    objectName: "photoHotspot_" + buttonId
                                    required property string buttonId
                                    required property string displayName
                                    required property real hotspotX
                                    required property real hotspotY
                                    required property real hotspotWidth
                                    required property real hotspotHeight
                                    required property bool isSelected
                                    required property bool isVoice
                                    readonly property real paintedW: photoImage.paintedWidth
                                    readonly property real paintedH: photoImage.paintedHeight
                                    readonly property real offsetX: photoImage.x + (photoImage.width - paintedW) / 2
                                    readonly property real offsetY: photoImage.y + (photoImage.height - paintedH) / 2
                                    width: Math.max(15, hotspotWidth * paintedW); height: Math.max(12, hotspotHeight * paintedH)
                                    x: offsetX + hotspotX * paintedW - width / 2; y: offsetY + hotspotY * paintedH - height / 2
                                    visible: SettingsController.photoAvailable; activeFocusOnTab: true
                                    Accessible.role: Accessible.Button; Accessible.name: displayName
                                    Rectangle {
                                        anchors.fill: parent; radius: height / 2
                                        color: hotspot.isSelected ? (hotspot.isVoice ? "#4df2914a" : "#42087cff") : (hoverHandler.hovered ? "#22087cff" : "transparent")
                                        border.width: hotspot.isSelected ? 2 : 0; border.color: hotspot.isVoice ? tokens.voiceAccent : tokens.accent
                                        scale: hotspot.isSelected ? 1.12 : 1.0
                                        Behavior on scale { NumberAnimation { duration: 160; easing.type: Easing.OutCubic } }
                                    }
                                    HoverHandler { id: hoverHandler }
                                    property real feedbackOpacity: 0
                                    Connections {
                                        target: root
                                        function onActivationNonceChanged() {
                                            if (root.activationButtonId === hotspot.buttonId)
                                                hotspotFeedback.restart()
                                        }
                                    }
                                    SequentialAnimation {
                                        id: hotspotFeedback
                                        NumberAnimation { target: hotspot; property: "feedbackOpacity"; to: 0.85; duration: 90; easing.type: Easing.OutCubic }
                                        NumberAnimation { target: hotspot; property: "feedbackOpacity"; to: 0; duration: 300; easing.type: Easing.InCubic }
                                    }
                                    Rectangle { anchors.fill: parent; radius: height / 2; color: tokens.accent; opacity: hotspot.feedbackOpacity; z: 2 }
                                    TapHandler { onTapped: root.revealButton(hotspot.buttonId, true) }
                                    // Do not bind the desktop keyboard Enter/
                                    // Space to a remote hotspot.  RC003's OK
                                    // usage is 0x28 (the same HID usage as a
                                    // normal keyboard Enter); keyboard focus
                                    // must never be allowed to simulate a
                                    // physical remote press.
                                }
                            }
                        }
                        Repeater {
                            model: ButtonMappingModel
                            delegate: Button {
                                id: micSummary
                                required property string buttonId
                                required property string displayName
                                required property bool isMic
                                required property bool isSelected
                                visible: isMic; objectName: isMic ? "summaryButton_" + buttonId : ""
                                // Keep the voice preset and the record action
                                // readable inside the same card; the remote
                                // image remains 196px wide, while this editor
                                // gets the full center-column width.
                                Layout.preferredWidth: 224
                                readonly property real targetCardHeight: isMic ? (root.inlineEditorButtonId === buttonId ? 126 : 58) : 0
                                implicitHeight: targetCardHeight
                                Layout.preferredHeight: targetCardHeight
                                Layout.minimumHeight: targetCardHeight
                                Layout.maximumHeight: targetCardHeight
                                Layout.alignment: Qt.AlignHCenter
                                clip: true
                                Behavior on Layout.preferredHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                Behavior on Layout.minimumHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                Behavior on Layout.maximumHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                onClicked: root.revealButton(buttonId, true)
                                Accessible.name: displayName
                                background: Rectangle {
                                    radius: 10
                                    color: micSummary.isSelected ? "#fff2e8" : tokens.fieldBackground
                                    border.width: 1
                                    border.color: micSummary.isSelected ? tokens.voiceAccent : "#edf0f3"
                                }
                                contentItem: ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: 8
                                    spacing: 5
                                    Label {
                                        Layout.fillWidth: true
                                        text: qsTr("麦克风键  ·  语音输入")
                                        horizontalAlignment: Text.AlignHCenter
                                        color: micSummary.isSelected ? tokens.voiceAccent : tokens.textPrimary
                                        font.bold: true
                                        font.pixelSize: 11
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: qsTr("按住：%1").arg(SettingsController.hotkeyText)
                                        horizontalAlignment: Text.AlignHCenter
                                        color: tokens.textSecondary
                                        font.pixelSize: 9
                                        elide: Text.ElideRight
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 30
                                        spacing: 5
                                        visible: root.inlineEditorButtonId === micSummary.buttonId
                                        opacity: visible ? 1 : 0
                                        clip: true
                                        Behavior on opacity { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
                                        ComboBox {
                                            id: voicePresetCombo
                                            objectName: "voicePresetCombo"
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 26
                                            model: SettingsController.voicePresetOptions
                                            currentIndex: Math.max(0, root.voicePresetIds.indexOf(SettingsController.voicePresetId))
                                            onActivated: SettingsController.applyVoicePreset(root.voicePresetIds[index])
                                            Accessible.name: qsTr("语音输入法预设")
                                        }
                                        UiButton {
                                            id: recordVoiceShortcutButton
                                            objectName: "recordVoiceShortcutButton"
                                            Layout.preferredHeight: 26
                                            text: qsTr("录入")
                                            quiet: true
                                            onClicked: root.openShortcutRecorder("mic", -1, true, "single_click")
                                        }
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        visible: root.inlineEditorButtonId === micSummary.buttonId
                                        text: qsTr("点击卡片可收起语音配置")
                                        horizontalAlignment: Text.AlignHCenter
                                        color: tokens.textSecondary
                                        font.pixelSize: 8
                                        opacity: visible ? 1 : 0
                                        Behavior on opacity { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
                                    }
                                }
                            }
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true; Layout.minimumWidth: 165; Layout.alignment: Qt.AlignVCenter; spacing: 5
                        Repeater {
                            model: ButtonMappingModel
                            delegate: Button {
                                id: rightSummary
                                required property int index
                                required property string buttonId
                                required property string displayName
                                required property string actionText
                                required property string doubleClickText
                                required property string longPressText
                                required property bool isSelected
                                readonly property bool belongsHere: [
                            "power", "volume_down", "tv", "down", "right", "ok"
                        ].indexOf(buttonId) >= 0
                                visible: belongsHere; objectName: belongsHere ? "summaryButton_" + buttonId : ""
                                Layout.fillWidth: true
                                readonly property real targetCardHeight: belongsHere ? (root.inlineEditorButtonId === buttonId ? 166 : 54) : 0
                                implicitHeight: targetCardHeight
                                Layout.preferredHeight: targetCardHeight
                                Layout.minimumHeight: targetCardHeight
                                Layout.maximumHeight: targetCardHeight
                                clip: true
                                property real feedbackOpacity: 0
                                Connections {
                                    target: root
                                    function onActivationNonceChanged() {
                                        if (root.activationButtonId === rightSummary.buttonId)
                                            feedbackAnimationRight.restart()
                                    }
                                }
                                SequentialAnimation {
                                    id: feedbackAnimationRight
                                    NumberAnimation { target: rightSummary; property: "feedbackOpacity"; to: 0.22; duration: 90; easing.type: Easing.OutCubic }
                                    NumberAnimation { target: rightSummary; property: "feedbackOpacity"; to: 0; duration: 260; easing.type: Easing.InCubic }
                                }
                                Behavior on Layout.preferredHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                Behavior on Layout.minimumHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                Behavior on Layout.maximumHeight {
                                    NumberAnimation { duration: 240; easing.type: Easing.OutCubic }
                                }
                                leftPadding: 9; rightPadding: 9
                                onClicked: root.revealButton(buttonId, true)
                                Accessible.name: displayName
                                background: Rectangle {
                                    radius: 8
                                    color: rightSummary.isSelected ? "#eaf3ff" : tokens.fieldBackground
                                    border.width: 1
                                    border.color: rightSummary.isSelected ? tokens.accent : "#edf0f3"
                                    Rectangle { anchors.fill: parent; radius: parent.radius; color: tokens.accent; opacity: rightSummary.feedbackOpacity; z: -1 }
                                }
                                contentItem: ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 8; spacing: 4
                                    Label {
                                        id: rightActionSummary
                                        Layout.fillWidth: true
                                        text: rightSummary.displayName
                                        color: tokens.textPrimary
                                        font.bold: true
                                        font.pixelSize: 10
                                        elide: Text.ElideRight
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: qsTr("单击：%1 · 双击：%2 · 长按：%3")
                                            .arg(rightSummary.actionText || qsTr("未设置"))
                                            .arg(rightSummary.doubleClickText || qsTr("未设置"))
                                            .arg(rightSummary.longPressText || qsTr("未设置"))
                                        color: rightSummary.isSelected ? tokens.accent : tokens.textSecondary
                                        font.pixelSize: 9
                                        elide: Text.ElideRight
                                        visible: root.inlineEditorButtonId !== rightSummary.buttonId
                                    }
                                    InlineActionEditor {
                                        visible: rightSummary.belongsHere
                                        expanded: root.inlineEditorButtonId === rightSummary.buttonId
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: expanded ? 104 : 0
                                        tokens: root.tokens; rowIndex: index; buttonId: rightSummary.belongsHere ? rightSummary.buttonId : ""
                                        actionText: rightSummary.actionText; doubleClickText: rightSummary.doubleClickText; longPressText: rightSummary.longPressText
                                        onRecordRequested: root.openShortcutRecorder(rightSummary.buttonId, index, false, trigger)
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // Voice settings live in the microphone card above. Keeping the
            // compatibility control hidden avoids a second, disconnected
            // configuration row below the mapping surface.
            Button { id: advancedMappingToggle; objectName: "advancedMappingToggle"; visible: false; text: root.advancedExpanded ? qsTr("收起配置") : qsTr("配置当前按键"); flat: true; onClicked: root.advancedExpanded = !root.advancedExpanded }
            // text: qsTr("语音输入") is kept in the embedded microphone card
            // above; this hidden compatibility label preserves the old object
            // contract without rendering a duplicate row.
            Label { visible: false; text: qsTr("语音输入") }

            UiCard {
                id: advancedEditor
                objectName: "advancedEditor"
                visible: root.advancedExpanded
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignTop
                Layout.preferredHeight: root.advancedExpanded ? 166 : 0
                Layout.minimumHeight: root.advancedExpanded ? 166 : 0
                Layout.maximumHeight: root.advancedExpanded ? 166 : 0
                elevated: false; cornerRadius: 14

                RowLayout {
                    anchors.fill: parent; anchors.margins: 9; spacing: 10
                    ColumnLayout {
                        Layout.preferredWidth: 250
                        Layout.minimumWidth: 220
                        Layout.maximumWidth: 270
                        Layout.fillHeight: true
                        spacing: 4
                        RowLayout {
                            Layout.fillWidth: true
                            Label { Layout.fillWidth: true; text: qsTr("选择按键"); color: tokens.textPrimary; font.bold: true; font.pixelSize: 11 }
                            // Physical calibration is fixed in the shipped RC003
                            // layout. Keep the object hook for older probes, but
                            // do not expose a calibration control in the product UI.
                            Button { id: detectRealKeyButton; objectName: "detectRealKeyButton"; visible: false; width: 0; text: "" }
                        }
                        GridView {
                            id: mappingList
                            objectName: "mappingList"
                            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; model: ButtonMappingModel
                            cellWidth: Math.floor(width / 2); cellHeight: 26
                            // Selection highlighting comes from the shared model.
                            // Starting at -1 prevents GridView from scrolling to
                            // the last default-selected row and clipping the top.
                            currentIndex: -1
                            onCurrentIndexChanged: positionViewAtIndex(currentIndex, GridView.Contain)
                            delegate: Rectangle {
                                id: mappingRow
                                required property int index
                                required property string buttonId
                                required property string displayName
                                required property string hidUsage
                                required property string actionText
                                required property string doubleClickText
                                required property string longPressText
                                required property bool isMic
                                required property bool isSelected
                                width: mappingList.cellWidth - 4; height: mappingList.cellHeight - 3; radius: 7
                                color: isSelected ? "#eaf3ff" : "transparent"; border.width: isSelected ? 1 : 0; border.color: tokens.accent
                                onActionTextChanged: actionCombo.editText = actionText
                                Button {
                                    anchors.left: parent.left
                                    anchors.right: actionEditor.left
                                    anchors.top: parent.top
                                    anchors.bottom: parent.bottom
                                    flat: true; leftPadding: 7; rightPadding: 7
                                    onClicked: SettingsController.selectButton(mappingRow.buttonId)
                                    contentItem: RowLayout {
                                        Label { Layout.fillWidth: true; text: mappingRow.displayName; color: tokens.textPrimary; font.pixelSize: 10; font.bold: mappingRow.isSelected }
                                        Label { visible: mappingRow.isMic; text: qsTr("固定语音"); color: mappingRow.isSelected ? tokens.voiceAccent : tokens.textSecondary; font.pixelSize: 8 }
                                    }
                                }
                                Item {
                                    id: actionEditor
                                    anchors.right: parent.right
                                    anchors.top: parent.top
                                    anchors.bottom: parent.bottom
                                    width: mappingRow.isMic ? 0 : 124
                                    ComboBox {
                                        id: actionCombo
                                        objectName: "actionCombo_" + mappingRow.buttonId
                                        anchors.fill: parent
                                        visible: !mappingRow.isMic
                                        editable: true
                                        model: SettingsController.presetActionOptions
                                        property bool initialized: false
                                        Component.onCompleted: { editText = mappingRow.actionText; initialized = true }
                                        onEditTextChanged: if (initialized && !mappingRow.isMic) ButtonMappingModel.setActionTextAt(mappingRow.index, editText)
                                        onAccepted: ButtonMappingModel.setActionTextAt(mappingRow.index, editText)
                                        onActivated: ButtonMappingModel.setActionTextAt(mappingRow.index, currentText)
                                    }
                                }
                            }
                        }
                        Label { Layout.fillWidth: true; Layout.minimumWidth: 0; text: SettingsController.keyDetectionText; color: SettingsController.keyDetectionActive ? tokens.accent : tokens.textSecondary; font.pixelSize: 8; elide: Text.ElideRight }
                    }

                    Rectangle { Layout.preferredWidth: 1; Layout.minimumWidth: 1; Layout.maximumWidth: 1; Layout.fillHeight: true; color: tokens.border }

                    Item {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                                    Layout.preferredWidth: 560
                                    Layout.minimumWidth: 420
                        clip: true
                        Repeater {
                            model: ButtonMappingModel
                            delegate: ColumnLayout {
                                id: detailEditor
                                required property int index
                                required property string buttonId
                                required property string displayName
                                required property string hidUsage
                                required property string actionText
                                required property string doubleClickText
                                required property string longPressText
                                required property bool isMic
                                required property bool isSelected
                                visible: isSelected; anchors.fill: parent; spacing: 5
                                onActionTextChanged: detailSingleCombo.editText = actionText
                                onDoubleClickTextChanged: doubleActionCombo.editText = doubleClickText
                                onLongPressTextChanged: longActionCombo.editText = longPressText
                                    RowLayout {
                                        Layout.fillWidth: true
                                    Label { text: detailEditor.displayName; color: tokens.textPrimary; font.bold: true; font.pixelSize: 13 }
                                    Label { Layout.fillWidth: true; text: detailEditor.hidUsage; color: tokens.textSecondary; font.pixelSize: 9 }
                                    Label { text: detailEditor.isMic ? qsTr("语音专用") : qsTr("三种触发方式"); color: detailEditor.isMic ? tokens.voiceAccent : tokens.accent; font.pixelSize: 9 }
                                }
                                Label {
                                    visible: detailEditor.isMic; Layout.fillWidth: true; Layout.fillHeight: true; wrapMode: Text.WordWrap; verticalAlignment: Text.AlignVCenter
                                    text: qsTr("麦克风键固定用于语音输入。可在上方选择输入法预设，或录入对应语音工具的快捷键。")
                                    color: tokens.textSecondary; font.pixelSize: 11
                                }
                                RowLayout {
                                    visible: !detailEditor.isMic; Layout.fillWidth: true; spacing: 5
                                    Label { text: qsTr("单击"); Layout.preferredWidth: 34; color: tokens.textSecondary; font.pixelSize: 10 }
                                    ComboBox {
                                        id: detailSingleCombo; objectName: "detailActionCombo_" + detailEditor.buttonId; Layout.fillWidth: true; editable: true
                                        model: SettingsController.presetActionOptions; property bool initialized: false
                                        Component.onCompleted: { editText = detailEditor.actionText; initialized = true }
                                        onEditTextChanged: if (initialized) ButtonMappingModel.setActionTextAt(detailEditor.index, editText)
                                        onAccepted: ButtonMappingModel.setActionTextAt(detailEditor.index, editText)
                                        onActivated: ButtonMappingModel.setActionTextAt(detailEditor.index, currentText)
                                    }
                                    UiButton { text: qsTr("录入"); quiet: true; onClicked: root.openShortcutRecorder(detailEditor.buttonId, detailEditor.index, false, "single_click") }
                                }
                                RowLayout {
                                    visible: !detailEditor.isMic; Layout.fillWidth: true; spacing: 5
                                    Label { text: qsTr("双击"); Layout.preferredWidth: 34; color: tokens.textSecondary; font.pixelSize: 10 }
                                    ComboBox {
                                        id: doubleActionCombo; objectName: "doubleActionCombo_" + detailEditor.buttonId; Layout.fillWidth: true; editable: true
                                        model: SettingsController.presetActionOptions; property bool initialized: false
                                        Component.onCompleted: { editText = detailEditor.doubleClickText; initialized = true }
                                        onEditTextChanged: if (initialized) ButtonMappingModel.setSecondaryActionTextAt(detailEditor.index, "double_click", editText)
                                        onAccepted: ButtonMappingModel.setSecondaryActionTextAt(detailEditor.index, "double_click", editText)
                                        onActivated: ButtonMappingModel.setSecondaryActionTextAt(detailEditor.index, "double_click", currentText)
                                    }
                                    UiButton { text: qsTr("录入"); quiet: true; onClicked: root.openShortcutRecorder(detailEditor.buttonId, detailEditor.index, false, "double_click") }
                                }
                                RowLayout {
                                    visible: !detailEditor.isMic; Layout.fillWidth: true; spacing: 5
                                    Label { text: qsTr("长按"); Layout.preferredWidth: 34; color: tokens.textSecondary; font.pixelSize: 10 }
                                    ComboBox {
                                        id: longActionCombo; objectName: "longActionCombo_" + detailEditor.buttonId; Layout.fillWidth: true; editable: true
                                        model: SettingsController.presetActionOptions; property bool initialized: false
                                        Component.onCompleted: { editText = detailEditor.longPressText; initialized = true }
                                        onEditTextChanged: if (initialized) ButtonMappingModel.setSecondaryActionTextAt(detailEditor.index, "long_press", editText)
                                        onAccepted: ButtonMappingModel.setSecondaryActionTextAt(detailEditor.index, "long_press", editText)
                                        onActivated: ButtonMappingModel.setSecondaryActionTextAt(detailEditor.index, "long_press", currentText)
                                    }
                                    UiButton { text: qsTr("录入"); quiet: true; onClicked: root.openShortcutRecorder(detailEditor.buttonId, detailEditor.index, false, "long_press") }
                                }
                                Label { visible: !detailEditor.isMic; Layout.fillWidth: true; text: qsTr("单击、双击、长按可以绑定系统按键或快捷键。"); color: tokens.textSecondary; font.pixelSize: 9 }
                            }
                        }
                    }
                }
            }
        }
    }

    // Compatibility anchor for older automation and the future DJI adapter.
    // It intentionally has no content and is never exposed in the current UI.
    Item {
        id: djiControlLayout
        objectName: "djiControlLayout"
        visible: false
    }

}
