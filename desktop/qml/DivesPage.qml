import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: page
    objectName: "divesPage-" + controller.serviceName
    required property var controller
    spacing: 12
    property int selectedRow: -1
    // Subsurface: a recorded dive's duration and depths come from its profile
    readonly property bool profileLocked: controller.profileLocksDepths && detailCard.sel.has_profile === true
    readonly property color fitOk: "#16a34a"
    // Coordinates at 6 decimals (~0.1 m) rather than Garmin's full float tail;
    // saving the rounded text leaves the stored coordinate as it was.
    function coordText(value) {
        if (value === undefined || value === null || value === "") return ""
        return String(Number(Number(value).toFixed(6)))
    }

    Component.onCompleted: controller.load()

    // Edits are staged, not uploaded on the spot: moving to another dive (or
    // saving) keeps what the form holds when it differs from what it showed
    // on arrival, and the controller uploads staged dives on Save / Save all.
    function formPayload() {
        var payload = {
            dive_number: fDiveNumber.text, date: fDate.text, time: fTime.text, duration: fDuration.text,
            max_depth: fMaxDepth.text, location: fLocation.text, activity_name: fActivityName.text,
            location_name: fLocationName.text, notes: fNotes.text,
            weight: fWeight.text, visibility: fVisibility.text, buddy: fBuddy.text,
            lat: fLat.text, lng: fLng.text, water_temp: fWaterTemp.text
        }
        if (controller.tanksEditable) {
            var tanks = []
            for (var i = 0; i < tankModel.count; i++) {
                var t = tankModel.get(i)
                tanks.push({ tank_name: t.tank_name, oxygen: t.oxygen, helium: t.helium,
                             volume: t.volume, start_pressure: t.start_pressure, end_pressure: t.end_pressure })
            }
            payload.tanks = tanks
        }
        return payload
    }
    function stageCurrent() {
        var filename = detailCard.sel.filename
        if (!filename || detailCard.formSnapshot === "") return
        if (JSON.stringify(formPayload()) !== detailCard.formSnapshot) controller.stage(filename, formPayload())
    }
    function isDeleting(row) {
        var r = controller.model.row(row)
        return !!r && controller.deletingFiles.indexOf(r.filename) >= 0
    }
    function isPending(row) {
        var r = controller.model.row(row)
        return !!r && controller.pendingFiles.indexOf(r.filename) >= 0
    }
    Connections {
        target: controller
        // the highlighted row follows the controller's selection, also after a
        // save or a sort reloads the table
        function onSelectedChanged() { page.selectedRow = controller.rowOf(controller.selected.filename || "") }
    }

    Card {
        // Fixed height on purpose: a taller window gives all of its extra
        // room to the dive form below, which is the part that runs out of
        // space. Only the elastic Location column grows with the width.
        Layout.preferredHeight: 380
        Layout.minimumHeight: 160
        title: controller.serviceName + " dives (" + controller.diveCount + ")"
        // Sorting is a click on a column heading and the column picker is a
        // right-click on the header row, so the only control left here is
        // Refresh - which belongs next to the title rather than on a toolbar
        // row of its own.
        headerContent: [
            // This page's own account (rework.md E19); hidden with only one.
            AccountPicker {
                objectName: "divesAccount-" + controller.serviceName
                accounts: controller.accounts
                current: controller.account
                enabled: !controller.busy
                onPicked: (account) => controller.setAccount(account)
            },
            Button {
                text: "Refresh"
                enabled: !controller.busy
                Tip {
                    text: controller.fitSupported
                          ? "Fetches new and changed dives; dives already on this computer are left alone"
                          : "Downloads every dive from " + controller.serviceName
                    visible: parent.hovered
                }
                onClicked: controller.refresh()
            },
            Button {
                text: "Full refresh"
                flat: true
                // only Garmin reuses unchanged dives; every other refresh is full already
                visible: controller.fitSupported
                enabled: !controller.busy
                Tip {
                    text: "Re-fetches every dive" + (controller.fitSupported ? " and downloads each device dive's FIT file again, replacing the saved one" : "") + ". Needed after editing only a dive's notes, buddy, weight or visibility on Garmin, which the activity listing does not reveal."
                    visible: parent.hovered
                }
                onClicked: controller.refreshAll()
            },
            Button {
                id: fitButton
                text: "FIT files ▾"
                flat: true
                visible: controller.fitSupported
                enabled: !controller.busy
                onClicked: fitMenu.open()
                Menu {
                    id: fitMenu
                    y: fitButton.height
                    MenuItem {
                        text: "Download this dive's FIT"
                        enabled: page.selectedRow >= 0 && !controller.selected.manual
                        onTriggered: controller.downloadFit()
                    }
                    MenuItem {
                        text: "Download all missing FITs (✗ in the table)"
                        onTriggered: controller.downloadMissingFits()
                    }
                }
            },
            Button {
                text: "Stop"
                visible: controller.stoppable
                onClicked: controller.stop()
                Tip {
                    text: "Stops after the dive being fetched now; what has arrived so far is kept"
                    visible: parent.hovered
                }
            },
            Text { text: controller.listStatus; color: Theme.muted; elide: Text.ElideRight; Layout.maximumWidth: 360 },
            Item { Layout.fillWidth: true },
            Text {
                visible: controller.pendingCount > 0
                text: (controller.pendingCount === 1 ? "1 unsaved change" : controller.pendingCount + " unsaved changes")
                      + (controller.deletingFiles.length > 0 ? " (" + controller.deletingFiles.length + " to delete)" : "")
                color: Theme.accent
                font.pixelSize: 12
            },
            Button {
                text: controller.pendingCount > 0 ? "Save all changes (" + controller.pendingCount + ")" : "Save all changes"
                enabled: !controller.busy && (controller.pendingCount > 0 || !!detailCard.sel.filename)
                Tip {
                    text: "Uploads every dive with unsaved changes to " + controller.serviceName + " in one go"
                    visible: parent.hovered
                }
                onClicked: {
                    page.stageCurrent()
                    if (controller.deletingFiles.length > 0) saveAllDialog.open()
                    else controller.saveAll()
                }
            }
        ]
        // A Garmin refresh is three API calls per dive with a cool-down between
        // each, so it runs for minutes: show how far it has got rather than an
        // anonymous spinner (rework.md E14). Indeterminate until the adapter
        // knows how many dives it is fetching.
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 2
            visible: controller.busy
            ProgressBar {
                Layout.fillWidth: true
                indeterminate: controller.progressFraction < 0
                from: 0; to: 1
                value: Math.max(0, controller.progressFraction)
            }
            Text {
                Layout.fillWidth: true
                text: controller.progressText
                visible: text !== ""
                color: Theme.muted
                font.pixelSize: 11
                elide: Text.ElideRight
            }
        }
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 120
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
                    // Click a heading to sort by it, click it again to flip
                    // the direction - the arrow marks which column is active.
                    // A per-cell delegate rather than one MouseArea over the
                    // whole header: no mapping from x back to a column, and
                    // the arrow lands in the right cell for free.
                    delegate: Rectangle {
                        required property int index
                        required property var display
                        readonly property string key: controller.model.columnKey(index)
                        readonly property bool active: key === controller.sortKey
                        implicitHeight: 28
                        color: headerMouse.containsMouse ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.12)
                                                         : Theme.card
                        border.color: Theme.border
                        RowLayout {
                            anchors { fill: parent; leftMargin: 6; rightMargin: 6 }
                            spacing: 3
                            Text {
                                Layout.fillWidth: true
                                text: display
                                color: Theme.text
                                font.pixelSize: 11
                                font.bold: active
                                elide: Text.ElideRight
                            }
                            Text {
                                visible: active
                                text: controller.sortAscending ? "▲" : "▼"
                                color: Theme.accent
                                font.pixelSize: 9
                            }
                        }
                        MouseArea {
                            id: headerMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                            onClicked: function (mouse) {
                                if (mouse.button === Qt.RightButton)
                                    columnDialog.open()
                                else
                                    controller.toggleSort(key)
                            }
                            Tip {
                                text: "Click to sort · right-click to choose columns"
                                visible: parent.containsMouse
                                delay: 600
                            }
                        }
                    }
                }
                TableView {
                    id: table
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    model: controller.model
                    // The fixed columns keep their widths; Location takes what
                    // is left, so the row spans the window at any size rather
                    // than only near the one it was laid out for. Below its
                    // minimum the table scrolls horizontally as before.
                    columnWidthProvider: function (column) {
                        if (!controller.model.isElasticColumn(column))
                            return controller.model.columnWidth(column)
                        return Math.max(controller.model.elasticMinWidth(),
                                        table.width - controller.model.fixedColumnsWidth())
                    }
                    // Widths are read once per layout pass, so a resize or a
                    // change to the visible columns has to ask for a new one.
                    onWidthChanged: Qt.callLater(table.forceLayout)
                    selectionModel: ItemSelectionModel { model: controller.model }
                    delegate: Rectangle {
                        implicitWidth: 100
                        implicitHeight: 26
                        color: row === page.selectedRow ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.25) : (row % 2 ? Theme.bg : Theme.card)
                        Text {
                            anchors { fill: parent; leftMargin: 6; rightMargin: 6 }
                            verticalAlignment: Text.AlignVCenter
                            readonly property bool pending: column === 0 && controller.pendingFiles.length > 0 && page.isPending(row)
                            readonly property bool deleting: controller.deletingFiles.length > 0 && page.isDeleting(row)
                            readonly property bool fitCell: controller.model.columnKey(column) === "fit"
                            text: pending ? (deleting ? "🗑 " : "✎ ") + display : display
                            font.italic: pending
                            font.strikeout: deleting
                            opacity: deleting ? 0.6 : 1
                            // FIT column: see the legend under the table
                            color: !fitCell ? Theme.text : display === "✓" ? page.fitOk : display === "✗" ? Theme.danger : Theme.muted
                            font.bold: fitCell
                            horizontalAlignment: fitCell ? Text.AlignHCenter : Text.AlignLeft
                            font.pixelSize: 12
                            elide: Text.ElideRight
                        }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: { page.stageCurrent(); page.selectedRow = row; controller.select(row) }
                        }
                    }
                    Connections {
                        target: controller.model
                        // dives listed while a refresh runs move the selected one down
                        function onRowsInserted() { page.selectedRow = controller.rowOf(controller.selected.filename || "") }
                        function onRowsRemoved() { page.selectedRow = controller.rowOf(controller.selected.filename || "") }
                        function onModelReset() {
                            page.selectedRow = controller.rowOf(controller.selected.filename || "")
                            // set_columns() resets the model, and the leftover
                            // width depends on which columns are visible.
                            Qt.callLater(table.forceLayout)
                        }
                    }
                }
            }
        }
        RowLayout {
            spacing: 14
            Text { text: "Legend:"; color: Theme.muted; font.pixelSize: 11 }
            Text { text: "✎ unsaved changes"; color: Theme.accent; font.pixelSize: 11 }
            Text { text: "🗑 marked for deletion"; color: Theme.danger; font.pixelSize: 11 }
            Text { visible: controller.fitSupported; text: "✓ FIT downloaded"; color: page.fitOk; font.pixelSize: 11; font.bold: true }
            Text { visible: controller.fitSupported; text: "✗ FIT not downloaded"; color: Theme.danger; font.pixelSize: 11; font.bold: true }
            Text { visible: controller.fitSupported; text: "M hand-logged dive (FIT made up by Garmin Connect)"; color: Theme.muted; font.pixelSize: 11; font.bold: true }
        }
        Text { text: controller.status; color: Theme.muted }
    }

    // The dive form is the one part of this page that is taller than the
    // window, so it is the one part that scrolls. The page itself is sized to
    // the viewport (see main.qml), so there is no second scrollbar around the
    // whole thing fighting this one.
    ScrollView {
        id: detailScroll
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.preferredHeight: 260
        Layout.minimumHeight: 180
        contentWidth: availableWidth
        clip: true
        // Smooth wheel/trackpad scrolling: no rubber-band bounce at the ends
        // and whole-pixel positions, so text does not shimmer while it moves.
        Component.onCompleted: {
            var flick = detailScroll.contentItem
            flick.boundsBehavior = Flickable.StopAtBounds
            flick.pixelAligned = true
        }

        ColumnLayout {
            width: detailScroll.availableWidth

            Card {
                id: detailCard
                title: "Dive data"
                property var sel: controller.selected
                // what the form showed on arrival, to tell an edit apart (see page.stageCurrent)
                property string formSnapshot: ""
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
                onSelChanged: {
                    loadTanks()
                    formSnapshot = ""
                    Qt.callLater(function () { detailCard.formSnapshot = detailCard.sel.filename ? JSON.stringify(page.formPayload()) : "" })
                }

                RowLayout {
                    spacing: 8
                    Text {
                        text: detailCard.sel.filename ? (detailCard.sel.date_time + " — " + ((controller.splitSiteNames ? detailCard.sel.activity_name : detailCard.sel.location) || "Unnamed dive")) : "No dive selected"
                        color: Theme.text
                        font.bold: true
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        readonly property bool deleting: !!detailCard.sel.filename && controller.deletingFiles.indexOf(detailCard.sel.filename) >= 0
                        visible: !!detailCard.sel.filename && controller.pendingFiles.indexOf(detailCard.sel.filename) >= 0
                        text: deleting ? "🗑 marked for deletion (Undo keeps it)" : "✎ unsaved changes"
                        color: deleting ? Theme.danger : Theme.accent
                        font.pixelSize: 11
                    }
                    Button {
                        text: "Save"
                        enabled: !!detailCard.sel.filename && !controller.busy
                        Tip {
                            text: controller.saveStagesOnly
                                  ? "Keeps this dive's changes; Save all changes sends every kept dive to " + controller.serviceName
                                  : "Uploads this dive's changes to " + controller.serviceName
                            visible: parent.hovered
                        }
                        onClicked: { page.stageCurrent(); controller.saveDive(detailCard.sel.filename) }
                    }
                    Button {
                        text: "Undo"
                        flat: true
                        enabled: !!detailCard.sel.filename && !controller.busy
                        Tip {
                            text: "Drops this dive's unsaved changes and shows its stored values again"
                            visible: parent.hovered
                        }
                        onClicked: controller.discard(detailCard.sel.filename)
                    }
                    Button {
                        text: "Delete"
                        flat: true
                        enabled: !!detailCard.sel.filename && !controller.busy
                        onClicked: deleteDialog.open()
                    }
                }
                Text {
                    visible: controller.fitSupported && !!detailCard.sel.filename
                    text: detailCard.sel.manual ? "FIT file: none (hand-logged dive)"
                          : (detailCard.sel.fit_file ? "FIT file: downloaded" : "FIT file: not downloaded yet (FIT files ▾ in the list above)")
                    color: Theme.muted
                    font.pixelSize: 11
                }
                // A regular grid: when and which dive, then depths and conditions,
                // then names, then people and position. Wide fields span two columns.
                GridLayout {
                    columns: 4
                    columnSpacing: 16
                    rowSpacing: 8
                    enabled: !!detailCard.sel.filename
                    LabeledField { id: fDate; label: "Date"; fieldWidth: 150; text: detailCard.sel.date || ""; onEditingFinished: page.stageCurrent() }
                    LabeledField { id: fTime; label: "Time"; fieldWidth: 150; text: detailCard.sel.time || ""; onEditingFinished: page.stageCurrent() }
                    LabeledField {
                        id: fDiveNumber
                        // Divelogs numbers its own dives by date/time order.
                        label: controller.diveNumberEditable ? "Dive #" : "Dive # (set by the service)"
                        fieldWidth: 150
                        readOnly: !controller.diveNumberEditable
                        text: detailCard.sel.dive_number || ""
                        onEditingFinished: page.stageCurrent()
                    }
                    LabeledField { id: fDuration; label: page.profileLocked ? "Duration (from the dive profile)" : "Duration (min)"; readOnly: page.profileLocked; fieldWidth: 150; text: detailCard.sel.duration || ""; onEditingFinished: page.stageCurrent() }

                    LabeledField { id: fMaxDepth; label: page.profileLocked ? "Max depth (from the dive profile)" : "Max depth (m)"; readOnly: page.profileLocked; fieldWidth: 150; text: detailCard.sel.max_depth || ""; onEditingFinished: page.stageCurrent() }
                    LabeledField { label: "Avg depth (m)"; fieldWidth: 150; readOnly: true; text: detailCard.sel.avg_depth || "" }
                    LabeledField { id: fWaterTemp; label: "Water temp (°C)"; fieldWidth: 150; text: detailCard.sel.water_temp_value !== undefined && detailCard.sel.water_temp_value !== null ? String(detailCard.sel.water_temp_value) : ""; onEditingFinished: page.stageCurrent() }
                    LabeledField { id: fVisibility; visible: controller.hasVisibility; label: "Visibility"; fieldWidth: 150; text: detailCard.sel.visibility || ""; onEditingFinished: page.stageCurrent() }

                    LabeledField { id: fLocation; Layout.columnSpan: 4; label: controller.locationLabel; placeholder: controller.locationHint; fieldWidth: 682; visible: !controller.splitSiteNames; text: detailCard.sel.location || ""; onEditingFinished: page.stageCurrent() }
                    LabeledField { id: fActivityName; Layout.columnSpan: 2; label: "Activity name"; fieldWidth: 316; visible: controller.splitSiteNames; text: detailCard.sel.activity_name || ""; onEditingFinished: page.stageCurrent() }
                    LabeledField { id: fLocationName; Layout.columnSpan: 2; label: "Location name"; fieldWidth: 316; visible: controller.splitSiteNames; text: detailCard.sel.location_name || ""; onEditingFinished: page.stageCurrent() }

                    LabeledField { id: fBuddy; objectName: "field-buddy"; label: "Buddy"; fieldWidth: 150; text: detailCard.sel.buddy || ""; onEditingFinished: page.stageCurrent() }
                    LabeledField { id: fWeight; label: "Weight"; fieldWidth: 150; text: detailCard.sel.weight || ""; onEditingFinished: page.stageCurrent() }
                    LabeledField { id: fLat; label: "Latitude"; fieldWidth: 150; text: page.coordText(detailCard.sel.lat); onEditingFinished: page.stageCurrent() }
                    LabeledField { id: fLng; label: "Longitude"; fieldWidth: 150; text: page.coordText(detailCard.sel.lng); onEditingFinished: page.stageCurrent() }

                    // Set by the device that uploaded the dive, not by the diver
                    LabeledField { objectName: "field-device"; Layout.columnSpan: 2; label: "Dive computer"; fieldWidth: 316; readOnly: true; visible: controller.showsDevice; text: detailCard.sel.device || "" }
                }
                ColumnLayout {
                    spacing: 2
                    enabled: !!detailCard.sel.filename
                    Text { text: "Notes"; color: Theme.muted; font.pixelSize: 11 }
                    // Grows with its text rather than scrolling inside the page:
                    // a nested scroller grabbed the wheel whenever the pointer
                    // crossed it, which made the page scroll stop and start.
                    // a frame drawn around it: the native macOS style gives a
                    // TextArea no border and does not allow replacing its background
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: Math.max(80, fNotes.implicitHeight + 8)
                        color: "transparent"
                        radius: 4
                        border.color: fNotes.activeFocus ? Theme.accent : Theme.border
                        TextArea {
                            id: fNotes
                            anchors { fill: parent; margins: 4 }
                            text: detailCard.sel.notes || ""
                            wrapMode: TextEdit.Wrap
                            selectByMouse: true
                            onEditingFinished: page.stageCurrent()
                        }
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
                        // headings and fields share one width per column, so they line up
                        RowLayout {
                            visible: tankModel.count > 0
                            spacing: 8
                            Text { text: "Name"; Layout.preferredWidth: 160; color: Theme.muted; font.pixelSize: 11 }
                            Text { text: "O2 %"; Layout.preferredWidth: 70; color: Theme.muted; font.pixelSize: 11 }
                            Text { text: "He %"; Layout.preferredWidth: 70; color: Theme.muted; font.pixelSize: 11 }
                            Text { text: "Volume (L)"; Layout.preferredWidth: 80; color: Theme.muted; font.pixelSize: 11 }
                            Text { text: "Start (bar)"; Layout.preferredWidth: 80; color: Theme.muted; font.pixelSize: 11 }
                            Text { text: "End (bar)"; Layout.preferredWidth: 80; color: Theme.muted; font.pixelSize: 11 }
                        }
                        Repeater {
                            model: tankModel
                            RowLayout {
                                spacing: 8
                                required property int index
                                required property string tank_name
                                required property string oxygen
                                required property string helium
                                required property string volume
                                required property string start_pressure
                                required property string end_pressure
                                TextField { Layout.preferredWidth: 160; text: tank_name; placeholderText: "e.g. transmitter name"; onEditingFinished: { tankModel.setProperty(index, "tank_name", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 70; text: oxygen; onEditingFinished: { tankModel.setProperty(index, "oxygen", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 70; text: helium; onEditingFinished: { tankModel.setProperty(index, "helium", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 80; text: volume; onEditingFinished: { tankModel.setProperty(index, "volume", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 80; text: start_pressure; onEditingFinished: { tankModel.setProperty(index, "start_pressure", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 80; text: end_pressure; onEditingFinished: { tankModel.setProperty(index, "end_pressure", text); page.stageCurrent() } }
                                Button {
                                    text: "✕"
                                    flat: true
                                    implicitWidth: 28
                                    Tip {
                                        text: "Remove this tank"
                                        visible: parent.hovered
                                    }
                                    onClicked: { tankModel.remove(index); page.stageCurrent() }
                                }
                            }
                        }
                        Button {
                            text: "+ Add tank"
                            flat: true
                            onClicked: { tankModel.append({tank_name: "", oxygen: "21", helium: "0", volume: "", start_pressure: "", end_pressure: ""}); page.stageCurrent() }
                        }
                    }
                }
                ColumnLayout {
                    id: profileSection
                    spacing: 4
                    visible: (detailCard.sel.samples || []).length > 1
                    // Temperature is deliberately not plotted: both services
                    // record it as whole degrees, so the curve is a staircase
                    // of 1° steps that reads as noise next to the depth line.
                    RowLayout {
                        spacing: 12
                        Text { text: "Depth profile"; color: Theme.muted; font.pixelSize: 11 }
                        Text { text: "● depth"; color: Theme.accent; font.pixelSize: 11 }
                    }
                    Canvas {
                        id: profileCanvas
                        Layout.fillWidth: true
                        Layout.preferredHeight: 160
                        // drawn once into a texture off the GUI thread, then
                        // only moved while the page scrolls
                        renderTarget: Canvas.FramebufferObject
                        renderStrategy: Canvas.Cooperative
                        onWidthChanged: requestPaint()
                        onHeightChanged: requestPaint()
                        Connections { target: detailCard; function onSelChanged() { profileCanvas.requestPaint() } }
                        onPaint: {
                            var ctx = getContext("2d")
                            ctx.clearRect(0, 0, width, height)
                            var samples = detailCard.sel.samples || []
                            if (samples.length < 2 || width <= 0 || height <= 0) return

                            // Right padding is small now that nothing is
                            // labelled on that edge (it held the temperature
                            // scale).
                            var pad = { left: 40, right: 12, top: 10, bottom: 20 }
                            var plotW = width - pad.left - pad.right
                            var plotH = height - pad.top - pad.bottom
                            if (plotW <= 0 || plotH <= 0) return

                            var maxTime = samples[samples.length - 1].time || 0
                            var maxDepth = 0
                            for (var i = 0; i < samples.length; i++) {
                                if (samples[i].depth > maxDepth) maxDepth = samples[i].depth
                            }
                            if (maxDepth <= 0) maxDepth = 1
                            if (maxTime <= 0) maxTime = 1

                            function xAt(t) { return pad.left + (t / maxTime) * plotW }
                            function yDepth(d) { return pad.top + (d / maxDepth) * plotH }

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

                            ctx.fillStyle = Theme.muted
                            ctx.font = "10px sans-serif"
                            ctx.fillText("0 m", 4, pad.top + 8)
                            ctx.fillText(maxDepth.toFixed(1) + " m", 4, pad.top + plotH)
                            ctx.fillText(Math.round(maxTime / 60) + " min", pad.left + plotW - 26, height - 4)
                        }
                    }
                }
            }
        }
    }

    Dialog {
        id: deleteDialog
        title: "Delete dive"
        modal: true
        standardButtons: Dialog.Yes | Dialog.No
        anchors.centerIn: Overlay.overlay
        Text { text: "Mark this dive for deletion? It is deleted from the service when you press Save all changes; Undo keeps it."; color: Theme.text; wrapMode: Text.WordWrap; width: 360 }
        onAccepted: controller.deleteSelected()
    }

    Dialog {
        id: saveAllDialog
        objectName: "saveAllDialog"
        title: "Save all changes"
        modal: true
        standardButtons: Dialog.Yes | Dialog.No
        anchors.centerIn: Overlay.overlay
        Text { text: "This also deletes the dives marked 🗑 from the service, for good. Continue?"; color: Theme.text; wrapMode: Text.WordWrap; width: 360 }
        onAccepted: controller.saveAll()
    }

    Dialog {
        id: columnDialog
        title: "Visible columns"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        anchors.centerIn: Overlay.overlay
        property var chosen: []
        onOpened: chosen = controller.visibleColumns.slice()
        // A grid rather than one tall column: the catalogue is long enough now
        // that a single column of checkboxes runs past a small window.
        GridLayout {
            columns: 3
            columnSpacing: 18
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
