import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

// Convert dive files (plans/convert.md I8): open Garmin .fit files (or
// Connect's zip), UDDF or Subsurface files, see the dives they hold, and
// save the selected ones as UDDF or Subsurface .ssrf - one dive through a
// Save-as dialog, several through a folder dialog with one file per dive -
// or send them to MySSI with the login saved in Settings (the SSI parts are
// hidden while ssi.UPLOAD_ENABLED is off, 2026-10-02). A Garmin .fit whose
// dive is in the app's Garmin cache (by activity id, or by start time when
// the name holds none) gets its site, buddy, notes, weight, visibility and
// tank volumes from there (I9, the checkbox, on by default). The list is a
// working list (I9b): Open adds to it, Remove (or Delete/Backspace) takes
// the selected dives off it, Clear empties it. No sync account is involved;
// nothing is written but the files chosen.
ColumnLayout {
    id: page
    objectName: "convertPage"
    spacing: 12
    readonly property var sel: convertController.selected
    readonly property bool hasDive: !!sel.date_time

    // A read-only value with its label above; hidden when empty, so the grid
    // shows only what the file holds.
    // The cells share the pane's width (fillWidth) rather than keeping a
    // fixed one: four fixed columns ran past the pane at the minimum window.
    component Field: ColumnLayout {
        property string label: ""
        property string value: ""
        property int fieldWidth: 150
        visible: value !== ""
        spacing: 1
        Layout.fillWidth: true
        Layout.preferredWidth: fieldWidth
        Layout.minimumWidth: 90
        Text { text: label; color: Theme.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: true }
        Text { text: value; color: Theme.text; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.WordWrap }
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

    Card {
        id: topCard
        title: "Convert dive files"
        headerContent: [
            Button {
                text: "Open files…"
                objectName: "openFilesButton"
                enabled: !convertController.busy
                Tip { text: "Garmin .fit files or Connect's \"export original\" zip, UDDF and Subsurface (.ssrf) files; several at once. The dives are added to the list."; visible: parent.hovered }
                onClicked: openDialog.open()
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
            text: "Open the files of a dive computer (Open adds to the list; Remove takes dives off it), pick the dives (Cmd/Ctrl-click adds one, Shift-click a range) and save them as UDDF or Subsurface files"
                  + (convertController.ssiEnabled ? ", or send them to your MySSI logbook" : "")
                  + ". Your accounts are not touched; only the files you choose are read and written."
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
                    Tip { text: "Takes the selected dives off the list (Delete or Backspace does the same); the files on disk are not touched"; visible: parent.hovered }
                    onClicked: convertController.removeSelected()
                }
                SmallButton {
                    text: "Clear"
                    objectName: "clearButton"
                    Tip { text: "Empties the list; the files on disk are not touched"; visible: parent.hovered }
                    onClicked: convertController.clear()
                }
            }
            RowLayout {
                spacing: 8
                Layout.leftMargin: 6
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
                    Keys.onDeletePressed: function (event) { convertController.removeSelected(); event.accepted = true }
                    Keys.onPressed: function (event) {
                        if (event.key === Qt.Key_Backspace) { convertController.removeSelected(); event.accepted = true }
                    }
                    delegate: Rectangle {
                        id: row
                        required property int index
                        required property var modelData
                        readonly property bool picked: convertController.selection.indexOf(index) >= 0
                        readonly property bool current: convertController.currentRow === index
                        width: diveList.width
                        implicitHeight: 26
                        color: picked ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, current ? 0.3 : 0.18)
                                      : (index % 2 ? Theme.bg : Theme.card)
                        RowLayout {
                            anchors { fill: parent; leftMargin: 6; rightMargin: 6 }
                            spacing: 8
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
                    title: "Dive"
                    Text {
                        text: page.hasDive ? page.sel.title : "No dive selected"
                        color: Theme.text
                        font.bold: true
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                    Text {
                        visible: page.hasDive && page.sel.source !== ""
                        text: "From " + page.sel.source + (page.sel.external_ids ? " · " + page.sel.external_ids : "")
                        color: Theme.muted
                        font.pixelSize: 11
                    }
                    Text {
                        objectName: "enrichedLine"
                        visible: page.hasDive && (page.sel.enriched || "") !== ""
                        text: page.sel.enriched ? "Filled in: " + page.sel.enriched : ""
                        color: Theme.muted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    GridLayout {
                        visible: page.hasDive
                        Layout.fillWidth: true
                        columns: 4
                        columnSpacing: 16
                        rowSpacing: 8
                        Field { label: "Start"; value: page.sel.date_time || "" }
                        Field { label: "Time zone"; value: page.sel.timezone || "" }
                        Field { label: "Dive #"; value: page.hasDive ? String(page.sel.dive_number) : "" }
                        Field { label: "Duration"; value: page.sel.duration || "" }
                        Field { label: "Max depth"; value: page.sel.max_depth || "" }
                        Field { label: "Avg depth"; value: page.sel.avg_depth || "" }
                        Field { label: "Water temp (min)"; value: page.sel.temp_min || "" }
                        Field { label: "Water temp (max)"; value: page.sel.temp_max || "" }
                        Field { label: "Bottom time"; value: page.sel.bottom_time || "" }
                        Field { label: "Surface interval"; value: page.sel.surface_interval || "" }
                        Field { label: "Dive mode"; value: page.sel.dive_mode || "" }
                        Field { label: "Water"; value: page.sel.water_type || "" }
                        Field { label: "Site"; value: page.sel.location || ""; fieldWidth: 316; Layout.columnSpan: 2 }
                        Field { label: "Buddy"; value: page.sel.buddy || "" }
                        Field { label: "Weight"; value: page.sel.weight || "" }
                        Field { label: "Visibility"; value: page.sel.visibility || "" }
                        Field { label: "Latitude"; value: page.sel.lat || "" }
                        Field { label: "Longitude"; value: page.sel.lng || "" }
                        Field { label: "Exit latitude"; value: page.sel.exit_lat || "" }
                        Field { label: "Exit longitude"; value: page.sel.exit_lng || "" }
                        Field { label: "Dive computer"; value: page.sel.computer || ""; fieldWidth: 316; Layout.columnSpan: 2 }
                        Field { label: "Gradient factors"; value: page.sel.gf || "" }
                        Field { label: "Deco model"; value: page.sel.deco_model || "" }
                        Field { label: "CNS start → end"; value: page.sel.cns || "" }
                        Field { label: "Water density"; value: page.sel.water_density || "" }
                    }
                    ColumnLayout {
                        visible: page.hasDive && (page.sel.notes || "") !== ""
                        spacing: 1
                        Text { text: "Notes"; color: Theme.muted; font.pixelSize: 11 }
                        Text { text: page.sel.notes || ""; color: Theme.text; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                    }
                    ColumnLayout {
                        visible: page.hasDive
                        spacing: 4
                        Text { text: "Tanks / gases"; color: Theme.muted; font.pixelSize: 11 }
                        Text { visible: (page.sel.tanks || []).length === 0; text: "—"; color: Theme.text }
                        RowLayout {
                            visible: (page.sel.tanks || []).length > 0
                            spacing: 8
                            Cell { text: "#"; cellWidth: 20; color: Theme.muted; font.pixelSize: 11 }
                            Cell { text: "Name"; cellWidth: 140; color: Theme.muted; font.pixelSize: 11 }
                            Cell { text: "Mix"; cellWidth: 90; color: Theme.muted; font.pixelSize: 11 }
                            Cell { text: "Volume"; cellWidth: 60; color: Theme.muted; font.pixelSize: 11 }
                            Cell { text: "Start"; cellWidth: 70; color: Theme.muted; font.pixelSize: 11 }
                            Cell { text: "End"; cellWidth: 70; color: Theme.muted; font.pixelSize: 11 }
                            Cell { text: "Role"; cellWidth: 90; color: Theme.muted; font.pixelSize: 11 }
                        }
                        Repeater {
                            model: page.sel.tanks || []
                            delegate: RowLayout {
                                required property var modelData
                                spacing: 8
                                Cell { text: String(modelData.index); cellWidth: 20 }
                                Cell { text: modelData.name; cellWidth: 140 }
                                Cell { text: modelData.mix; cellWidth: 90 }
                                Cell { text: modelData.volume; cellWidth: 60 }
                                Cell { text: modelData.start_pressure; cellWidth: 70 }
                                Cell { text: modelData.end_pressure; cellWidth: 70 }
                                Cell { text: modelData.role; cellWidth: 90; color: Theme.muted }
                            }
                        }
                    }
                    // the depth profile (ProfileChart.qml, shared with the dives pages)
                    ProfileChart { Layout.fillWidth: true; samples: page.sel.samples || [] }
                    Text {
                        visible: page.hasDive
                        text: page.sel.sample_count > 0
                              ? (page.sel.sample_count + " samples"
                                 + ((page.sel.channels || []).length > 0 ? " · also logged: " + page.sel.channels.join(", ") : ""))
                              : "No dive profile in the file"
                        color: Theme.muted
                        font.pixelSize: 11
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }
                    ColumnLayout {
                        visible: page.hasDive && page.sel.event_count > 0
                        spacing: 1
                        Text { text: "Events (" + (page.hasDive ? page.sel.event_count : 0) + ")"; color: Theme.muted; font.pixelSize: 11 }
                        Repeater {
                            model: page.sel.events || []
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
                    text: "Save as " + modelData.label + "…"
                    enabled: convertController.selectedCount > 0 && !convertController.busy
                    Tip {
                        text: (convertController.selectedCount > 1
                               ? "Writes one " + modelData.extension + " file per selected dive into a folder you pick"
                               : "Writes the selected dive as a " + modelData.extension + " file")
                        visible: parent.hovered
                    }
                    onClicked: page.startSave(modelData.id)
                }
            }
            // Hidden while ssi.UPLOAD_ENABLED is off (2026-10-02, until the owner's live test)
            Button {
                text: "Send to SSI…"
                objectName: "sendToSsiButton"
                visible: convertController.ssiEnabled
                enabled: convertController.selectedCount > 0 && !convertController.busy && !convertController.ssiBusy && !convertController.ssiPlanOpen
                Tip { text: "Uploads the selected dives to your MySSI logbook with the login saved in Settings. Unofficial: " + convertController.ssiNote; visible: parent.hovered }
                onClicked: convertController.prepareSsi()
            }
            Text {
                visible: convertController.selectedCount > 0
                text: convertController.selectedCount === 1 ? "1 dive selected" : convertController.selectedCount + " dives selected, one file per dive"
                color: Theme.muted
                font.pixelSize: 12
            }
            Item { Layout.fillWidth: true }
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

        // ---- MySSI: the plan (sites to check), then the results ----------
        ColumnLayout {
            id: ssiSection
            objectName: "ssiSection"
            Layout.fillWidth: true
            spacing: 6
            visible: convertController.ssiEnabled && (convertController.ssiBusy || convertController.ssiPlanOpen || convertController.ssiMessage !== "" || convertController.ssiResults.length > 0)
            ProgressBar { Layout.fillWidth: true; indeterminate: true; visible: convertController.ssiBusy }
            Text { visible: text !== ""; text: convertController.ssiMessage; color: Theme.text; font.pixelSize: 12; wrapMode: Text.WordWrap; Layout.fillWidth: true }
            Text {
                visible: convertController.ssiPlanOpen
                text: "Unofficial: " + convertController.ssiNote
                color: Theme.muted
                font.pixelSize: 11
                font.italic: true
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }
            RowLayout {
                visible: convertController.ssiPlanOpen
                spacing: 8
                Text { text: convertController.ssiLoginStatus; color: Theme.muted; font.pixelSize: 11 }
                Text { text: "·"; color: Theme.muted; font.pixelSize: 11 }
                Text { text: convertController.ssiSitesStatus; color: Theme.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                Button {
                    text: convertController.ssiSitesDownloaded ? "Update site database" : "Download site database"
                    flat: true
                    enabled: !convertController.ssiBusy
                    onClicked: convertController.downloadSites()
                }
            }
            // The plan (one box per dive) and the results scroll in a bounded
            // area, so a long batch never squeezes the dive list above away.
            ScrollView {
                id: ssiScroll
                Layout.fillWidth: true
                // what is left after the top card, the dive list's floor and
                // this card's own lines, so Send stays on screen at any height
                readonly property int roomLeft: page.height - topCard.height - 240 - 250
                Layout.preferredHeight: Math.min(ssiContent.implicitHeight, Math.max(110, roomLeft))
                visible: (convertController.ssiPlanOpen && convertController.ssiPlan.length > 0) || convertController.ssiResults.length > 0
                contentWidth: availableWidth
                clip: true
                ColumnLayout {
                    id: ssiContent
                    width: ssiScroll.availableWidth
                    spacing: 6
                    Repeater {
                        model: convertController.ssiPlanOpen ? convertController.ssiPlan : []
                        delegate: Rectangle {
                            id: planRow
                            required property var modelData
                            property var results: []
                            readonly property bool planned: modelData.status === "planned"
                            Layout.fillWidth: true
                            implicitHeight: planColumn.implicitHeight + 16
                            radius: 6
                            color: "transparent"
                            border.color: Theme.border
                            ColumnLayout {
                                id: planColumn
                                anchors { left: parent.left; right: parent.right; top: parent.top; margins: 8 }
                                spacing: 4
                                RowLayout {
                                    spacing: 10
                                    Text { text: planRow.modelData.when; color: Theme.text; font.bold: true; font.pixelSize: 12 }
                                    Text {
                                        text: planRow.planned ? "will be sent as dive " + planRow.modelData.number : "skipped: " + planRow.modelData.reason
                                        color: planRow.planned ? Theme.ok : Theme.muted
                                        font.pixelSize: 12
                                        wrapMode: Text.WordWrap
                                        Layout.fillWidth: true
                                    }
                                }
                                RowLayout {
                                    visible: planRow.planned
                                    spacing: 8
                                    Text { text: "SSI site:"; color: Theme.muted; font.pixelSize: 11 }
                                    Text {
                                        text: planRow.modelData.site_label !== "" ? planRow.modelData.site_label : "none (sent without a site)"
                                        color: planRow.modelData.site_label !== "" ? Theme.text : Theme.muted
                                        font.pixelSize: 12
                                        elide: Text.ElideRight
                                        Layout.maximumWidth: 320
                                    }
                                    TextField {
                                        id: siteSearch
                                        Layout.preferredWidth: 220
                                        placeholderText: "search SSI sites…"
                                        font.pixelSize: 12
                                        onTextEdited: planRow.results = convertController.searchSites(text)
                                    }
                                    Button {
                                        text: "No site"
                                        flat: true
                                        visible: planRow.modelData.site_label !== ""
                                        onClicked: { convertController.clearSite(planRow.modelData.row); siteSearch.text = ""; planRow.results = [] }
                                    }
                                }
                                Flow {
                                    visible: planRow.planned && planRow.results.length > 0
                                    Layout.fillWidth: true
                                    spacing: 4
                                    Repeater {
                                        model: planRow.results
                                        delegate: Button {
                                            required property var modelData
                                            text: modelData.label
                                            flat: true
                                            font.pixelSize: 11
                                            onClicked: { convertController.setSite(planRow.modelData.row, modelData.id); siteSearch.text = ""; planRow.results = [] }
                                        }
                                    }
                                }
                                Repeater {
                                    model: planRow.planned ? planRow.modelData.dropped : []
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
                        }
                    }
                    Repeater {
                        model: convertController.ssiResults
                        delegate: Text {
                            required property var modelData
                            text: (modelData.status === "sent" ? "✓ " : modelData.status === "skipped" ? "– " : "✗ ") + modelData.summary
                            color: modelData.status === "sent" ? Theme.ok : modelData.status === "failed" ? Theme.danger : Theme.muted
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }
                    }
                }
            }
            RowLayout {
                visible: convertController.ssiPlanOpen
                spacing: 8
                Button {
                    objectName: "ssiSendButton"
                    text: "Send " + convertController.ssiPlannedCount + (convertController.ssiPlannedCount === 1 ? " dive" : " dives") + " to MySSI"
                    highlighted: true
                    enabled: convertController.ssiPlannedCount > 0 && !convertController.ssiBusy
                    onClicked: convertController.sendToSsi()
                }
                Button { text: "Cancel"; flat: true; enabled: !convertController.ssiBusy; onClicked: convertController.cancelSsi() }
            }
        }
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
