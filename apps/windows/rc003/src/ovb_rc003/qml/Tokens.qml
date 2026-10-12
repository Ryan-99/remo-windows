// Semantic design tokens (XRBM-030 DESIGN_VARIANCE 4 / VISUAL_DENSITY 5):
// colors derived from the OS SystemPalette so light/dark mode is followed
// automatically (no hand-picked dark-mode color set to keep in sync), plus
// fixed corner-radius/spacing/font-size constants shared by every page.
// Instantiated exactly once in main.qml and passed down to each page as a
// `tokens` property - deliberately NOT a pragma Singleton, so this stays a
// plain, implicitly-directory-imported QML type with no module/qmldir
// registration to keep correct under a frozen PyInstaller build.
import QtQuick

QtObject {
    id: tokens

    property SystemPalette palette: SystemPalette {
        colorGroup: SystemPalette.Active
    }

    // The approved HTML prototype is the visual source of truth.  Keep the
    // native controls and OS palette plumbing, but use the same calm light
    // canvas/card hierarchy so the packaged QML page does not fall back to a
    // generic system-grey dashboard.
    property color background: "#f6f8fb"
    property color surface: "#fdfefe"
    property color textPrimary: "#202124"
    property color textSecondary: "#858990"
    property color textTertiary: "#9aa0a8"
    property color disabledText: "#a4a6ab"
    property color accent: "#087cff"
    property color accentText: "#ffffff"
    property color border: "#e3e7ed"
    // Text-input/ComboBox field background (QQC2's "base" palette role) -
    // distinct from `surface` (used for card backgrounds).
    property color fieldBackground: "#f5f5f6"
    property color buttonBackground: "#ffffff"
    property color buttonText: "#202124"

    // Semantic colors: only ever used for a real state (a real save error,
    // a real save/launch success, the fixed identity of the mic hotspot) -
    // never decorative (XRBM-030 Design read: "错误/成功只用于真实状态").
    property color voiceAccent: "#F2914A"
    property color successColor: "#2E7D32"
    property color errorColor: "#C62828"

    // Matches index-v8.html's -apple-system / Segoe UI stack while keeping
    // Chinese glyphs in a UI-optimized sans-serif face on Windows.
    property string fontFamily: "Microsoft YaHei UI"

    property int cornerRadiusSmall: 10
    property int cornerRadiusLarge: 21

    property int spacingTiny: 4
    property int spacingSmall: 8
    property int spacingMedium: 12
    property int spacingLarge: 20

    property int fontSizeSmall: 11
    property int fontSizeBody: 13
    property int fontSizeTitle: 16
    property int fontSizePageTitle: 24
}
