import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

// Dive edit (plans/convert.md I8, I14; "Convert" until I14): open Garmin
// .fit files (or Connect's zip), UDDF or Subsurface files, see and edit the
// dives they hold, and save the selected ones as UDDF or Subsurface .ssrf -
// one dive through a Save-as dialog, several through a folder dialog with
// one file per dive. (MySSI left this page with plans/myssi-sync.md K4: it
// is a sync service with its own Dives page now.)
// The detail card is the editor (I14): every field the model holds, in the
// chosen units; a date, time or zone change moves the whole profile; the
// edited dive is what Save as writes (Save as .fit patches a copy of the
// opened .fit with the edits, I14b, or writes a new .fit for a dive from
// any other file, I12). Nothing is written back to the file
// opened. A dive with unsaved edits carries a dot in the list and "edited"
// on the card until Revert puts it back or Save as writes it; Remove,
// Clear and closing the app (main.qml) ask before such a dive is lost
// (I15). Merge… (I17, I17b) under the list is on with two or more dives
// selected and joins them - the logs of a dive the computer split, in
// start order (convertController.mergeChain) - into one .fit, adding or
// removing the surface time, through the Merge dialog and a Save dialog;
// the dialog says in red why a selection cannot be merged (another
// computer, more than 2 hours apart, not a .fit: mergeProblem), with
// Generate debug report… (I21) next to the reason - and next to a failed
// merge's error under the list - saving a text file for the developer
// (no positions, names or notes in it). The merged dive joins the list,
// the originals stay. A Garmin .fit whose
// dive is in the app's Garmin cache (by activity id, or by start time when
// the name holds none) gets its site, buddy, notes, weight, visibility and
// tank volumes from there (I9, the checkbox, on by default). The list is a
// working list (I9b): Open adds to it, Remove (or Delete/Backspace) takes
// the selected dives off it, Clear empties it. "Open folder…" (I13) adds
// every dive file directly in a chosen folder (subfolders are not searched).
// "Open Cached" (I9c) is the same Open dialog started in the app's Garmin
// FIT cache - the files the Garmin page downloaded, in a data folder the
// diver would not find by hand; it does not become the remembered last
// folder. No sync account is involved; nothing is written but the files
// chosen.
ColumnLayout {
    id: page
    objectName: "convertPage"
    spacing: 12
    readonly property var sel: convertController.selected
    readonly property bool hasDive: !!sel.date_time
    // the editor shows one dive; several selected show a count and the Save buttons
    readonly property bool editing: hasDive && convertController.selectedCount === 1
    // dives downloaded on the Garmin page meanwhile enable Open Cached
    onVisibleChanged: if (visible) convertController.refreshCache()

    // An editable field with its label above, sharing the grid's width
    // (LabeledField with fillWidth; the Dives page's fields keep a fixed one).
    component Edit: LabeledField {
        fillWidth: true
        fieldWidth: 150
    }
    // A choice field: the words the model knows (plus the file's own word
    // when it is another), blank first
    component Choice: ColumnLayout {
        id: choice
        property string label: ""
        property alias model: box.model
        property alias currentIndex: box.currentIndex
        property alias currentText: box.currentText
        signal picked()
        spacing: 2
        Layout.fillWidth: true
        Layout.preferredWidth: 150
        Layout.minimumWidth: 90
        Text { text: choice.label; color: Theme.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
        ComboBox {
            id: box
            Layout.fillWidth: true
            font.pixelSize: 12
            model: [""]
            displayText: currentText === "" ? "—" : currentText
            delegate: ItemDelegate {
                required property var modelData
                required property int index
                width: box.width
                text: modelData === "" ? "(none)" : modelData
                font.pixelSize: 12
                highlighted: box.highlightedIndex === index
            }
            onActivated: choice.picked()
        }
    }
    component SmallButton: Button {
        flat: true
        font.pixelSize: 11
        topPadding: 2
        bottomPadding: 2
        leftPadding: 8
        rightPadding: 8
    }
    component Cell: Text {
        property int cellWidth: 70
        Layout.preferredWidth: cellWidth
        color: Theme.text
        font.pixelSize: 12
        elide: Text.ElideRight
    }

    // The editor's values as the controller takes them (every field; the
    // controller applies only those whose text differs from what it showed).
    function formPayload() {
        var tanks = []
        for (var i = 0; i < tankModel.count; i++) {
            var t = tankModel.get(i)
            tanks.push({ tank_name: t.tank_name, oxygen: t.oxygen, helium: t.helium, volume: t.volume,
                         start_pressure: t.start_pressure, end_pressure: t.end_pressure, role: t.role })
        }
        return {
            date: fDate.text, time: fTime.text, utc_offset: fOffset.text,
            duration: fDuration.text, max_depth: fMaxDepth.text, avg_depth: fAvgDepth.text,
            temp_min: fTempMin.text, temp_max: fTempMax.text, temp_avg: fTempAvg.text, air_temp: fAirTemp.text,
            dive_number: fDiveNumber.text, surface_interval: fSurfaceInterval.text, bottom_time: fBottomTime.text,
            dive_mode: fDiveMode.currentText, water_type: fWaterType.currentText, water_density: fWaterDensity.text,
            location: fLocation.text, lat: fLat.text, lng: fLng.text, exit_lat: fExitLat.text, exit_lng: fExitLng.text,
            buddy: fBuddy.text, notes: fNotes.text, weight: fWeight.text, visibility: fVisibility.text, suit: fSuit.text,
            computer_vendor: fVendor.text, computer_model: fModel.text, computer_serial: fSerial.text,
            computer_firmware: fFirmware.text, gf_low: fGfLow.text, gf_high: fGfHigh.text, deco_model: fDecoModel.text,
            cns_start: fCnsStart.text, cns_end: fCnsEnd.text, sac: fSac.text, tanks: tanks
        }
    }
    // A field commit (Return, Tab, a click elsewhere): the form goes to the
    // controller when it differs from what it showed on arrival, and the
    // edited dive comes back through sel.
    function stageCurrent() {
        if (!page.editing || detailCard.formSnapshot === "") return
        var payload = formPayload()
        if (JSON.stringify(payload) !== detailCard.formSnapshot) convertController.edit(convertController.currentRow, payload)
    }

    // Remove and Clear ask first when a dive with unsaved edits would go
    // with it (I15); the dialog's Discard does what was asked, Keep nothing.
    function removeSelected() {
        if (convertController.selectedCount === 0) return
        if (convertController.revertCount > 0) discardDialog.ask("remove", convertController.revertCount)
        else convertController.removeSelected()
    }
    function clearList() {
        var edited = convertController.editedRows.length
        if (edited > 0) discardDialog.ask("clear", edited)
        else convertController.clear()
    }

    function startSave(formatId) {
        if (convertController.selectedCount === 0) return
        if (convertController.selectedCount === 1) {
            saveDialog.target = formatId
            saveDialog.nameFilters = convertController.saveNameFilters(formatId)
            saveDialog.currentFile = convertController.suggestedFileUrl(formatId)
            saveDialog.open()
        } else {
            folderDialog.target = formatId
            folderDialog.open()
        }
    }

    // Merge (I17): the Save dialog's file goes to the controller with the
    // dialog's ticked chain and mode; the controller merges on its worker.
    function runMerge(url) {
        convertController.merge(mergeDialog.tickedRows(), url, mergeDialog.closeGaps, mergeDialog.serialChoice())
    }
    // Generate debug report… (I21): a save dialog for the report of the
    // dialog's ticked rows (rows) or of the last failed merge (null).
    readonly property string debugReportTip: "Saves a text file with technical details of these logs (no positions, names or notes) that you can send to the developer."
    function askDebugReport(rows) {
        debugReportDialog.rows = rows
        debugReportDialog.currentFile = convertController.debugReportUrl()
        debugReportDialog.open()
    }
    function saveDebugReport(url) {
        if (!debugReportDialog.rows) convertController.saveDebugReport(url)
        else convertController.saveDebugReport(url, debugReportDialog.rows)
    }

    Card {
        id: topCard
        title: "Dive edit"
        headerContent: [
            Button {
                text: "Open files…"
                objectName: "openFilesButton"
                enabled: !convertController.busy
                Tip { text: "Garmin .fit files or Connect's \"export original\" zip, UDDF and Subsurface (.ssrf) files; several at once. The dives are added to the list."; visible: parent.hovered }
                onClicked: openDialog.open()
            },
            Button {
                text: "Open folder…"
                objectName: "openFolderButton"
                enabled: !convertController.busy
                Tip { text: "Every dive file directly in the folder (Garmin .fit or Connect zip, UDDF, Subsurface); subfolders are not searched. The dives are added to the list."; visible: parent.hovered }
                onClicked: openFolderDialog.open()
            },
            Button {
                text: "Open Cached"
                objectName: "openCachedButton"
                enabled: !convertController.busy && convertController.cacheAvailable
                // a HoverHandler, not hovered: the tip explains the button when it is off too
                Tip { text: convertController.cacheTip; visible: cachedHover.hovered }
                HoverHandler { id: cachedHover }
                onClicked: cachedDialog.open()
            },
            CheckBox {
                objectName: "enrichCheckBox"
                text: "Fill from Garmin cache"
                checked: convertController.enrichFromCache
                onToggled: convertController.setEnrichFromCache(checked)
                Tip {
                    text: "A Garmin .fit file holds the profile and the tanks but not what you typed into Garmin Connect. When the dive is one this app has cached (the Garmin page's dives) - found by the activity id in the file's name, or by its start time when the name holds none - its site, buddy, notes, weight, visibility and tank sizes are filled in from there, only where the file has none. Untick to show the file alone."
                    visible: parent.hovered
                }
            },
            Text {
                text: convertController.files.length > 0 ? convertController.files.join(", ") : ""
                color: Theme.muted
                font.pixelSize: 12
                elide: Text.ElideMiddle
                Layout.maximumWidth: 420
            },
            Item { Layout.fillWidth: true }
        ]
        Text {
            visible: convertController.diveCount === 0
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 12
            text: "Open the files of a dive computer, or a folder of them (Open adds to the list; Remove takes dives off it), pick a dive to edit it on the right (every field; a change of the date, time or time zone moves the whole profile), select the dives (Cmd/Ctrl-click adds one, Shift-click a range) and save them with their edits as UDDF or Subsurface files. Your accounts are not touched; the files you open are never changed, only the files you save are written."
        }
        ProgressBar { Layout.fillWidth: true; indeterminate: true; visible: convertController.busy }
        Text { visible: text !== ""; text: convertController.message; color: Theme.muted; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
        Repeater {
            model: convertController.warnings
            delegate: Text {
                required property string modelData
                text: "⚠ " + modelData
                color: Theme.danger
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }
    }

    RowLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.minimumHeight: 240
        spacing: 12

        Card {
            id: listCard
            Layout.preferredWidth: 440
            Layout.minimumWidth: 360
            Layout.fillHeight: true
            title: "Dives (" + convertController.diveCount + ")"
            // The list's own actions sit in a compact row inside the card, not
            // beside the title: three full-size buttons there ran past the
            // card's edge at its preferred width.
            RowLayout {
                Layout.fillWidth: true
                spacing: 4
                visible: convertController.diveCount > 0
                Text {
                    text: convertController.selectedCount > 0 ? convertController.selectedCount + " selected" : "None selected"
                    color: Theme.muted
                    font.pixelSize: 11
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
                SmallButton {
                    text: "Select all"
                    objectName: "selectAllButton"
                    visible: convertController.diveCount > 1
                    onClicked: convertController.selectAll()
                }
                SmallButton {
                    text: "Remove"
                    objectName: "removeButton"
                    enabled: convertController.selectedCount > 0
                    Tip { text: "Takes the selected dives off the list (Delete or Backspace does the same); the files on disk are not touched. Asks first when a dive has unsaved edits"; visible: parent.hovered }
                    onClicked: page.removeSelected()
                }
                SmallButton {
                    text: "Clear"
                    objectName: "clearButton"
                    Tip { text: "Empties the list; the files on disk are not touched. Asks first when a dive has unsaved edits"; visible: parent.hovered }
                    onClicked: page.clearList()
                }
            }
            RowLayout {
                spacing: 8
                Layout.leftMargin: 6
                Cell { text: ""; cellWidth: 10 }
                Cell { text: "Date"; cellWidth: 78; color: Theme.muted; font.pixelSize: 11 }
                Cell { text: "Time"; cellWidth: 44; color: Theme.muted; font.pixelSize: 11 }
                Cell { text: "#"; cellWidth: 36; color: Theme.muted; font.pixelSize: 11 }
                Cell { text: "Depth"; cellWidth: 54; color: Theme.muted; font.pixelSize: 11 }
                Cell { text: "Time"; cellWidth: 54; color: Theme.muted; font.pixelSize: 11 }
                Cell { text: "Site"; cellWidth: 120; color: Theme.muted; font.pixelSize: 11; Layout.fillWidth: true }
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumHeight: 120
                color: "transparent"
                border.color: Theme.border
                radius: 6
                clip: true
                ListView {
                    id: diveList
                    objectName: "convertDiveList"
                    anchors { fill: parent; margins: 1 }
                    clip: true
                    model: convertController.dives
                    ScrollBar.vertical: ScrollBar {}
                    // Delete or Backspace removes the selected dives from the list (I9b)
                    Keys.onDeletePressed: function (event) { page.removeSelected(); event.accepted = true }
                    Keys.onPressed: function (event) {
                        if (event.key === Qt.Key_Backspace) { page.removeSelected(); event.accepted = true }
                    }
                    delegate: Rectangle {
                        id: row
                        required property int index
                        required property var modelData
                        readonly property bool picked: convertController.selection.indexOf(index) >= 0
                        readonly property bool current: convertController.currentRow === index
                        // unsaved edits (I15): the dot, like the Dives page's ✎
                        readonly property bool edited: convertController.editedRows.indexOf(index) >= 0
                        width: diveList.width
                        implicitHeight: 26
                        color: picked ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, current ? 0.3 : 0.18)
                                      : (index % 2 ? Theme.bg : Theme.card)
                        RowLayout {
                            anchors { fill: parent; leftMargin: 6; rightMargin: 6 }
                            spacing: 8
                            Cell { objectName: "editMark-" + row.index; text: row.edited ? "●" : ""; cellWidth: 10; color: Theme.accent }
                            Cell { text: row.modelData.date; cellWidth: 78 }
                            Cell { text: row.modelData.time; cellWidth: 44 }
                            Cell { text: String(row.modelData.dive_number); cellWidth: 36 }
                            Cell { text: row.modelData.max_depth; cellWidth: 54 }
                            Cell { text: row.modelData.duration; cellWidth: 54 }
                            Cell { text: row.modelData.location || row.modelData.source; cellWidth: 120; Layout.fillWidth: true; color: row.modelData.location ? Theme.text : Theme.muted }
                        }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: function (mouse) {
                                var toggle = (mouse.modifiers & Qt.ControlModifier) !== 0 || (mouse.modifiers & Qt.MetaModifier) !== 0
                                var extend = (mouse.modifiers & Qt.ShiftModifier) !== 0
                                diveList.forceActiveFocus()
                                convertController.clickRow(row.index, toggle, extend)
                            }
                        }
                    }
                }
            }
            Text {
                visible: convertController.diveCount > 0
                text: "Click selects one dive · Cmd/Ctrl-click adds or removes one · Shift-click selects a range · Delete removes the selected dives from the list"
                color: Theme.muted
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            // Merge… (I17b) at the bottom of the list: on with two or more
            // dives selected; the dialog lists them in start order and says
            // why they cannot be merged when they cannot.
            RowLayout {
                visible: convertController.diveCount > 0
                spacing: 4
                Layout.fillWidth: true
                Text {
                    objectName: "mergeHint"
                    text: "Merge…: select the logs of a dive the computer split in two or more (Cmd/Ctrl-click), then join them into one .fit."
                    color: Theme.muted
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                SmallButton {
                    objectName: "mergeButton"
                    text: "Merge…"
                    enabled: convertController.selectedCount > 1 && !convertController.busy
                    Tip {
                        objectName: "mergeTip"
                        text: convertController.selectedCount > 1
                              ? "Joins the selected Garmin .fit logs of one dive that the computer split into several (same dive computer, each log starting within 2 hours of the previous one) into one .fit file. Select the logs, then Merge… asks whether to add or remove the surface time between them."
                              : "Select two or more logs of the same dive to merge them."
                        visible: parent.hovered
                    }
                    onClicked: mergeDialog.openFor()
                }
            }
            // a failed merge: the reason in red with Generate debug report… (I21) next to it
            RowLayout {
                visible: convertController.mergeError !== ""
                spacing: 4
                Layout.fillWidth: true
                Text {
                    objectName: "mergeError"
                    visible: convertController.mergeError !== ""
                    text: "⚠ Merge: " + convertController.mergeError
                    color: Theme.danger
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                SmallButton {
                    objectName: "debugReportListButton"
                    text: "Generate debug report…"
                    enabled: convertController.canReport && !convertController.busy
                    Tip { objectName: "debugReportListTip"; text: page.debugReportTip; visible: parent.hovered }
                    onClicked: page.askDebugReport(null)
                }
            }
        }

        ScrollView {
            id: detailScroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            clip: true
            Component.onCompleted: {
                var flick = detailScroll.contentItem
                flick.boundsBehavior = Flickable.StopAtBounds
                flick.pixelAligned = true
            }
            ColumnLayout {
                width: detailScroll.availableWidth
                Card {
                    id: detailCard
                    title: convertController.selectedCount > 1 ? "Dives" : "Dive"
                    // The editor (I14): every field the model holds, loaded from
                    // sel.form on each selection change (imperative, so a field the
                    // diver typed into shows the controller's echo again) and sent
                    // back whole on each field commit; the controller keeps only
                    // the fields whose text changed. A units switch re-emits the
                    // selection, so the form is re-loaded and re-snapshotted: not
                    // an edit.
                    property string formSnapshot: ""
                    ListModel { id: tankModel }
                    function loadTanks(form) {
                        tankModel.clear()
                        var list = form.tanks || []
                        for (var i = 0; i < list.length; i++) {
                            var t = list[i]
                            tankModel.append({ tank_name: t.tank_name || "", oxygen: t.oxygen || "", helium: t.helium || "",
                                               volume: t.volume || "", start_pressure: t.start_pressure || "",
                                               end_pressure: t.end_pressure || "", role: t.role || "" })
                        }
                    }
                    function loadChoice(box, options, value) {
                        box.model = options || [""]
                        var i = box.model.indexOf(value || "")
                        box.currentIndex = i < 0 ? 0 : i
                    }
                    function loadForm() {
                        var form = page.sel.form || {}
                        fDate.text = form.date || ""; fTime.text = form.time || ""; fOffset.text = form.utc_offset || ""
                        fDuration.text = form.duration || ""; fMaxDepth.text = form.max_depth || ""; fAvgDepth.text = form.avg_depth || ""
                        fTempMin.text = form.temp_min || ""; fTempMax.text = form.temp_max || ""; fTempAvg.text = form.temp_avg || ""
                        fAirTemp.text = form.air_temp || ""; fSuit.text = form.suit || ""
                        fDiveNumber.text = form.dive_number || ""; fSurfaceInterval.text = form.surface_interval || ""
                        fBottomTime.text = form.bottom_time || ""; fWaterDensity.text = form.water_density || ""
                        fLocation.text = form.location || ""; fLat.text = form.lat || ""; fLng.text = form.lng || ""
                        fExitLat.text = form.exit_lat || ""; fExitLng.text = form.exit_lng || ""
                        fBuddy.text = form.buddy || ""; fNotes.text = form.notes || ""; fWeight.text = form.weight || ""
                        fVisibility.text = form.visibility || ""
                        fVendor.text = form.computer_vendor || ""; fModel.text = form.computer_model || ""
                        fSerial.text = form.computer_serial || ""; fFirmware.text = form.computer_firmware || ""
                        fGfLow.text = form.gf_low || ""; fGfHigh.text = form.gf_high || ""; fDecoModel.text = form.deco_model || ""
                        fCnsStart.text = form.cns_start || ""; fCnsEnd.text = form.cns_end || ""
                        fSac.text = form.sac || ""; fSac.placeholder = form.sac_hint || ""
                        var choices = page.sel.choices || {}
                        loadChoice(fDiveMode, choices.dive_mode, form.dive_mode)
                        loadChoice(fWaterType, choices.water_type, form.water_type)
                        loadTanks(form)
                        formSnapshot = page.hasDive ? JSON.stringify(page.formPayload()) : ""
                    }
                    Component.onCompleted: loadForm()
                    Connections {
                        target: page
                        function onSelChanged() { detailCard.loadForm() }
                    }
                    Text {
                        text: convertController.selectedCount > 1 ? convertController.selectedCount + " dives selected"
                              : (page.hasDive ? page.sel.title : "No dive selected")
                        objectName: "detailTitle"
                        color: Theme.text
                        font.bold: true
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        visible: convertController.selectedCount > 1
                        text: "Save as writes one file per dive; select one dive to edit it."
                        color: Theme.muted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    // the source line, the unsaved-edit mark and Revert (I15): one dive
                    // selected reverts it, several revert the edited ones among them
                    RowLayout {
                        visible: page.hasDive
                        spacing: 8
                        Text {
                            objectName: "sourceLine"
                            visible: page.editing && page.sel.source !== ""
                            text: "From " + page.sel.source + (page.sel.external_ids ? " · " + page.sel.external_ids : "")
                            color: Theme.muted
                            font.pixelSize: 11
                            elide: Text.ElideMiddle
                            Layout.maximumWidth: 480
                        }
                        Text {
                            objectName: "editedMark"
                            visible: page.editing && convertController.editedRows.indexOf(convertController.currentRow) >= 0
                            text: "● edited"
                            color: Theme.accent
                            font.pixelSize: 11
                        }
                        Item { Layout.fillWidth: true }
                        Button {
                            objectName: "revertButton"
                            text: convertController.selectedCount > 1 && convertController.revertCount > 0 ? "Revert " + convertController.revertCount : "Revert"
                            flat: true
                            enabled: convertController.revertCount > 0 && !convertController.busy
                            Tip {
                                text: convertController.selectedCount > 1
                                      ? "Puts the selected dives with unsaved edits back as they were read from their files; the files on disk are not touched"
                                      : "Puts the dive back as it was read from the file; the file on disk is not touched"
                                visible: parent.hovered
                            }
                            onClicked: convertController.selectedCount > 1 ? convertController.revertSelected() : convertController.revert(convertController.currentRow)
                        }
                    }
                    // Rebreather data is untested (plans/gap-roadmap.md R18b): said, muted, under the source line
                    Text {
                        objectName: "ccrNote"
                        visible: page.hasDive && !!page.sel.ccr_note
                        text: page.sel.ccr_note || ""
                        color: Theme.muted
                        font.pixelSize: 11
                        font.italic: true
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    Text {
                        objectName: "enrichedLine"
                        visible: page.editing && (page.sel.enriched || "") !== ""
                        text: page.sel.enriched ? "Filled in: " + page.sel.enriched : ""
                        color: Theme.muted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    Text {
                        objectName: "editError"
                        visible: page.editing && convertController.editError !== ""
                        text: "⚠ " + convertController.editError
                        color: Theme.danger
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    // Dive: when, how long, how deep, the water, the kind of dive.
                    // Quantities are shown and typed in the Settings page's unit
                    // system (units.labels, Track J); the controller converts.
                    ColumnLayout {
                        visible: page.editing
                        spacing: 2
                        Layout.fillWidth: true
                        Text { text: "Dive"; color: Theme.muted; font.pixelSize: 11; font.bold: true }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: 4
                            columnSpacing: 12
                            rowSpacing: 6
                            Edit { id: fDate; objectName: "edit-date"; label: "Date"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fTime; objectName: "edit-time"; label: "Time"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fOffset; objectName: "edit-utc-offset"; label: "Time zone (UTC offset)"; placeholder: "+07:00"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fDiveNumber; objectName: "edit-dive-number"; label: "Dive #"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fDuration; objectName: "edit-duration"; label: page.sel.locked ? "Duration (from the dive profile)" : "Duration (min)"; readOnly: page.sel.locked === true; onEditingFinished: page.stageCurrent() }
                            Edit { id: fMaxDepth; objectName: "edit-max-depth"; label: page.sel.locked ? "Max depth (from the dive profile)" : "Max depth (" + units.labels.depth + ")"; readOnly: page.sel.locked === true; onEditingFinished: page.stageCurrent() }
                            Edit { id: fAvgDepth; objectName: "edit-avg-depth"; label: page.sel.locked ? "Avg depth (from the dive profile)" : "Avg depth (" + units.labels.depth + ")"; readOnly: page.sel.locked === true; onEditingFinished: page.stageCurrent() }
                            Edit { id: fBottomTime; objectName: "edit-bottom-time"; label: "Bottom time (min)"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fTempMin; objectName: "edit-temp-min"; label: "Water temp min (" + units.labels.temperature + ")"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fTempMax; objectName: "edit-temp-max"; label: "Water temp max (" + units.labels.temperature + ")"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fTempAvg; objectName: "edit-temp-avg"; label: "Water temp avg (" + units.labels.temperature + ")"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fAirTemp; objectName: "edit-air-temp"; label: "Air temp (" + units.labels.temperature + ")"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fSurfaceInterval; objectName: "edit-surface-interval"; label: "Surface interval (min)"; onEditingFinished: page.stageCurrent() }
                            Choice { id: fDiveMode; objectName: "edit-dive-mode"; label: "Dive mode"; onPicked: page.stageCurrent() }
                            Choice { id: fWaterType; objectName: "edit-water-type"; label: "Water type"; onPicked: page.stageCurrent() }
                            Edit { id: fWaterDensity; objectName: "edit-water-density"; label: "Water density (kg/m³)"; onEditingFinished: page.stageCurrent() }
                            // the computer's own SAC; when it logged none the placeholder holds the one computed from the tanks (R9)
                            Edit { id: fSac; objectName: "edit-sac"; label: "SAC (" + units.labels.sac + ")"; onEditingFinished: page.stageCurrent() }
                            Edit { objectName: "show-rmv"; label: "RMV (" + units.labels.sac + ")"; readOnly: true; text: page.hasDive ? (page.sel.rmv || "") : "" }
                            Edit { objectName: "show-otu"; label: "OTU"; readOnly: true; text: page.hasDive ? (page.sel.otu || "") : "" }
                            Item { Layout.fillWidth: true }
                        }
                    }
                    // Site: the name and the entry and exit positions
                    ColumnLayout {
                        visible: page.editing
                        spacing: 2
                        Layout.fillWidth: true
                        Text { text: "Site"; color: Theme.muted; font.pixelSize: 11; font.bold: true }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: 4
                            columnSpacing: 12
                            rowSpacing: 6
                            Edit { id: fLocation; objectName: "edit-location"; label: "Site"; Layout.columnSpan: 4; fieldWidth: 600; onEditingFinished: page.stageCurrent() }
                            Edit { id: fLat; objectName: "edit-lat"; label: "Latitude"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fLng; objectName: "edit-lng"; label: "Longitude"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fExitLat; objectName: "edit-exit-lat"; label: "Exit latitude"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fExitLng; objectName: "edit-exit-lng"; label: "Exit longitude"; onEditingFinished: page.stageCurrent() }
                        }
                    }
                    // People and notes
                    ColumnLayout {
                        visible: page.editing
                        spacing: 2
                        Layout.fillWidth: true
                        Text { text: "People / notes"; color: Theme.muted; font.pixelSize: 11; font.bold: true }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: 4
                            columnSpacing: 12
                            rowSpacing: 6
                            Edit { id: fBuddy; objectName: "edit-buddy"; label: "Buddy"; Layout.columnSpan: 2; fieldWidth: 300; onEditingFinished: page.stageCurrent() }
                            Edit { id: fWeight; objectName: "edit-weight"; label: "Weight (" + units.labels.weight + ")"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fVisibility; objectName: "edit-visibility"; label: "Visibility (" + units.labels.depth + ")"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fSuit; objectName: "edit-suit"; label: "Suit"; Layout.columnSpan: 2; fieldWidth: 300; onEditingFinished: page.stageCurrent() }
                        }
                        Text { text: "Notes"; color: Theme.muted; font.pixelSize: 11 }
                        // Grows with its text rather than scrolling inside the page
                        // (see DivesPage.qml); a frame drawn around it: the native
                        // macOS style gives a TextArea no border.
                        Rectangle {
                            Layout.fillWidth: true
                            implicitHeight: Math.max(60, fNotes.implicitHeight + 8)
                            color: "transparent"
                            radius: 4
                            border.color: fNotes.activeFocus ? Theme.accent : Theme.border
                            TextArea {
                                id: fNotes
                                objectName: "edit-notes"
                                anchors { fill: parent; margins: 4 }
                                font.pixelSize: 12
                                wrapMode: TextEdit.Wrap
                                selectByMouse: true
                                onEditingFinished: page.stageCurrent()
                            }
                        }
                    }
                    // Computer: what recorded the dive and its deco settings
                    ColumnLayout {
                        visible: page.editing
                        spacing: 2
                        Layout.fillWidth: true
                        Text { text: "Dive computer"; color: Theme.muted; font.pixelSize: 11; font.bold: true }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: 4
                            columnSpacing: 12
                            rowSpacing: 6
                            Edit { id: fVendor; objectName: "edit-computer-vendor"; label: "Maker"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fModel; objectName: "edit-computer-model"; label: "Model"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fSerial; objectName: "edit-computer-serial"; label: "Serial"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fFirmware; objectName: "edit-computer-firmware"; label: "Firmware"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fGfLow; objectName: "edit-gf-low"; label: "GF low (%)"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fGfHigh; objectName: "edit-gf-high"; label: "GF high (%)"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fDecoModel; objectName: "edit-deco-model"; label: "Deco model"; onEditingFinished: page.stageCurrent() }
                            Item { Layout.fillWidth: true }
                            Edit { id: fCnsStart; objectName: "edit-cns-start"; label: "CNS start (%)"; onEditingFinished: page.stageCurrent() }
                            Edit { id: fCnsEnd; objectName: "edit-cns-end"; label: "CNS end (%)"; onEditingFinished: page.stageCurrent() }
                        }
                    }
                    // Tanks: the editable table (I14), one row per gas mixture
                    ColumnLayout {
                        visible: page.editing
                        spacing: 4
                        Layout.fillWidth: true
                        Text { text: "Tanks / gases"; color: Theme.muted; font.pixelSize: 11; font.bold: true }
                        RowLayout {
                            objectName: "tankHeadings"
                            visible: tankModel.count > 0
                            spacing: 6
                            Text { text: "Name"; Layout.preferredWidth: 90; Layout.fillWidth: true; color: Theme.muted; font.pixelSize: 11 }
                            Text { text: "O2 %"; Layout.preferredWidth: 46; color: Theme.muted; font.pixelSize: 11 }
                            Text { text: "He %"; Layout.preferredWidth: 46; color: Theme.muted; font.pixelSize: 11 }
                            Text { text: "Volume (" + units.labels.volume + ")"; Layout.preferredWidth: 60; color: Theme.muted; font.pixelSize: 11; elide: Text.ElideRight }
                            Text { text: "Start (" + units.labels.pressure + ")"; Layout.preferredWidth: 56; color: Theme.muted; font.pixelSize: 11; elide: Text.ElideRight }
                            Text { text: "End (" + units.labels.pressure + ")"; Layout.preferredWidth: 56; color: Theme.muted; font.pixelSize: 11; elide: Text.ElideRight }
                            Text { text: "Role"; Layout.preferredWidth: 70; color: Theme.muted; font.pixelSize: 11 }
                            Item { Layout.preferredWidth: 24 }
                        }
                        Repeater {
                            id: tankRows
                            objectName: "tankRows"
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
                                required property string role
                                TextField { Layout.preferredWidth: 90; Layout.fillWidth: true; font.pixelSize: 12; text: tank_name; placeholderText: "name"; onEditingFinished: { tankModel.setProperty(index, "tank_name", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 46; font.pixelSize: 12; text: oxygen; onEditingFinished: { tankModel.setProperty(index, "oxygen", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 46; font.pixelSize: 12; text: helium; onEditingFinished: { tankModel.setProperty(index, "helium", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 60; font.pixelSize: 12; text: volume; onEditingFinished: { tankModel.setProperty(index, "volume", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 56; font.pixelSize: 12; text: start_pressure; onEditingFinished: { tankModel.setProperty(index, "start_pressure", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 56; font.pixelSize: 12; text: end_pressure; onEditingFinished: { tankModel.setProperty(index, "end_pressure", text); page.stageCurrent() } }
                                TextField { Layout.preferredWidth: 70; font.pixelSize: 12; text: role; placeholderText: "backGas"; onEditingFinished: { tankModel.setProperty(index, "role", text); page.stageCurrent() } }
                                SmallButton {
                                    text: "✕"
                                    implicitWidth: 24
                                    Tip { text: "Remove this tank"; visible: parent.hovered }
                                    onClicked: { tankModel.remove(index); page.stageCurrent() }
                                }
                            }
                        }
                        SmallButton {
                            text: "+ Add tank"
                            objectName: "addTankButton"
                            onClicked: { tankModel.append({tank_name: "", oxygen: "21", helium: "0", volume: "", start_pressure: "", end_pressure: "", role: ""}); page.stageCurrent() }
                        }
                    }
                    // the depth profile (ProfileChart.qml, shared with the dives pages)
                    ProfileChart {
                        objectName: "profileChart"; visible: page.editing; Layout.fillWidth: true
                        samples: page.editing ? (page.sel.samples || []) : []
                        depthScale: units.scales.depth
                        // the hover tooltip asks the controller for one sample at a time (I19)
                        markers: page.editing ? (page.sel.markers || []) : []
                        ceiling: page.editing ? (page.sel.ceiling || []) : []
                        detailsProvider: function (i) { return convertController.sampleDetails(i) }
                    }
                    Text {
                        visible: page.editing
                        text: page.sel.sample_count > 0
                              ? (page.sel.sample_count + " samples"
                                 + ((page.sel.channels || []).length > 0 ? " · also logged: " + page.sel.channels.join(", ") : "")
                                 + " · a change of the date, time or time zone moves the whole profile")
                              : "No dive profile in the file"
                        color: Theme.muted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    // Events stay read-only
                    ColumnLayout {
                        visible: page.editing && page.sel.event_count > 0
                        spacing: 1
                        Text { text: "Events (" + (page.hasDive ? page.sel.event_count : 0) + ")"; color: Theme.muted; font.pixelSize: 11 }
                        Repeater {
                            model: page.editing ? (page.sel.events || []) : []
                            delegate: Text {
                                required property string modelData
                                text: modelData
                                color: Theme.text
                                font.pixelSize: 11
                                font.family: "Menlo"
                            }
                        }
                    }
                }
            }
        }
    }

    Card {
        id: saveCard
        title: "Save as / send"
        RowLayout {
            spacing: 8
            Repeater {
                objectName: "saveTargets"
                model: convertController.targets
                delegate: Button {
                    required property var modelData
                    objectName: "saveTarget-" + modelData.id
                    // Save as .fit patches the opened file's bytes for a dive opened from a .fit (I14b) and
                    // writes any other dive from scratch (I12); off, with the tip, for a selection mixing the two
                    readonly property bool patchTarget: modelData.patch === true
                    readonly property string blocked: patchTarget ? convertController.fitSaveTip : ""
                    text: modelData.title
                    enabled: convertController.selectedCount > 0 && !convertController.busy && blocked === ""
                    Tip {
                        text: (blocked !== "" ? blocked
                               : patchTarget
                                 ? (convertController.fitEncodes
                                    ? (convertController.selectedCount > 1
                                       ? "Writes one Garmin .fit file per selected dive into a folder you pick"
                                       : "Writes the selected dive as a new Garmin .fit file")
                                    : convertController.selectedCount > 1
                                    ? "Patches each selected dive's own .fit with its edits into a new file per dive in a folder you pick; the opened files are kept"
                                    : "Patches the dive's own .fit with your edits into a new file; the opened file is kept")
                               : (convertController.selectedCount > 1
                                  ? "Writes one " + modelData.extension + " file per selected dive into a folder you pick"
                                  : "Writes the selected dive as a " + modelData.extension + " file"))
                        visible: parent.hovered
                    }
                    onClicked: page.startSave(modelData.id)
                }
            }
            Text {
                visible: convertController.selectedCount > 0
                text: convertController.selectedCount === 1 ? "1 dive selected" : convertController.selectedCount + " dives selected, one file per dive"
                color: Theme.muted
                font.pixelSize: 12
            }
            Item { Layout.fillWidth: true }
        }
        // Save as .fit (I14b / I12): what the file cannot take, before the save, and the Connect re-upload note
        Text {
            objectName: "fitDropsNote"
            visible: convertController.fitDrops.length > 0
            text: "Not in a .fit, save as UDDF or Subsurface for these: " + convertController.fitDrops.join(", ")
            color: Theme.muted
            font.pixelSize: 11
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
        Text {
            objectName: "fitConnectNote"
            visible: convertController.selectedCount > 0 && convertController.fitSaveTip === ""
            text: "Garmin Connect knows a dive by its start time and device; delete the original activity there before uploading an edited file."
            color: Theme.muted
            font.pixelSize: 11
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }
        Repeater {
            model: convertController.saveWarnings
            delegate: Text {
                required property string modelData
                text: "Not carried: " + modelData
                color: Theme.muted
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }
    }

    // Remove / Clear with unsaved edits in the way (I15): like main.qml's
    // "Unsaved dive changes", Discard goes on with what was asked, Keep stops.
    Dialog {
        id: discardDialog
        objectName: "discardEditsDialog"
        property string action: ""
        property int count: 0
        function ask(what, n) { action = what; count = n; open() }
        title: "Unsaved dive edits"
        modal: true
        anchors.centerIn: Overlay.overlay
        // a fixed width: a custom footer and the dialog's implicit width otherwise feed each other (a binding loop)
        width: 420
        Text {
            text: "Discard edits to " + discardDialog.count + (discardDialog.count === 1 ? " dive?" : " dives?")
                  + (discardDialog.action === "clear" ? " Clear empties the list." : " Remove takes the dive" + (discardDialog.count === 1 ? "" : "s") + " off the list.")
            color: Theme.text
            wrapMode: Text.WordWrap
            width: 380
        }
        footer: DialogButtonBox {
            Button { objectName: "discardEditsButton"; text: "Discard"; DialogButtonBox.buttonRole: DialogButtonBox.DestructiveRole }
            Button { objectName: "keepEditsButton"; text: "Keep"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        }
        onDiscarded: {
            close()
            if (action === "clear") convertController.clear()
            else convertController.removeSelected()
        }
    }

    // Merge split dive (I17, I17b): the selected dives in start order as
    // checkable rows (the first fixed; a chain cannot skip, so unticking a
    // row unticks the ones after it and ticking one ticks the ones before),
    // the reason in red when the ticked rows cannot be merged (Save… off),
    // add or remove the surface time, then Save… asks where the merged .fit
    // goes.
    Dialog {
        id: mergeDialog
        objectName: "mergeDialog"
        property bool closeGaps: closeGapsChoice.checked
        property bool anyEdited: false
        property int tickedCount: 0
        property string problem: ""     // why the ticked rows cannot be merged ("" when they can)
        // the dive computer serial (I20): the choices when a ticked log has none, the note under them
        property var serialChoices: []
        property bool serialMissing: false
        property string serialNote: ""
        function recheck() {
            var rows = tickedRows()
            problem = convertController.mergeProblemOf(rows)
            var serials = convertController.mergeSerialsOf(rows)
            serialChoices = serials.choices
            serialMissing = serials.missing.length > 0
            serialNote = serials.note
            serialBox.currentIndex = serialChoices.length > 0 ? 0 : -1
        }
        function serialChoice() {
            return (problem === "" && serialMissing && serialChoices.length > 0) ? String(serialBox.currentText) : ""
        }
        function openFor() {
            mergeRows.clear()
            var chain = convertController.mergeChain
            var edited = false
            for (var i = 0; i < chain.length; i++) {
                var r = chain[i]
                mergeRows.append({ row: r.row, date: r.date, time: r.time, number: String(r.dive_number), duration: r.duration,
                                   source: r.source, edited: r.edited, ticked: true })
                if (r.edited) edited = true
            }
            anyEdited = edited
            tickedCount = chain.length
            keepTimesChoice.checked = true
            recheck()
            open()
        }
        function setTicked(index, on) {
            if (index === 0) return
            if (on) for (var i = 1; i <= index; i++) mergeRows.setProperty(i, "ticked", true)
            else for (var j = index; j < mergeRows.count; j++) mergeRows.setProperty(j, "ticked", false)
            var n = 0
            for (var k = 0; k < mergeRows.count; k++) if (mergeRows.get(k).ticked) n++
            tickedCount = n
            recheck()
        }
        function tickedRows() {
            var rows = []
            for (var i = 0; i < mergeRows.count; i++) if (mergeRows.get(i).ticked) rows.push(mergeRows.get(i).row)
            return rows
        }
        title: "Merge split dive"
        modal: true
        anchors.centerIn: Overlay.overlay
        // a fixed width: a custom footer and the dialog's implicit width otherwise feed each other (a binding loop)
        width: 600
        ListModel { id: mergeRows }
        ColumnLayout {
            width: 560
            spacing: 8
            Text {
                text: "The selected logs, in start order. The logs of one dive come from the same dive computer and follow each other within 2 hours; they are joined into one .fit, the first log's file with the later ones' samples, events and tank readings added. The files you opened are kept."
                color: Theme.muted
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            Repeater {
                objectName: "mergeRows"
                model: mergeRows
                delegate: RowLayout {
                    required property int index
                    required property var model
                    spacing: 8
                    Layout.fillWidth: true
                    CheckBox {
                        objectName: "mergeRow-" + index
                        checked: model.ticked
                        enabled: index > 0
                        onToggled: {
                            var on = checked
                            mergeDialog.setTicked(index, on)
                            checked = Qt.binding(function () { return model.ticked })
                        }
                        Tip { text: index === 0 ? "The first log: the merged dive starts here" : "Untick to leave this log and the ones after it out"; visible: parent.hovered }
                    }
                    Cell { text: model.date; cellWidth: 80 }
                    Cell { text: model.time; cellWidth: 60 }
                    Cell { text: model.number === "" ? "" : "dive " + model.number; cellWidth: 70 }
                    Cell { text: model.duration; cellWidth: 60 }
                    Cell { text: model.source; cellWidth: 170; elide: Text.ElideMiddle; Layout.fillWidth: true }
                    Cell { objectName: "mergeRowEdited-" + index; text: model.edited ? "● edited" : ""; color: Theme.accent; cellWidth: 60 }
                }
            }
            RowLayout {
                visible: mergeDialog.problem !== ""
                spacing: 4
                Layout.fillWidth: true
                Text {
                    objectName: "mergeProblem"
                    visible: mergeDialog.problem !== ""
                    text: "⚠ Cannot merge: " + mergeDialog.problem
                    color: Theme.danger
                    font.pixelSize: 11
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                SmallButton {
                    objectName: "debugReportButton"
                    text: "Generate debug report…"
                    Tip { objectName: "debugReportTip"; text: page.debugReportTip; visible: parent.hovered }
                    onClicked: page.askDebugReport(mergeDialog.tickedRows())
                }
            }
            RowLayout {
                objectName: "mergeSerialRow"
                visible: mergeDialog.problem === "" && mergeDialog.serialMissing && mergeDialog.serialChoices.length > 0
                spacing: 8
                Layout.fillWidth: true
                Text { text: "Dive computer serial for the merged file:"; color: Theme.text; font.pixelSize: 12 }
                ComboBox {
                    id: serialBox
                    objectName: "mergeSerialChoice"
                    model: mergeDialog.serialChoices
                    Layout.preferredWidth: 180
                }
            }
            Text {
                objectName: "mergeSerialNote"
                visible: mergeDialog.problem === "" && mergeDialog.serialNote !== ""
                text: mergeDialog.serialNote
                color: Theme.accent
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            RadioButton {
                id: keepTimesChoice
                objectName: "mergeKeepTimes"
                text: "Add the surface time"
                checked: true
            }
            Text {
                text: "The surface time between the logs stays in the dive, filled with the previous log's last sample: every log keeps its clock times, so photos and videos still line up, and the dive ends when the last log did."
                color: Theme.muted
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                leftPadding: 28
                Layout.fillWidth: true
            }
            RadioButton {
                id: closeGapsChoice
                objectName: "mergeCloseGaps"
                text: "Remove the surface time"
            }
            Text {
                text: "The surface time between the logs is taken out: each later log moves back to follow the previous one, so the dive is continuous and ends earlier than the last log did; photos and videos from the later logs no longer match the log."
                color: Theme.muted
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                leftPadding: 28
                Layout.fillWidth: true
            }
            Text {
                objectName: "mergeEditedNote"
                visible: mergeDialog.anyEdited
                text: "Unsaved edits are not in the merged file - save them as .fit first."
                color: Theme.accent
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            Text {
                objectName: "mergeConnectNote"
                text: "Garmin Connect knows a dive by its start time and device; delete the original activities there before uploading the merged file."
                color: Theme.muted
                font.pixelSize: 11
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
        }
        footer: DialogButtonBox {
            Button { objectName: "mergeSaveButton"; text: "Save…"; enabled: mergeDialog.tickedCount > 1 && mergeDialog.problem === ""; DialogButtonBox.buttonRole: DialogButtonBox.AcceptRole }
            Button { objectName: "mergeCancelButton"; text: "Cancel"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole }
        }
        onAccepted: {
            mergeSaveDialog.nameFilters = convertController.saveNameFilters("fit")
            mergeSaveDialog.currentFile = convertController.mergeFileUrl()
            mergeSaveDialog.open()
        }
    }
    FileDialog {
        id: mergeSaveDialog
        title: "Save the merged dive as"
        fileMode: FileDialog.SaveFile
        currentFolder: convertController.dialogFolder
        onAccepted: page.runMerge(selectedFile.toString())
    }
    FileDialog {
        id: debugReportDialog
        objectName: "debugReportDialog"
        property var rows: null
        title: "Save the debug report as"
        fileMode: FileDialog.SaveFile
        nameFilters: ["Text files (*.txt)"]
        currentFolder: convertController.dialogFolder
        onAccepted: page.saveDebugReport(selectedFile.toString())
    }

    // The dialogs open in Documents the first time, then in the last folder
    // used (desktop_prefs.json, decision Q11); the controller remembers it.
    FileDialog {
        id: openDialog
        title: "Open dive files"
        fileMode: FileDialog.OpenFiles
        nameFilters: convertController.openNameFilters
        currentFolder: convertController.dialogFolder
        onAccepted: convertController.openFiles(selectedFiles)
    }
    // Open folder… (I13): every dive file directly in the folder picked
    FolderDialog {
        id: openFolderDialog
        title: "Open the dive files in a folder"
        currentFolder: convertController.dialogFolder
        onAccepted: convertController.openFolder(selectedFolder.toString())
    }
    // Open Cached (I9c): the same dialog in the Garmin FIT cache of the
    // Garmin page's account; opening from there leaves the last folder alone.
    FileDialog {
        id: cachedDialog
        title: "Open cached Garmin dive files"
        fileMode: FileDialog.OpenFiles
        nameFilters: convertController.openNameFilters
        currentFolder: convertController.cacheFolder
        onAccepted: convertController.openCachedFiles(selectedFiles)
    }
    FileDialog {
        id: saveDialog
        property string target: ""
        title: "Save dive as"
        fileMode: FileDialog.SaveFile
        currentFolder: convertController.dialogFolder
        onAccepted: convertController.saveAs(target, selectedFile.toString())
    }
    FolderDialog {
        id: folderDialog
        property string target: ""
        title: "Save one file per dive into"
        currentFolder: convertController.dialogFolder
        onAccepted: convertController.saveEach(target, selectedFolder.toString())
    }
}
