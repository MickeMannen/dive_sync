import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    spacing: 12
    property var pairs: syncController.pairs
    property var directions: syncController.directionsFor(pairBox.currentValue || "")

    Card {
        title: "Sync"
        RowLayout {
            spacing: 16
            ColumnLayout {
                spacing: 2
                Text { text: "Pair"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: pairBox
                    Layout.preferredWidth: 260
                    model: pairs
                    textRole: "label"
                    valueRole: "id"
                    onActivated: directions = syncController.directionsFor(currentValue || "")
                }
            }
            ColumnLayout {
                spacing: 2
                Text { text: "Direction"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: directionBox
                    Layout.preferredWidth: 180
                    model: directions
                    textRole: "label"
                    valueRole: "value"
                }
            }
        }
        RowLayout {
            spacing: 16
            CheckBox { id: dryRun; text: "Dry run" }
            CheckBox { id: onlyNew; text: "Only new dives"; checked: true }
            CheckBox { id: gases; text: "Sync gases"; checked: true }
            CheckBox { id: fit; text: "Sync FIT files" }
        }
        RowLayout {
            spacing: 8
            CheckBox { id: overwrite; text: "Overwrite existing cache" }
            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: Theme.muted
                font.pixelSize: 11
                text: "Download dives fetches and caches raw dive data locally - run this at least once before browsing/editing dives in the Garmin/Divelogs Dives tabs."
            }
        }
        RowLayout {
            spacing: 8
            Button {
                text: "Sync now"
                enabled: !syncController.running
                onClicked: syncController.runSync(dryRun.checked, directionBox.currentValue || "bidirectional",
                                                  onlyNew.checked, gases.checked, fit.checked, pairBox.currentValue || "")
            }
            Button {
                text: "Download dives"
                enabled: !syncController.running
                onClicked: syncController.download(overwrite.checked, "")
            }
            Text { text: syncController.status; color: Theme.muted; Layout.leftMargin: 8 }
        }
        ProgressBar { Layout.fillWidth: true; indeterminate: syncController.running; visible: syncController.running }
        ScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: 300
            TextArea {
                id: logArea
                readOnly: true
                wrapMode: TextEdit.NoWrap
                font.family: "Menlo"
                font.pixelSize: 11
                Connections {
                    target: syncController
                    function onLogLine(line) {
                        logArea.append(line)
                        if (logArea.lineCount > 400) logArea.remove(0, logArea.text.indexOf("\n") + 1)
                    }
                    function onRunningChanged() { if (syncController.running) logArea.clear() }
                }
            }
        }
    }
}
