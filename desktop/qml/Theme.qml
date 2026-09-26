pragma Singleton
import QtQuick

QtObject {
    readonly property bool dark: Qt.styleHints.colorScheme === Qt.Dark
    readonly property color bg: dark ? "#16181c" : "#f5f6f8"
    readonly property color card: dark ? "#1f2226" : "#ffffff"
    readonly property color border: dark ? "#33373d" : "#dcdfe4"
    readonly property color text: dark ? "#e6e8eb" : "#1c1e21"
    readonly property color muted: dark ? "#9aa0a8" : "#6b7280"
    readonly property color accent: "#0ea5a4"
    readonly property color danger: "#dc2626"
    readonly property color sidebar: "#1f2937"
    readonly property color sidebarText: "#e5e7eb"
    readonly property color navActive: "#0ea5a4"
    // Tooltips (Tip.qml): solid and high-contrast in both schemes
    readonly property color tipBg: dark ? "#2d3239" : "#1f2937"
    readonly property color tipText: dark ? "#f3f4f6" : "#f9fafb"
    readonly property color tipBorder: dark ? "#4b5563" : "#1f2937"
    readonly property int radius: 8
    readonly property int pad: 14
}
