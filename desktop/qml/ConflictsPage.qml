import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Conflicts of every sync pair, grouped by pair: each shows the dive, the
// field and both sides' values, and a button per side that makes it win.
ColumnLayout {
    spacing: 12
    Component.onCompleted: conflictsController.load()
    // the page is kept alive between visits: reload when shown, so a sync that
    // queued new conflicts meanwhile is reflected
    onVisibleChanged: if (visible) conflictsController.load()

    RowLayout {
        Layout.fillWidth: true
        spacing: 8
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 12
            text: "A rule with the policy \"Fill blanks, ask about real differences\" waits here when both sides hold a different value. Pick the value to keep; the other service is updated."
        }
        Button { text: "Reload"; flat: true; enabled: !conflictsController.busy; onClicked: conflictsController.load() }
    }
    Text { visible: text !== ""; text: conflictsController.message; color: Theme.muted; font.pixelSize: 12 }
    ProgressBar { Layout.fillWidth: true; indeterminate: true; visible: conflictsController.busy }

    Repeater {
        model: conflictsController.groups
        delegate: Card {
            id: group
            required property var modelData
            Layout.fillWidth: true
            title: modelData.label + " - " + modelData.conflicts.length + (modelData.conflicts.length === 1 ? " conflict" : " conflicts")
            Repeater {
                model: group.modelData.conflicts
                delegate: Rectangle {
                    id: item
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: itemColumn.implicitHeight + 16
                    radius: 6
                    color: "transparent"
                    border.color: Theme.border
                    ColumnLayout {
                        id: itemColumn
                        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 8 }
                        spacing: 6
                        RowLayout {
                            spacing: 10
                            Text { text: item.modelData.target_label; color: Theme.text; font.bold: true }
                            Text { text: "dive " + item.modelData.dive_time; color: Theme.muted; font.pixelSize: 11 }
                        }
                        GridLayout {
                            columns: 3
                            columnSpacing: 12
                            rowSpacing: 4
                            Text { text: item.modelData.source_name; color: Theme.muted; font.pixelSize: 11; Layout.preferredWidth: 140 }
                            Text {
                                text: item.modelData.source_text
                                color: Theme.text
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                                Layout.maximumWidth: 520
                            }
                            Button {
                                text: "Keep this"
                                enabled: !conflictsController.busy
                                ToolTip.text: "Writes this value to " + item.modelData.target_name
                                ToolTip.visible: hovered
                                onClicked: conflictsController.resolve(item.modelData.pair_id, item.modelData.id, "source")
                            }
                            Text { text: item.modelData.target_name; color: Theme.muted; font.pixelSize: 11; Layout.preferredWidth: 140 }
                            Text {
                                text: item.modelData.target_text
                                color: Theme.text
                                wrapMode: Text.WordWrap
                                Layout.fillWidth: true
                                Layout.maximumWidth: 520
                            }
                            Button {
                                text: "Keep this"
                                enabled: !conflictsController.busy
                                ToolTip.text: "Writes this value to " + item.modelData.source_name
                                ToolTip.visible: hovered
                                onClicked: conflictsController.resolve(item.modelData.pair_id, item.modelData.id, "target")
                            }
                        }
                    }
                }
            }
        }
    }
}
