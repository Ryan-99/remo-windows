// Top-level Windows settings window (XRBM-030 In-scope item 2). It uses a
// native Qt Quick window and Windows' own title-bar/Fluent chrome.
//
// `SettingsController`/`ButtonMappingModel` below are QML SINGLETON types
// (registered via qmlRegisterSingletonInstance in
// qt_settings_app.run_settings_window(), imported from the
// "OvbRc003Settings" module) - deliberately not exposed as
// engine.rootContext() context properties. During this task, an
// isolated repro proved that a context property can read back as null the
// first time it is accessed from a binding evaluated during a nested
// component's own construction (e.g. inside a ScrollView's deferred content,
// or a ListView's currentIndex binding, before an externally-supplied
// property has finished propagating down to it) - a QML singleton has no
// such hazard, since every file that imports the module gets the same
// already-fully-constructed instance immediately, resolved once by the
// type system rather than walked through a context hierarchy each time.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OvbRc003Settings 1.0

ApplicationWindow {
    id: window
    title: qsTr("Remo 设置")
    width: 1120
    height: 760
    minimumWidth: 900
    minimumHeight: 700
    visible: true
    // Qt.Window keeps this a normal top-level program. Frameless alone is
    // treated as a tool window, which Windows hides from the taskbar.
    flags: Qt.Window | Qt.FramelessWindowHint | Qt.WindowSystemMenuHint | Qt.WindowMinimizeButtonHint

    property Tokens tokens: Tokens {}

    // Bridge status is process-wide, not a connection-page concern. Polling
    // from the root keeps physical RC003 presses visible while the mapping,
    // statistics, or permissions page is active.
    Timer {
        id: bridgeStatusPollTimer
        objectName: "bridgeStatusPollTimer"
        interval: 120
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: SettingsController.refreshBridgeStatus()
    }

    Timer {
        id: usageRefreshTimer
        interval: 3000
        repeat: true
        running: true
        triggeredOnStart: false
        onTriggered: SettingsController.refreshStatistics()
    }

    Timer {
        id: updateCheckTimer
        interval: 1200
        repeat: false
        running: true
        onTriggered: SettingsController.checkForUpdates()
    }

    // QQuickStyle may install its own per-control font after the Python
    // application font has been set.  Propagating the family from the
    // ApplicationWindow keeps the real frozen Windows process on the same
    // sans-serif Chinese UI face as the approved HTML prototype instead of
    // falling back to a Song/serif face.
    font.family: tokens.fontFamily
    font.pixelSize: tokens.fontSizeBody

    // Opaque client. A transparent margin made the Windows drag frame much
    // larger than the visible window; the shadow now comes from DWM.
    color: tokens.background
    // XRBM-030 RETRY 1 blocker 2: Qt Quick Controls' "FluentWinUI3" style
    // resolves its OWN default text/background colors from
    // Qt.styleHints.colorScheme independently of Tokens.qml's colors (which
    // come from the plain QtQuick `SystemPalette` type instead) - under the
    // offscreen QPA platform (no real desktop/compositor), colorScheme
    // reports `Unknown`, and FluentWinUI3 falls back to a DARK-styled
    // control appearance (white button/tab/field text) while SystemPalette
    // separately falls back to a LIGHT one (white base, black text) - two
    // independent "default" guesses that disagree, producing white text on
    // a light background. Explicitly setting this window's own `palette`
    // (which Qt Quick Controls propagates down to every descendant Button/
    // TabButton/ComboBox/TextField automatically, verified by a minimal
    // isolated repro during this task) from the SAME SystemPalette-derived
    // tokens used for the rest of this window keeps both systems reading
    // from one source of truth in both light AND dark real Windows
    // sessions - this does not hard-code a light-only palette, since every
    // value below is itself OS-derived via Tokens.qml.
    palette.window: tokens.background
    palette.windowText: tokens.textPrimary
    palette.button: tokens.buttonBackground
    palette.buttonText: tokens.buttonText
    palette.base: tokens.fieldBackground
    palette.text: tokens.textPrimary
    palette.highlight: tokens.accent
    palette.highlightedText: tokens.accentText

    Rectangle {
        id: windowSurface
        anchors.fill: parent
        anchors.margins: 0
        radius: 12
        clip: true
        color: tokens.background
        antialiasing: true
        border.width: 0

        Rectangle {
            id: titleBar
            objectName: "customTitleBar"
            // `sidebar` lives inside the page RowLayout, so it is not a
            // direct sibling of this title bar. Bind x instead of anchoring
            // across that hierarchy (which would produce a QML warning).
            x: sidebar.x + sidebar.width
            anchors.top: parent.top
            height: 46
            width: parent.width - x
            // Match the reference composition: the title area is a quiet
            // continuation of the light app surface, with no extra title
            // text competing with the navigation and page heading.
            color: tokens.background
            topRightRadius: windowSurface.radius
            z: 10

            MouseArea {
            anchors.left: parent.left
            anchors.right: windowControls.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            onPressed: window.startSystemMove()
            onDoubleClicked: window.visibility === Window.Maximized
                ? window.showNormal() : window.showMaximized()
        }

            Row {
            id: windowControls
            anchors.right: parent.right
            anchors.top: parent.top
            height: parent.height

            Button {
                objectName: "minimizeWindowButton"
                width: 46; height: titleBar.height; flat: true
                onClicked: window.showMinimized()
                Accessible.name: qsTr("最小化")
                contentItem: Item { anchors.fill: parent; Image { anchors.centerIn: parent; width: 16; height: 16; source: "icons/window-minimize.svg"; sourceSize.width: 32; sourceSize.height: 32; fillMode: Image.PreserveAspectFit; opacity: parent.parent.hovered ? 1 : 0.72 } }
                background: Rectangle { color: parent.hovered ? "#f0f2f4" : "transparent" }
            }
            Button {
                objectName: "maximizeWindowButton"
                width: 46; height: titleBar.height; flat: true
                onClicked: window.visibility === Window.Maximized ? window.showNormal() : window.showMaximized()
                Accessible.name: window.visibility === Window.Maximized ? qsTr("还原") : qsTr("最大化")
                contentItem: Item { anchors.fill: parent; Image { anchors.centerIn: parent; width: 16; height: 16; source: window.visibility === Window.Maximized ? "icons/window-restore.svg" : "icons/window-maximize.svg"; sourceSize.width: 32; sourceSize.height: 32; fillMode: Image.PreserveAspectFit; opacity: parent.parent.hovered ? 1 : 0.72 } }
                background: Rectangle { color: parent.hovered ? "#f0f2f4" : "transparent" }
            }
            Button {
                objectName: "closeWindowButton"
                width: 48; height: titleBar.height; flat: true
                onClicked: window.close()
                Accessible.name: qsTr("关闭")
                contentItem: Item { anchors.fill: parent; Image { anchors.centerIn: parent; width: 16; height: 16; source: "icons/window-close.svg"; sourceSize.width: 32; sourceSize.height: 32; fillMode: Image.PreserveAspectFit; opacity: parent.parent.hovered ? 1 : 0.92 } }
                // Keep the hover fill inside the same top-right arc as the
                // window surface; a square hover rectangle used to cover the
                // rounded corner.
                background: Rectangle {
                    color: parent.hovered ? "#e81123" : "transparent"
                    radius: windowSurface.radius
                    topLeftRadius: 0
                    bottomLeftRadius: 0
                    bottomRightRadius: 0
                }
            }
        }

        }

        RowLayout {
        anchors.left: windowSurface.left
        anchors.right: windowSurface.right
        anchors.top: windowSurface.top
        anchors.bottom: windowSurface.bottom
        spacing: 0

        Rectangle {
            id: sidebar
            objectName: "sidebar"
            Layout.preferredWidth: 112
            Layout.fillHeight: true
            color: "#ffffff"
            border.color: tokens.border
            border.width: 0
            radius: windowSurface.radius
            clip: true

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: tokens.spacingSmall
                spacing: tokens.spacingSmall

                Image {
                    Layout.alignment: Qt.AlignHCenter
                    Layout.preferredWidth: 52
                    Layout.preferredHeight: 52
                    source: SettingsController.appLogoSource
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    sourceSize.width: 76
                    sourceSize.height: 76
                }
                Item { Layout.preferredHeight: 8 }

                Item {
                    id: tabBar
                    objectName: "tabBar"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    property int currentIndex: 0

                    ColumnLayout {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        spacing: tokens.spacingSmall

                    Button {
                        id: connectionTab
                        objectName: "connectionTabButton"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 56
                        text: qsTr("连接")
                        Accessible.name: text
                        highlighted: tabBar.currentIndex === 0
                        onClicked: tabBar.currentIndex = 0
                        contentItem: ColumnLayout {
                            spacing: 1
                            Image { Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 18; Layout.preferredHeight: 18; source: "icons/radio-tower.svg"; fillMode: Image.PreserveAspectFit; opacity: connectionTab.highlighted ? 1 : 0.72 }
                            Label { Layout.fillWidth: true; text: connectionTab.text; horizontalAlignment: Text.AlignHCenter; color: connectionTab.highlighted ? Qt.darker(tokens.accent, 1.25) : tokens.textPrimary; font.pixelSize: 10 }
                        }
                        background: Rectangle { radius: tokens.cornerRadiusSmall; color: connectionTab.highlighted ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.14) : "transparent" }
                    }
                    Button {
                        id: mappingTab
                        objectName: "mappingTabButton"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 56
                        text: SettingsController.isRc003Device ? qsTr("按键") : qsTr("设备")
                        Accessible.name: text
                        highlighted: tabBar.currentIndex === 1
                        onClicked: tabBar.currentIndex = 1
                        contentItem: ColumnLayout {
                            spacing: 1
                            Image { Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 18; Layout.preferredHeight: 18; source: "icons/sliders-horizontal.svg"; fillMode: Image.PreserveAspectFit; opacity: mappingTab.highlighted ? 1 : 0.72 }
                            Label { Layout.fillWidth: true; text: mappingTab.text; horizontalAlignment: Text.AlignHCenter; color: mappingTab.highlighted ? Qt.darker(tokens.accent, 1.25) : tokens.textPrimary; font.pixelSize: 10 }
                        }
                        background: Rectangle { radius: tokens.cornerRadiusSmall; color: mappingTab.highlighted ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.14) : "transparent" }
                    }
                    Button {
                        id: systemTab
                        objectName: "systemTabButton"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 56
                        text: qsTr("权限")
                        Accessible.name: text
                        highlighted: tabBar.currentIndex === 2
                        onClicked: tabBar.currentIndex = 2
                        contentItem: ColumnLayout {
                            spacing: 1
                            Image { Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 18; Layout.preferredHeight: 18; source: "icons/shield-check.svg"; fillMode: Image.PreserveAspectFit; opacity: systemTab.highlighted ? 1 : 0.72 }
                            Label { Layout.fillWidth: true; text: systemTab.text; horizontalAlignment: Text.AlignHCenter; color: systemTab.highlighted ? Qt.darker(tokens.accent, 1.25) : tokens.textPrimary; font.pixelSize: 10 }
                        }
                        background: Rectangle { radius: tokens.cornerRadiusSmall; color: systemTab.highlighted ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.14) : "transparent" }
                    }
                    Button {
                        id: aboutTab
                        objectName: "aboutTabButton"
                        Layout.fillWidth: true
                        Layout.preferredHeight: 56
                        text: qsTr("关于")
                        Accessible.name: text
                        highlighted: tabBar.currentIndex === 3
                        onClicked: tabBar.currentIndex = 3
                        contentItem: ColumnLayout {
                            spacing: 1
                            Image { Layout.alignment: Qt.AlignHCenter; Layout.preferredWidth: 18; Layout.preferredHeight: 18; source: "icons/circle-info.svg"; fillMode: Image.PreserveAspectFit; opacity: aboutTab.highlighted ? 1 : 0.72 }
                            Label { Layout.fillWidth: true; text: aboutTab.text; horizontalAlignment: Text.AlignHCenter; color: aboutTab.highlighted ? Qt.darker(tokens.accent, 1.25) : tokens.textPrimary; font.pixelSize: 10 }
                        }
                        background: Rectangle { radius: tokens.cornerRadiusSmall; color: aboutTab.highlighted ? Qt.rgba(tokens.accent.r, tokens.accent.g, tokens.accent.b, 0.14) : "transparent" }
                    }
                    }
                }

                Label {
                    Layout.fillWidth: true
                    text: qsTr("Remo · Windows")
                    horizontalAlignment: Text.AlignHCenter
                    color: tokens.disabledText
                    font.pixelSize: 10
                }
            }
        }

        StackLayout {
            id: pageStack
            objectName: "pageStack"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.topMargin: titleBar.height
            Layout.bottomMargin: windowSurface.radius
            currentIndex: tabBar.currentIndex

            ConnectionPage { tokens: window.tokens }
            ButtonsPage { id: buttonsPage; tokens: window.tokens }
            SystemPage { tokens: window.tokens }
            AboutPage { tokens: window.tokens }
        }

        // Physical presses animate the mapping page only while that page is
        // already active. Mapping actions themselves remain bridge-owned and
        // continue to work regardless of which settings page is visible.
        Connections {
            target: SettingsController
            function onPhysicalButtonActivated(buttonId) {
                // Keep the latest physical event visible when the user is on
                // another page; switching to Buttons then reveals the last
                // selected key without changing bridge-owned actions.
                buttonsPage.revealButton(buttonId, false)
            }
        }

        Connections {
            target: tabBar
            function onCurrentIndexChanged() {
                const active = tabBar.currentIndex === 1
                SettingsController.setPhysicalButtonFeedbackActive(active)
                if (!active) {
                    buttonsPage.inlineEditorButtonId = ""
                    buttonsPage.activationButtonId = ""
                }
            }
        }

        Component.onCompleted: SettingsController.setPhysicalButtonFeedbackActive(tabBar.currentIndex === 1)

        }

    }
}
