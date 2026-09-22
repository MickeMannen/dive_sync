import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    spacing: 12
    Component.onCompleted: conflictsController.load(mappingController.pairId)

    Card {
        title: "Conflicts"
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 11
            text: "Links with the policy \"Ask me\" record here when both sides hold different values. Pick which side should win; the other side is updated on the service."
        }
        RowLayout {
            Button { text: "Reload"; flat: true; enabled: !conflictsController.busy; onClicked: conflictsController.load(mappingController.pairId) }
            Text { text: conflictsController.message; color: Theme.muted }
        }
        Repeater {
            model: conflictsController.conflicts
            delegate: Rectangle {
                required property var modelData
                Layout.fillWidth: true
                implicitHeight: 52
                radius: 4
                color: "transparent"
                border.color: Theme.border
                RowLayout {
                    anchors { fill: parent; margins: 6 }
                    spacing: 12
                    Text { text: modelData.dive_time; color: Theme.muted; Layout.preferredWidth: 140 }
                    Text { text: modelData.link_id; color: Theme.text; font.bold: true; Layout.preferredWidth: 90 }
                    Text { text: modelData.source_key + "\n" + JSON.stringify(modelData.source_value); color: Theme.text; Layout.preferredWidth: 220; elide: Text.ElideRight }
                    Text { text: modelData.target_key + "\n" + JSON.stringify(modelData.target_value); color: Theme.text; Layout.preferredWidth: 220; elide: Text.ElideRight }
                    Button { text: "Use source"; enabled: !conflictsController.busy; onClicked: conflictsController.resolve(modelData.id, "source") }
                    Button { text: "Use target"; enabled: !conflictsController.busy; onClicked: conflictsController.resolve(modelData.id, "target") }
                }
            }
        }
    }
}
