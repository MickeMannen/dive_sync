import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: page
    required property var controller
    spacing: 12
    property int selectedRow: -1

    Component.onCompleted: controller.load()

    Card {
        title: controller.serviceName + " dives"
        RowLayout {
            spacing: 8
            Button { text: "Refresh"; enabled: !controller.busy; onClicked: controller.refresh() }
            Button { text: "Columns…"; onClicked: columnDialog.open() }
            ColumnLayout {
                spacing: 2
                Text { text: "Sort by"; color: Theme.muted; font.pixelSize: 11 }
                RowLayout {
                    ComboBox {
                        id: sortBox
                        Layout.preferredWidth: 150
                        model: controller.allColumns
                        textRole: "label"
                        valueRole: "key"
                        Component.onCompleted: currentIndex = Math.max(0, indexOfValue(controller.sortKey))
                        onActivated: controller.setSort(currentValue, controller.sortAscending)
                    }
                    Button {
                        text: controller.sortAscending ? "▲" : "▼"
                        onClicked: controller.setSort(controller.sortKey, !controller.sortAscending)
                    }
                }
            }
            Text { text: controller.listStatus; color: Theme.muted; Layout.fillWidth: true; elide: Text.ElideRight }
        }
        ProgressBar { Layout.fillWidth: true; indeterminate: true; visible: controller.busy }
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 300
            color: "transparent"
            border.color: Theme.border
            radius: 6
            clip: true
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 1
                spacing: 0
                HorizontalHeaderView {
                    id: header
                    Layout.fillWidth: true
                    syncView: table
                    clip: true
                }
                TableView {
                    id: table
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    model: controller.model
                    columnWidthProvider: function (column) { return controller.model.columnWidth(column) }
                    selectionModel: ItemSelectionModel { model: controller.model }
                    delegate: Rectangle {
                        implicitWidth: 100
                        implicitHeight: 26
                        color: row === page.selectedRow ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.25) : (row % 2 ? Theme.bg : Theme.card)
                        Text {
                            anchors { fill: parent; leftMargin: 6; rightMargin: 6 }
                            verticalAlignment: Text.AlignVCenter
                            text: display
                            color: Theme.text
                            font.pixelSize: 12
                            elide: Text.ElideRight
                        }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: { page.selectedRow = row; controller.select(row) }
                        }
                    }
                    Connections {
                        target: controller.model
                        function onModelReset() { page.selectedRow = -1 }
                    }
                }
            }
        }
        Text { text: controller.status; color: Theme.muted }
    }

    Card {
        title: "Dive data"
        property var sel: controller.selected
        Text {
            text: sel.filename ? (sel.date_time + " — " + (sel.location || "Unnamed dive")) : "No dive selected"
            color: Theme.text
            font.bold: true
        }
        GridLayout {
            columns: 4
            columnSpacing: 12
            rowSpacing: 6
            enabled: !!sel.filename
            LabeledField { id: fDiveNumber; label: "Dive #"; fieldWidth: 90; text: sel.dive_number || "" }
            LabeledField { id: fDate; label: "Date"; fieldWidth: 120; text: sel.date || "" }
            LabeledField { id: fTime; label: "Time"; fieldWidth: 100; text: sel.time || "" }
            LabeledField { id: fDuration; label: "Duration (min)"; fieldWidth: 100; text: sel.duration || "" }
            LabeledField { id: fMaxDepth; label: "Max depth (m)"; fieldWidth: 100; text: sel.max_depth || "" }
            LabeledField { label: "Avg depth (m)"; fieldWidth: 100; readOnly: true; text: sel.avg_depth || "" }
            LabeledField { label: "Water temp"; fieldWidth: 120; readOnly: true; text: sel.water_temp || "" }
            LabeledField { id: fLocation; label: "Location"; fieldWidth: 260; text: sel.location || "" }
            LabeledField { id: fWeight; label: "Weight"; fieldWidth: 100; text: sel.weight || "" }
            LabeledField { id: fVisibility; label: "Visibility"; fieldWidth: 100; text: sel.visibility || "" }
            LabeledField { id: fBuddy; label: "Buddy"; fieldWidth: 160; text: sel.buddy || "" }
            LabeledField { label: "Tanks"; fieldWidth: 320; readOnly: true; text: sel.tanks || "" }
        }
        ColumnLayout {
            spacing: 2
            enabled: !!sel.filename
            Text { text: "Notes"; color: Theme.muted; font.pixelSize: 11 }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: 80
                TextArea { id: fNotes; text: sel.notes || ""; wrapMode: TextEdit.Wrap; selectByMouse: true }
            }
        }
        RowLayout {
            spacing: 8
            Button {
                text: "Save"
                enabled: !!sel.filename && !controller.busy
                onClicked: controller.save({
                    dive_number: fDiveNumber.text, date: fDate.text, time: fTime.text, duration: fDuration.text,
                    max_depth: fMaxDepth.text, location: fLocation.text, notes: fNotes.text,
                    weight: fWeight.text, visibility: fVisibility.text, buddy: fBuddy.text
                })
            }
            Button {
                text: "Delete"
                enabled: !!sel.filename && !controller.busy
                onClicked: deleteDialog.open()
            }
        }
    }

    Dialog {
        id: deleteDialog
        title: "Delete dive"
        modal: true
        standardButtons: Dialog.Yes | Dialog.No
        anchors.centerIn: Overlay.overlay
        Text { text: "Delete this dive from " + controller.serviceName + " (removes the local cache file and the remote dive)?"; color: Theme.text; wrapMode: Text.WordWrap; width: 360 }
        onAccepted: controller.deleteSelected()
    }

    Dialog {
        id: columnDialog
        title: "Visible columns"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        anchors.centerIn: Overlay.overlay
        property var chosen: []
        onOpened: chosen = controller.visibleColumns.slice()
        ColumnLayout {
            Repeater {
                model: controller.allColumns
                CheckBox {
                    required property var modelData
                    text: modelData.label
                    checked: columnDialog.chosen.indexOf(modelData.key) >= 0
                    onToggled: {
                        var list = columnDialog.chosen.slice()
                        var i = list.indexOf(modelData.key)
                        if (checked && i < 0) list.push(modelData.key)
                        if (!checked && i >= 0) list.splice(i, 1)
                        columnDialog.chosen = list
                    }
                }
            }
        }
        onAccepted: controller.setVisibleColumns(chosen)
    }
}
