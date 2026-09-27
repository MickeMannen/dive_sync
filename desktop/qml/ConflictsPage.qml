import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Conflicts of every sync pair, grouped by pair: each shows the dive, the
// field and both sides' values, and a button per side that picks it. Picks
// are staged (Undo takes one back) and written together by Save changes.
// The header with the buttons, progress and message stays put; only the
// list below it scrolls, so the outcome of a pick made far down a long
// queue is always in view.
ColumnLayout {
    id: page
    spacing: 12

    // One side's value: struck through and muted once the other side is picked.
    component ValueText: Text {
        required property string side
        // the enclosing conflict delegate sets this from its own pick
        property string pick: ""
        readonly property bool loser: pick !== "" && pick !== side
        color: loser ? Theme.muted : Theme.text
        font.strikeout: loser
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        Layout.maximumWidth: 520
    }

    // One side's control: Keep this, green once this side is picked; clicking
    // it again undoes the pick. Drawn by hand so the green does not depend on
    // the platform's Qt Quick style.
    component PickCell: AbstractButton {
        id: cell
        required property string side
        required property string other
        property string pick: ""
        property string pairId: ""
        property string conflictId: ""
        readonly property bool chosen: pick === side
        enabled: !conflictsController.busy
        padding: 6
        leftPadding: 12
        rightPadding: 12
        contentItem: Text {
            text: cell.chosen ? "Keeping this ✓" : "Keep this"
            color: cell.chosen ? "white" : (cell.enabled ? Theme.accent : Theme.muted)
            font.pixelSize: 12
            font.bold: cell.chosen
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 5
            color: cell.chosen ? Theme.ok : (cell.hovered ? Qt.alpha(Theme.accent, 0.12) : "transparent")
            border.color: cell.chosen ? Theme.ok : (cell.enabled ? Theme.accent : Theme.border)
            border.width: 1
        }
        Tip {
            text: cell.chosen ? "Staged: " + cell.other + " gets this value when you save. Click again to undo"
                              : "Stages this value for " + cell.other + "; nothing is written until you save"
            visible: cell.hovered
        }
        onClicked: conflictsController.stage(pairId, conflictId, side)
    }

    Component.onCompleted: conflictsController.load()
    // the page is kept alive between visits: reload when shown, so a sync that
    // queued new conflicts meanwhile is reflected
    onVisibleChanged: if (visible) conflictsController.load()

    Card {
        Layout.fillWidth: true
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: Theme.muted
                font.pixelSize: 12
                text: "A rule with the policy \"Fill blanks, ask about real differences\" waits here when both sides hold a different value. Click Keep this on the value to keep (it turns green; click it again to undo). Nothing is written until you press Save for the service that gets the updates."
            }
            Button { text: "Reload"; flat: true; enabled: !conflictsController.busy; onClicked: conflictsController.load() }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            // one Save per service that has picks waiting to be written to it
            Repeater {
                model: conflictsController.pendingServices
                delegate: Button {
                    required property var modelData
                    text: "Save " + modelData.count + (modelData.count === 1 ? " change to " : " changes to ") + modelData.name
                    highlighted: true
                    enabled: !conflictsController.busy
                    Tip { text: "Writes the picked values to " + modelData.name; visible: parent.hovered }
                    onClicked: conflictsController.save(modelData.service)
                }
            }
            Button {
                text: "Discard picks"
                flat: true
                visible: conflictsController.stagedCount > 0
                enabled: !conflictsController.busy
                onClicked: conflictsController.discard()
            }
            Text {
                visible: conflictsController.stagedCount > 0
                text: conflictsController.stagedCount + (conflictsController.stagedCount === 1 ? " pick" : " picks") + " waiting"
                color: Theme.text
                font.bold: true
                font.pixelSize: 12
            }
            Item { Layout.fillWidth: true }
        }
        ProgressBar { Layout.fillWidth: true; indeterminate: true; visible: conflictsController.busy }
        Text { visible: text !== ""; text: conflictsController.message; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
    }

    ScrollView {
        id: listScroll
        Layout.fillWidth: true
        Layout.fillHeight: true
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            width: listScroll.availableWidth
            spacing: 12

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
                            // "source" / "target" while a pick for this conflict waits to be saved
                            property string pick: conflictsController.staged[modelData.id] || ""
                            Layout.fillWidth: true
                            implicitHeight: itemColumn.implicitHeight + 16
                            radius: 6
                            color: "transparent"
                            border.color: pick !== "" ? Theme.accent : Theme.border
                            border.width: pick !== "" ? 2 : 1
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
                                    // source row
                                    Text { text: item.modelData.source_name; color: Theme.muted; font.pixelSize: 11; Layout.preferredWidth: 140 }
                                    ValueText { side: "source"; pick: item.pick; text: item.modelData.source_text }
                                    PickCell { side: "source"; pick: item.pick; pairId: item.modelData.pair_id; conflictId: item.modelData.id; other: item.modelData.target_name }
                                    // target row
                                    Text { text: item.modelData.target_name; color: Theme.muted; font.pixelSize: 11; Layout.preferredWidth: 140 }
                                    ValueText { side: "target"; pick: item.pick; text: item.modelData.target_text }
                                    PickCell { side: "target"; pick: item.pick; pairId: item.modelData.pair_id; conflictId: item.modelData.id; other: item.modelData.source_name }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
