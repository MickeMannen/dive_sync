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
            text: detailCard.sel.filename ? (detailCard.sel.date_time + " — " + (detailCard.sel.location || "Unnamed dive")) : "No dive selected"
            color: Theme.text
            font.bold: true
        }
        GridLayout {
            columns: 4
            columnSpacing: 12
            rowSpacing: 6
            enabled: !!detailCard.sel.filename
            LabeledField { id: fDiveNumber; label: "Dive #"; fieldWidth: 90; text: detailCard.sel.dive_number || "" }
            LabeledField { id: fDate; label: "Date"; fieldWidth: 120; text: detailCard.sel.date || "" }
            LabeledField { id: fTime; label: "Time"; fieldWidth: 100; text: detailCard.sel.time || "" }
            LabeledField { id: fDuration; label: "Duration (min)"; fieldWidth: 100; text: detailCard.sel.duration || "" }
            LabeledField { id: fMaxDepth; label: "Max depth (m)"; fieldWidth: 100; text: detailCard.sel.max_depth || "" }
            LabeledField { label: "Avg depth (m)"; fieldWidth: 100; readOnly: true; text: detailCard.sel.avg_depth || "" }
            LabeledField { id: fWaterTemp; label: "Water temp (°C)"; fieldWidth: 100; text: detailCard.sel.water_temp_value !== undefined && detailCard.sel.water_temp_value !== null ? String(detailCard.sel.water_temp_value) : "" }
            LabeledField { id: fLocation; label: "Location"; fieldWidth: 260; text: detailCard.sel.location || "" }
            LabeledField { id: fWeight; label: "Weight"; fieldWidth: 100; text: detailCard.sel.weight || "" }
            LabeledField { id: fVisibility; label: "Visibility"; fieldWidth: 100; text: detailCard.sel.visibility || "" }
            LabeledField { id: fBuddy; label: "Buddy"; fieldWidth: 160; text: detailCard.sel.buddy || "" }
            LabeledField { id: fLat; label: "Latitude"; fieldWidth: 100; text: detailCard.sel.lat !== undefined && detailCard.sel.lat !== null ? String(detailCard.sel.lat) : "" }
            LabeledField { id: fLng; label: "Longitude"; fieldWidth: 100; text: detailCard.sel.lng !== undefined && detailCard.sel.lng !== null ? String(detailCard.sel.lng) : "" }
        }
        ColumnLayout {
            spacing: 2
            enabled: !!detailCard.sel.filename
            Text { text: "Notes"; color: Theme.muted; font.pixelSize: 11 }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: 80
                TextArea { id: fNotes; text: detailCard.sel.notes || ""; wrapMode: TextEdit.Wrap; selectByMouse: true }
            }
        }
        ColumnLayout {
            spacing: 4
            enabled: !!detailCard.sel.filename
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
            Text { visible: !controller.tanksEditable; text: detailCard.sel.tanks || "—"; color: Theme.text }
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
        ColumnLayout {
            id: profileSection
            spacing: 4
            visible: (detailCard.sel.samples || []).length > 1
            function hasTemperature(samples) {
                for (var i = 0; i < samples.length; i++) {
                    if (samples[i].temp !== null && samples[i].temp !== undefined) return true
                }
                return false
            }
            RowLayout {
                spacing: 12
                Text { text: "Depth profile"; color: Theme.muted; font.pixelSize: 11 }
                Text { text: "● depth"; color: Theme.accent; font.pixelSize: 11 }
                Text { text: "● temperature"; color: "#e07b39"; font.pixelSize: 11; visible: profileSection.hasTemperature(detailCard.sel.samples || []) }
            }
            Canvas {
                id: profileCanvas
                Layout.fillWidth: true
                Layout.preferredHeight: 160
                onWidthChanged: requestPaint()
                onHeightChanged: requestPaint()
                Connections { target: detailCard; function onSelChanged() { profileCanvas.requestPaint() } }
                onPaint: {
                    var ctx = getContext("2d")
                    ctx.clearRect(0, 0, width, height)
                    var samples = detailCard.sel.samples || []
                    if (samples.length < 2 || width <= 0 || height <= 0) return

                    var pad = { left: 40, right: 44, top: 10, bottom: 20 }
                    var plotW = width - pad.left - pad.right
                    var plotH = height - pad.top - pad.bottom
                    if (plotW <= 0 || plotH <= 0) return

                    var maxTime = samples[samples.length - 1].time || 0
                    var maxDepth = 0
                    var temps = []
                    for (var i = 0; i < samples.length; i++) {
                        if (samples[i].depth > maxDepth) maxDepth = samples[i].depth
                        if (samples[i].temp !== null && samples[i].temp !== undefined) temps.push(samples[i].temp)
                    }
                    if (maxDepth <= 0) maxDepth = 1
                    if (maxTime <= 0) maxTime = 1
                    var minTemp = temps.length ? Math.min.apply(null, temps) : 0
                    var maxTemp = temps.length ? Math.max.apply(null, temps) : 0
                    if (maxTemp === minTemp) { maxTemp += 1; minTemp -= 1 }

                    function xAt(t) { return pad.left + (t / maxTime) * plotW }
                    function yDepth(d) { return pad.top + (d / maxDepth) * plotH }
                    function yTemp(t) { return pad.top + (1 - (t - minTemp) / (maxTemp - minTemp)) * plotH }

                    ctx.strokeStyle = Theme.border
                    ctx.lineWidth = 1
                    ctx.beginPath()
                    ctx.moveTo(pad.left, pad.top)
                    ctx.lineTo(pad.left, pad.top + plotH)
                    ctx.lineTo(pad.left + plotW, pad.top + plotH)
                    ctx.stroke()

                    ctx.strokeStyle = Theme.accent
                    ctx.lineWidth = 1.5
                    ctx.beginPath()
                    for (var j = 0; j < samples.length; j++) {
                        var x = xAt(samples[j].time || 0)
                        var y = yDepth(samples[j].depth)
                        if (j === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                    }
                    ctx.stroke()

                    if (temps.length > 1) {
                        ctx.strokeStyle = "#e07b39"
                        ctx.lineWidth = 1.5
                        ctx.beginPath()
                        var started = false
                        for (var k = 0; k < samples.length; k++) {
                            var s = samples[k]
                            if (s.temp === null || s.temp === undefined) continue
                            var tx = xAt(s.time || 0)
                            var ty = yTemp(s.temp)
                            if (!started) { ctx.moveTo(tx, ty); started = true } else ctx.lineTo(tx, ty)
                        }
                        ctx.stroke()
                    }

                    ctx.fillStyle = Theme.muted
                    ctx.font = "10px sans-serif"
                    ctx.fillText("0 m", 4, pad.top + 8)
                    ctx.fillText(maxDepth.toFixed(1) + " m", 4, pad.top + plotH)
                    ctx.fillText(Math.round(maxTime / 60) + " min", pad.left + plotW - 26, height - 4)
                    if (temps.length) {
                        ctx.fillStyle = "#e07b39"
                        ctx.fillText(maxTemp.toFixed(1) + "°", width - pad.right + 4, pad.top + 8)
                        ctx.fillText(minTemp.toFixed(1) + "°", width - pad.right + 4, pad.top + plotH)
                    }
                }
            }
        }
        RowLayout {
            spacing: 8
            Button {
                text: "Save"
                enabled: !!detailCard.sel.filename && !controller.busy
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
                enabled: !!detailCard.sel.filename && !controller.busy
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
