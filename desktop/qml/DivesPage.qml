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
        id: detailCard
        title: "Dive data"
        property var sel: controller.selected
        ListModel { id: tankModel }
        function loadTanks() {
            tankModel.clear()
            var list = detailCard.sel.tanks_detail || []
            for (var i = 0; i < list.length; i++) {
                var t = list[i]
                tankModel.append({
                    tank_name: t.tank_name || "",
                    oxygen: (t.oxygen !== undefined && t.oxygen !== null) ? String(t.oxygen) : "",
                    helium: (t.helium !== undefined && t.helium !== null) ? String(t.helium) : "",
                    volume: (t.volume !== undefined && t.volume !== null) ? String(t.volume) : "",
                    start_pressure: (t.start_pressure !== undefined && t.start_pressure !== null) ? String(t.start_pressure) : "",
                    end_pressure: (t.end_pressure !== undefined && t.end_pressure !== null) ? String(t.end_pressure) : ""
                })
            }
        }
        onSelChanged: loadTanks()

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
            LabeledField { id: fWaterTemp; label: "Water temp (°C)"; fieldWidth: 100; text: sel.water_temp_value !== undefined && sel.water_temp_value !== null ? String(sel.water_temp_value) : "" }
            LabeledField { id: fLocation; label: "Location"; fieldWidth: 260; text: sel.location || "" }
            LabeledField { id: fWeight; label: "Weight"; fieldWidth: 100; text: sel.weight || "" }
            LabeledField { id: fVisibility; label: "Visibility"; fieldWidth: 100; text: sel.visibility || "" }
            LabeledField { id: fBuddy; label: "Buddy"; fieldWidth: 160; text: sel.buddy || "" }
            LabeledField { id: fLat; label: "Latitude"; fieldWidth: 100; text: sel.lat !== undefined && sel.lat !== null ? String(sel.lat) : "" }
            LabeledField { id: fLng; label: "Longitude"; fieldWidth: 100; text: sel.lng !== undefined && sel.lng !== null ? String(sel.lng) : "" }
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
        ColumnLayout {
            spacing: 4
            enabled: !!sel.filename
            RowLayout {
                spacing: 8
                Text { text: "Tanks / gases"; color: Theme.muted; font.pixelSize: 11 }
                Text {
                    visible: !controller.tanksEditable
                    text: "(read-only — " + controller.serviceName + " does not accept gas edits)"
                    color: Theme.muted
                    font.pixelSize: 11
                    font.italic: true
                }
            }
            Text { visible: !controller.tanksEditable; text: sel.tanks || "—"; color: Theme.text }
            ColumnLayout {
                visible: controller.tanksEditable
                spacing: 4
                RowLayout {
                    spacing: 6
                    Text { text: "Name"; Layout.preferredWidth: 90; color: Theme.muted; font.pixelSize: 11 }
                    Text { text: "O2 %"; Layout.preferredWidth: 60; color: Theme.muted; font.pixelSize: 11 }
                    Text { text: "He %"; Layout.preferredWidth: 60; color: Theme.muted; font.pixelSize: 11 }
                    Text { text: "Vol (L)"; Layout.preferredWidth: 60; color: Theme.muted; font.pixelSize: 11 }
                    Text { text: "Start (bar)"; Layout.preferredWidth: 70; color: Theme.muted; font.pixelSize: 11 }
                    Text { text: "End (bar)"; Layout.preferredWidth: 70; color: Theme.muted; font.pixelSize: 11 }
                }
                Repeater {
                    model: tankModel
                    RowLayout {
                        spacing: 6
                        required property int index
                        required property string tank_name
                        required property string oxygen
                        required property string helium
                        required property string volume
                        required property string start_pressure
                        required property string end_pressure
                        TextField { Layout.preferredWidth: 90; text: tank_name; onEditingFinished: tankModel.setProperty(index, "tank_name", text) }
                        TextField { Layout.preferredWidth: 60; text: oxygen; onEditingFinished: tankModel.setProperty(index, "oxygen", text) }
                        TextField { Layout.preferredWidth: 60; text: helium; onEditingFinished: tankModel.setProperty(index, "helium", text) }
                        TextField { Layout.preferredWidth: 60; text: volume; onEditingFinished: tankModel.setProperty(index, "volume", text) }
                        TextField { Layout.preferredWidth: 70; text: start_pressure; onEditingFinished: tankModel.setProperty(index, "start_pressure", text) }
                        TextField { Layout.preferredWidth: 70; text: end_pressure; onEditingFinished: tankModel.setProperty(index, "end_pressure", text) }
                        Button { text: "Remove"; onClicked: tankModel.remove(index) }
                    }
                }
                Button {
                    text: "Add tank"
                    onClicked: tankModel.append({tank_name: "", oxygen: "21", helium: "0", volume: "", start_pressure: "", end_pressure: ""})
                }
            }
        }
        RowLayout {
            spacing: 8
            Button {
                text: "Save"
                enabled: !!sel.filename && !controller.busy
                onClicked: {
                    var payload = {
                        dive_number: fDiveNumber.text, date: fDate.text, time: fTime.text, duration: fDuration.text,
                        max_depth: fMaxDepth.text, location: fLocation.text, notes: fNotes.text,
                        weight: fWeight.text, visibility: fVisibility.text, buddy: fBuddy.text,
                        lat: fLat.text, lng: fLng.text, water_temp: fWaterTemp.text
                    }
                    if (controller.tanksEditable) {
                        var tanks = []
                        for (var i = 0; i < tankModel.count; i++) {
                            var row = tankModel.get(i)
                            tanks.push({
                                tank_name: row.tank_name, oxygen: row.oxygen, helium: row.helium,
                                volume: row.volume, start_pressure: row.start_pressure, end_pressure: row.end_pressure
                            })
                        }
                        payload.tanks = tanks
                    }
                    controller.save(payload)
                }
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
