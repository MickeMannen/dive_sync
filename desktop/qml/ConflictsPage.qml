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

    // One side's control: Keep this while nothing is picked, a check mark
    // and Undo once this side is.
    component PickCell: RowLayout {
        required property string side
        required property string other
        property string pick: ""
        property string pairId: ""
        property string conflictId: ""
        readonly property bool chosen: pick === side
        spacing: 6
        Text { visible: chosen; text: "✓ Keeping this"; color: Theme.accent; font.bold: true; font.pixelSize: 12 }
        Button {
            visible: pick === ""
            text: "Keep this"
            enabled: !conflictsController.busy
            Tip { text: "Stages this value for " + other + "; nothing is written until Save changes"; visible: parent.hovered }
            onClicked: conflictsController.stage(pairId, conflictId, side)
        }
        Button {
            visible: chosen
            text: "Undo"
            flat: true
            enabled: !conflictsController.busy
            Tip { text: "Take this pick back"; visible: parent.hovered }
            onClicked: conflictsController.unstage(conflictId)
        }
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
                text: "A rule with the policy \"Fill blanks, ask about real differences\" waits here when both sides hold a different value. Pick the value to keep on each conflict (Undo takes a pick back), then Save changes: the other service is updated for every pick."
            }
            Button { text: "Reload"; flat: true; enabled: !conflictsController.busy; onClicked: conflictsController.load() }
        }
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Button {
                text: "Save changes"
                highlighted: true
                enabled: !conflictsController.busy && conflictsController.stagedCount > 0
                onClicked: conflictsController.save()
            }
            Button {
                text: "Discard picks"
                flat: true
                enabled: !conflictsController.busy && conflictsController.stagedCount > 0
                onClicked: conflictsController.discard()
            }
            Text {
                visible: conflictsController.stagedCount > 0
                text: conflictsController.stagedCount + (conflictsController.stagedCount === 1 ? " pick" : " picks") + " waiting to be saved"
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
