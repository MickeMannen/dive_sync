import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes

ColumnLayout {
    id: page
    spacing: 12
    property var typeIcons: ({ text: "T", number: "#", datetime: "⏱", gps: "⌖", list: "≡", tanks: "🛢", samples: "📈" })
    property string dragKey: ""

    Component.onCompleted: mappingController.reload()

    Card {
        title: "Mapping board" + (mappingController.dirty ? "  ●" : "")
        RowLayout {
            spacing: 16
            ColumnLayout {
                spacing: 2
                Text { text: "Pair"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: pairBox
                    Layout.preferredWidth: 260
                    model: mappingController.pairs
                    textRole: "label"
                    valueRole: "id"
                    onActivated: { mappingController.selectPair(currentValue); conflictsController.load(currentValue) }
                }
            }
            ColumnLayout {
                spacing: 2
                Text { text: "Direction"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: dirBox
                    Layout.preferredWidth: 160
                    model: mappingController.directions
                    textRole: "label"
                    valueRole: "value"
                    Component.onCompleted: currentIndex = Math.max(0, indexOfValue(mappingController.pairDirection))
                    Connections { target: mappingController; function onBoardChanged() { dirBox.currentIndex = Math.max(0, dirBox.indexOfValue(mappingController.pairDirection)) } }
                }
            }
            LabeledField { id: graceField; label: "Grace window (min)"; fieldWidth: 90; text: String(mappingController.pairGrace) }
            Button { text: "Save pair options"; onClicked: mappingController.savePairOptions(dirBox.currentValue, parseInt(graceField.text) || 0) }
            Text { text: mappingController.matchKeys; color: Theme.muted; font.pixelSize: 11 }
        }

        Item {
            id: boardArea
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(sourceColumn.implicitHeight, targetColumn.implicitHeight) + 30

            FieldColumn { id: sourceColumn; x: 0; width: (boardArea.width - 120) / 2; title: mappingController.sourceName; fields: mappingController.sourceFields }
            FieldColumn { id: targetColumn; x: boardArea.width - width; width: (boardArea.width - 120) / 2; title: mappingController.targetName; fields: mappingController.targetFields }

            Shape {
                id: lines
                anchors.fill: parent
                z: -1
                Repeater {
                    model: page.linkSegments()
                    delegate: ShapePath {
                        required property var modelData
                        strokeWidth: modelData.selected ? 3.5 : 2
                        strokeColor: modelData.off ? Theme.muted : Theme.accent
                        fillColor: "transparent"
                        strokeStyle: modelData.off ? ShapePath.DashLine : ShapePath.SolidLine
                        startX: modelData.x1; startY: modelData.y1
                        PathCubic { x: modelData.x2; y: modelData.y2; control1X: (modelData.x1 + modelData.x2) / 2; control1Y: modelData.y1; control2X: (modelData.x1 + modelData.x2) / 2; control2Y: modelData.y2 }
                    }
                }
            }
        }
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 11
            text: "Drag a field onto a field in the other column to link them. Click a row below to edit the link. Drop a further field onto an already linked target to build a composite. Fields without a link are not synced; 🔒 fields cannot be written by their service."
        }

        // link rows
        Repeater {
            model: mappingController.links
            delegate: Rectangle {
                required property var modelData
                Layout.fillWidth: true
                implicitHeight: 28
                radius: 4
                color: modelData.id === mappingController.selectedId ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.15) : "transparent"
                RowLayout {
                    anchors { fill: parent; leftMargin: 6; rightMargin: 6 }
                    Text { text: modelData.id; color: Theme.text; font.bold: true; Layout.preferredWidth: 120; elide: Text.ElideRight }
                    Text { text: modelData.source_labels.join(" + ") + (modelData.template ? " (template)" : ""); color: modelData.direction === "off" ? Theme.muted : Theme.text; Layout.preferredWidth: 220; elide: Text.ElideRight }
                    Text { text: ({ bidirectional: "↔", to_target: "→", to_source: "←", off: "off" })[modelData.direction]; color: Theme.muted }
                    Text { text: modelData.target_label; color: modelData.direction === "off" ? Theme.muted : Theme.text; Layout.preferredWidth: 160; elide: Text.ElideRight }
                    Text { text: modelData.conflict; color: Theme.muted; Layout.preferredWidth: 120 }
                    Text { text: modelData.match_order !== null && modelData.match_order !== undefined ? "key " + modelData.match_order : ""; color: Theme.muted; Layout.preferredWidth: 50 }
                    Button { text: "Edit"; flat: true; onClicked: mappingController.selectLink(modelData.id) }
                    Button { text: "✕"; flat: true; onClicked: mappingController.deleteLink(modelData.id) }
                }
            }
        }

        // editor
        Rectangle {
            id: editor
            visible: mappingController.selectedId !== ""
            Layout.fillWidth: true
            implicitHeight: editorColumn.implicitHeight + 20
            color: "transparent"
            border.color: Theme.accent
            radius: 6
            property var link: mappingController.selectedLink
            ColumnLayout {
                id: editorColumn
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: 10 }
                spacing: 6
                Text { text: "Link " + (editor.link.source ? editor.link.source.join(" + ") + " → " + editor.link.target : ""); color: Theme.text; font.bold: true }
                RowLayout {
                    spacing: 12
                    LabeledField { id: eId; label: "Id"; fieldWidth: 140; text: editor.link.id || "" }
                    ColumnLayout {
                        spacing: 2
                        Text { text: "Direction"; color: Theme.muted; font.pixelSize: 11 }
                        ComboBox {
                            id: eDir
                            Layout.preferredWidth: 170
                            model: mappingController.allowedDirections
                            textRole: "label"; valueRole: "value"
                            Connections { target: mappingController; function onSelectedChanged() { eDir.currentIndex = Math.max(0, eDir.indexOfValue(mappingController.selectedLink.direction)) } }
                        }
                    }
                    ColumnLayout {
                        spacing: 2
                        Text { text: "Conflict"; color: Theme.muted; font.pixelSize: 11 }
                        ComboBox {
                            id: eConflict
                            Layout.preferredWidth: 220
                            model: [{ value: "prefer_non_empty", label: "Fill blanks only" }, { value: "prefer_source", label: "Mirror source when non-empty" }, { value: "source_wins", label: "Source always wins" }, { value: "target_wins", label: "Target always wins" }, { value: "manual", label: "Ask me (manual)" }]
                            textRole: "label"; valueRole: "value"
                            Connections { target: mappingController; function onSelectedChanged() { eConflict.currentIndex = Math.max(0, eConflict.indexOfValue(mappingController.selectedLink.conflict)) } }
                        }
                    }
                    LabeledField { id: eMatch; label: "Match key order"; fieldWidth: 90; text: editor.link.match_order !== null && editor.link.match_order !== undefined ? String(editor.link.match_order) : "" }
                    LabeledField { id: eSep; label: "Separator"; fieldWidth: 60; text: editor.link.separator || ", " }
                }
                ColumnLayout {
                    spacing: 2
                    Text { text: "Template ({field} placeholders; empty = plain copy)"; color: Theme.muted; font.pixelSize: 11 }
                    RowLayout {
                        TextField {
                            id: eTemplate
                            Layout.fillWidth: true
                            text: editor.link.template || ""
                            font.family: "Menlo"
                            selectByMouse: true
                            onTextEdited: mappingController.previewTemplate(text)
                        }
                        ComboBox {
                            id: fieldPicker
                            Layout.preferredWidth: 200
                            model: editor.link.source || []
                        }
                        Button { text: "Insert"; onClicked: { eTemplate.insert(eTemplate.cursorPosition, "{" + fieldPicker.currentText + "}"); mappingController.previewTemplate(eTemplate.text) } }
                    }
                    Text { text: "Preview: " + mappingController.preview; color: Theme.text; font.family: "Menlo"; font.pixelSize: 11 }
                    Text { text: mappingController.previewProblems; color: Theme.danger; font.pixelSize: 11; visible: text !== ""; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                }
                RowLayout {
                    spacing: 8
                    Button {
                        text: "Apply"
                        onClicked: {
                            var problem = mappingController.updateLink({ id: eId.text, direction: eDir.currentValue, conflict: eConflict.currentValue, match_order: eMatch.text, separator: eSep.text, template: eTemplate.text })
                            if (problem) editorMessage.text = problem; else editorMessage.text = "Applied to the board (not saved yet)."
                        }
                    }
                    Button { text: "Close"; flat: true; onClicked: mappingController.selectLink("") }
                    Button { text: "Delete link"; flat: true; onClicked: mappingController.deleteLink(mappingController.selectedId) }
                    Text { id: editorMessage; color: Theme.muted }
                }
            }
        }

        RowLayout {
            spacing: 8
            Button { text: "Save board"; onClicked: mappingController.save() }
            Button { text: "Cancel"; flat: true; onClicked: mappingController.cancel() }
            Button { text: "Reset to defaults"; flat: true; onClicked: resetDialog.open() }
            Button { text: "Test mapping"; flat: true; enabled: !mappingController.busy; onClicked: mappingController.testMapping() }
            Text { text: mappingController.message; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        }
        ProgressBar { Layout.fillWidth: true; indeterminate: true; visible: mappingController.busy }

        // test result
        ColumnLayout {
            id: testResultBox
            visible: mappingController.testResult.ok === true
            property var result: mappingController.testResult
            Text { text: testResultBox.result.ok ? ("Fetched " + JSON.stringify(testResultBox.result.fetched) + ", " + testResultBox.result.matched + " matched pair(s). Read-only: nothing was written.") : ""; color: Theme.muted; font.pixelSize: 11 }
            Repeater {
                model: testResultBox.result.rows || []
                delegate: RowLayout {
                    required property var modelData
                    Text { text: modelData.dive_time; color: Theme.muted; Layout.preferredWidth: 140; font.pixelSize: 11 }
                    Text { text: modelData.link; color: Theme.text; Layout.preferredWidth: 100; font.pixelSize: 11 }
                    Text { text: modelData.source_key + " = " + JSON.stringify(modelData.source_value); color: Theme.text; Layout.preferredWidth: 260; elide: Text.ElideRight; font.pixelSize: 11 }
                    Text { text: modelData.target_key + " = " + JSON.stringify(modelData.target_value); color: Theme.text; Layout.preferredWidth: 260; elide: Text.ElideRight; font.pixelSize: 11 }
                    Text { text: modelData.result; color: modelData.conflict ? Theme.danger : (modelData.result.indexOf("write") === 0 ? Theme.accent : Theme.muted); font.pixelSize: 11 }
                }
            }
        }
    }

    Dialog {
        id: resetDialog
        title: "Reset to defaults"
        modal: true
        standardButtons: Dialog.Yes | Dialog.No
        anchors.centerIn: Overlay.overlay
        Text { text: "Replace the board with the shipped defaults for this pair? Nothing is saved until you press Save."; color: Theme.text; wrapMode: Text.WordWrap; width: 360 }
        onAccepted: mappingController.resetToDefaults()
    }
    Dialog {
        id: applyAllDialog
        title: "Apply to all dives?"
        modal: true
        standardButtons: Dialog.Yes | Dialog.No
        anchors.centerIn: Overlay.overlay
        Text { text: "Apply the changed mapping to all matched dives on the next run? (No = only new and changed dives)"; color: Theme.text; wrapMode: Text.WordWrap; width: 360 }
        onAccepted: mappingController.applyToAll()
    }
    Connections { target: mappingController; function onAskApplyToAll() { applyAllDialog.open() } }

    // ---- geometry for the lines --------------------------------------
    function linkSegments() {
        var segs = []
        var links = mappingController.links
        for (var i = 0; i < links.length; i++) {
            var l = links[i]
            var to = targetColumn.itemFor(l.target) || sourceColumn.itemFor(l.target)
            if (!to) continue
            for (var j = 0; j < l.source.length; j++) {
                var from = sourceColumn.itemFor(l.source[j]) || targetColumn.itemFor(l.source[j])
                if (!from) continue
                var a = from.mapToItem(boardArea, 0, 0)
                var b = to.mapToItem(boardArea, 0, 0)
                var fromLeft = a.x < b.x
                segs.push({
                    x1: fromLeft ? a.x + from.width : a.x, y1: a.y + from.height / 2,
                    x2: fromLeft ? b.x : b.x + to.width, y2: b.y + to.height / 2,
                    off: l.direction === "off", selected: l.id === mappingController.selectedId
                })
            }
        }
        return segs
    }

    component FieldColumn: ColumnLayout {
        id: col
        property string title: ""
        property var fields: []
        spacing: 4
        function itemFor(key) {
            for (var i = 0; i < repeater.count; i++) {
                var it = repeater.itemAt(i)
                if (it && it.fieldKey === key) return it
            }
            return null
        }
        Text { text: col.title; color: Theme.text; font.bold: true }
        Repeater {
            id: repeater
            model: col.fields
            delegate: Rectangle {
                id: fieldItem
                required property var modelData
                property string fieldKey: modelData.key
                Layout.fillWidth: true
                implicitHeight: 28
                radius: 5
                color: Theme.card
                border.color: dropArea.containsDrag ? (page.dragKey && mappingController.canLink(page.dragKey, fieldKey) === "" ? Theme.accent : Theme.danger) : (modelData.linked ? Theme.accent : Theme.border)
                opacity: modelData.writable ? 1 : 0.75
                ToolTip.text: modelData.key
                ToolTip.visible: hover.hovered
                HoverHandler { id: hover }
                RowLayout {
                    anchors { fill: parent; leftMargin: 8; rightMargin: 8 }
                    Text { text: page.typeIcons[modelData.type] || "?"; color: Theme.muted; Layout.preferredWidth: 18 }
                    Text { text: modelData.label; color: Theme.text; Layout.fillWidth: true; elide: Text.ElideRight }
                    Text { text: modelData.unit || ""; color: Theme.muted; font.pixelSize: 10 }
                    Text { text: modelData.writable ? "" : "🔒"; font.pixelSize: 10 }
                }
                Drag.active: dragHandler.active
                Drag.hotSpot.x: width / 2
                Drag.hotSpot.y: height / 2
                Drag.keys: ["field"]
                DragHandler {
                    id: dragHandler
                    onActiveChanged: {
                        if (active) { page.dragKey = fieldItem.fieldKey; fieldItem.z = 10 }
                        else { fieldItem.Drag.drop(); fieldItem.z = 0; fieldItem.x = 0; fieldItem.y = fieldItem.y; page.dragKey = ""; fieldItem.Layout.fillWidth = false; fieldItem.Layout.fillWidth = true }
                    }
                }
                DropArea {
                    id: dropArea
                    anchors.fill: parent
                    keys: ["field"]
                    onDropped: function (drop) {
                        if (page.dragKey && page.dragKey !== fieldItem.fieldKey) mappingController.createLink(page.dragKey, fieldItem.fieldKey)
                    }
                }
            }
        }
    }
}
