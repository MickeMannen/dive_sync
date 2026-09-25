import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: page
    spacing: 12

    function startSync() {
        syncController.runSyncBetween(dryRun.checked, sourceBox.currentValue || "", targetBox.currentValue || "",
                                      onlyNew.checked, gases.checked,
                                      garminAccountBox.currentIndex > 0 ? garminAccountBox.currentText : "",
                                      divelogsAccountBox.currentIndex > 0 ? divelogsAccountBox.currentText : "",
                                      garminCache.checked, mirror.checked)
    }

    Dialog {
        id: mirrorDialog
        objectName: "mirrorDialog"
        width: 480
        title: "Mirror " + sourceBox.currentText + " to " + targetBox.currentText
        modal: true
        standardButtons: Dialog.Yes | Dialog.No
        anchors.centerIn: Overlay.overlay
        Text {
            width: 420
            wrapMode: Text.WordWrap
            color: Theme.text
            text: "Every dive on " + targetBox.currentText + " that " + sourceBox.currentText
                  + " does not have will be DELETED, and mapped fields are overwritten with the source's values. "
                  + "A dry run first shows exactly what would change. Continue?"
        }
        onAccepted: page.startSync()
    }
    // A run syncs from Source into Target and writes the target only; the
    // pair (and its mapping board) behind the two is found either way round.
    property var targets: syncController.targetsFor(sourceBox.currentValue || "")

    Card {
        title: "Sync"
        RowLayout {
            spacing: 16
            ColumnLayout {
                spacing: 2
                Text { text: "Source (read from)"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: sourceBox
                    objectName: "sourceBox"
                    Layout.preferredWidth: 220
                    model: syncController.endpoints
                    textRole: "label"
                    valueRole: "spec"
                    onActivated: {
                        var keep = targetBox.currentValue
                        targets = syncController.targetsFor(currentValue || "")
                        targetBox.currentIndex = Math.max(0, targetBox.indexOfValue(keep))
                    }
                }
            }
            Text { text: "→"; color: Theme.muted; font.pixelSize: 18; Layout.alignment: Qt.AlignBottom; Layout.bottomMargin: 6 }
            ColumnLayout {
                spacing: 2
                Text { text: "Target (written to)"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: targetBox
                    objectName: "targetBox"
                    Layout.preferredWidth: 220
                    model: targets
                    textRole: "label"
                    valueRole: "spec"
                }
            }
            Button {
                text: "⇄"
                flat: true
                Layout.alignment: Qt.AlignBottom
                ToolTip.text: "Swap source and target"
                ToolTip.visible: hovered
                onClicked: {
                    var s = sourceBox.currentValue, t = targetBox.currentValue
                    sourceBox.currentIndex = Math.max(0, sourceBox.indexOfValue(t))
                    targets = syncController.targetsFor(sourceBox.currentValue || "")
                    targetBox.currentIndex = Math.max(0, targetBox.indexOfValue(s))
                }
            }
            // Only Garmin and Divelogs have a list of accounts to choose
            // between; Submersion is one store and Subsurface Cloud one
            // account, so they have nothing to pick and no selector here.
            ColumnLayout {
                spacing: 2
                visible: syncController.garminAccounts.length > 1
                Text { text: "Garmin account"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: garminAccountBox
                    Layout.preferredWidth: 180
                    model: ["Default account"].concat(syncController.garminAccounts)
                }
            }
            ColumnLayout {
                spacing: 2
                visible: syncController.divelogsAccounts.length > 1
                Text { text: "Divelogs account"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: divelogsAccountBox
                    Layout.preferredWidth: 180
                    model: ["Default account"].concat(syncController.divelogsAccounts)
                }
            }
        }
        RowLayout {
            spacing: 16
            CheckBox { id: dryRun; text: "Dry run" }
            CheckBox { id: onlyNew; text: "Only new dives"; checked: true; enabled: !mirror.checked }
            CheckBox { id: gases; text: "Sync gases"; checked: true }
            CheckBox {
                id: mirror
                objectName: "mirrorCheck"
                text: "Mirror: make the target a copy of the source"
                ToolTip.text: "Compares every dive, creates the ones the target is missing, forces every mapped field to the source's value and DELETES every target dive the source does not have. Rules marked 'never overwrite' are left alone. Without this, a sync never deletes anything."
                ToolTip.visible: hovered
                ToolTip.delay: 300
            }
            CheckBox {
                id: garminCache
                text: "Use cached Garmin dives"
                checked: true
                ToolTip.text: "Garmin is slow (three requests per dive), so a dive whose entry in Garmin's list is unchanged is read from the local copy instead of downloaded again - for syncing and for Download dives. Untick it to fetch every Garmin dive again, e.g. when testing or after editing only a dive's notes, buddy, weight or visibility on Garmin (not visible in that list). Other services are always downloaded in full."
                ToolTip.visible: hovered
                ToolTip.delay: 500
            }
        }
        RowLayout {
            spacing: 8
            Button {
                text: "Sync now"
                enabled: !syncController.running
                onClicked: {
                    if (mirror.checked && !dryRun.checked) mirrorDialog.open()
                    else page.startSync()
                }
            }
            Text { text: syncController.status; color: Theme.muted; Layout.leftMargin: 8; Layout.fillWidth: true; elide: Text.ElideRight }
        }
        ProgressBar { Layout.fillWidth: true; indeterminate: syncController.running; visible: syncController.running }
    }

    Card {
        title: "Download dives"
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 13
            text: "Stores each service's dives on this computer, for its Dives page - run it once per service before browsing or editing there. Garmin reuses unchanged dives while \"Use cached Garmin dives\" is ticked above; every other service is downloaded in full."
        }
        RowLayout {
            spacing: 8
            ComboBox {
                id: downloadServiceBox
                Layout.preferredWidth: 220
                textRole: "label"
                valueRole: "id"
                // "" downloads every configured service.
                model: [{ id: "", label: "All configured services" }].concat(syncController.services)
            }
            Button {
                text: "Download dives"
                enabled: !syncController.running
                onClicked: syncController.download(!garminCache.checked, downloadServiceBox.currentValue || "")
            }
        }
    }

    Card {
        title: "Log"
        headerContent: [
            Item { Layout.fillWidth: true },
            Button { text: "Clear"; flat: true; onClicked: { logArea.lines = []; logArea.clear() } }
        ]
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 320
            color: "transparent"
            radius: 4
            border.color: Theme.border
        ScrollView {
            anchors { fill: parent; margins: 4 }
            TextArea {
                id: logArea
                readOnly: true
                wrapMode: TextEdit.NoWrap
                font.family: "Menlo"
                font.pixelSize: 11
                // Newest line first, so the run's latest state is in view
                // without scrolling. Kept as a plain JS list because the text
                // is rebuilt top-down on every line; the oldest drops off the
                // bottom once the window is full.
                property var lines: []
                Connections {
                    target: syncController
                    function onLogLine(line) {
                        logArea.lines.unshift(line)
                        if (logArea.lines.length > 400) logArea.lines.pop()
                        logArea.text = logArea.lines.join("\n")
                    }
                    function onRunningChanged() {
                        if (syncController.running) { logArea.lines = []; logArea.clear() }
                    }
                }
            }
        }
        }
    }
}
