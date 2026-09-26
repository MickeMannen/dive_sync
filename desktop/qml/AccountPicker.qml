import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Which of a service's accounts a page works with (rework.md E19). Only
// shown when there is a choice to make: with one account nothing changes on
// screen. ``current`` comes from the controller, which remembers the pick.
ColumnLayout {
    id: picker
    property string label: ""
    property var accounts: []
    property string current: ""
    signal picked(string account)
    visible: accounts.length > 1
    spacing: 2
    Text { visible: text !== ""; text: picker.label; color: Theme.muted; font.pixelSize: 11 }
    ComboBox {
        id: box
        Layout.preferredWidth: 220
        model: picker.accounts
        currentIndex: Math.max(0, picker.accounts.indexOf(picker.current))
        onActivated: picker.picked(currentText)
        Tip {
            text: "Which " + (picker.label || "account") + " this page works with"
            visible: parent.hovered && picker.label === ""
            delay: 500
        }
    }
}
