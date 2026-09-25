import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes

// Mapping board, rebuilt around receiver rules (rework.md G6): one panel per
// receiving side - "Y → X" - with the sender's fields on the
// left (drag sources) and the receiver's fields on the right (drop targets,
// each showing its rule), a match-key strip and a rule editor.
ColumnLayout {
    id: page
    spacing: 12
    property var typeIcons: ({ text: "T", number: "#", datetime: "⏱", gps: "⌖", list: "≡", tanks: "🛢", samples: "📈" })
    property string dragKey: ""
    property string dragReceiver: ""
    property string dragRole: ""       // "sender" or "receiver": which column the drag started in
    // Bumped whenever a field row's on-screen position could have changed, so
    // the connector geometry (read via mapToItem(), which the binding engine
    // cannot track) is forced to recompute - see the earlier note in git
    // history: without it every line read y=0 and never moved again.
    property int layoutGen: 0

    Component.onCompleted: mappingController.reload()
    focus: true
    Keys.onEscapePressed: function (event) { event.accepted = mappingController.clearArmed() }

    // Release of a dragged field row: hand it to the DropArea under it and say
    // so when nothing took it, instead of silently snapping back.
    function finishDrag(item) {
        var action = item.Drag.drop()
        if (action === Qt.IgnoreAction)
            mappingController.notify("Drop a field onto a field in the other column of the same panel to add a rule.")
        page.dragKey = ""; page.dragReceiver = ""; page.dragRole = ""
    }

    Card {
        title: "Mapping board" + (mappingController.dirty ? "  ●" : "")
        RowLayout {
            spacing: 16
            // What the board shows: the rules for writing Target from Source.
            ColumnLayout {
                spacing: 2
                Text { text: "Source (read from)"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: sourceBox
                    objectName: "mapSourceBox"
                    Layout.preferredWidth: 200
                    model: mappingController.endpoints
                    textRole: "label"
                    valueRole: "id"
                    Component.onCompleted: currentIndex = Math.max(0, indexOfValue(mappingController.viewSource))
                    onActivated: {
                        var targets = mappingController.targetsFor(currentValue)
                        var keep = targetBox.currentValue
                        var target = targets.some(function (t) { return t.id === keep }) ? keep : (targets.length ? targets[0].id : "")
                        page.showView(currentValue, target)
                    }
                }
            }
            Text { text: "→"; color: Theme.muted; font.pixelSize: 18; Layout.alignment: Qt.AlignBottom; Layout.bottomMargin: 6 }
            ColumnLayout {
                spacing: 2
                Text { text: "Target (written to)"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: targetBox
                    objectName: "mapTargetBox"
                    Layout.preferredWidth: 200
                    model: mappingController.targetsFor(mappingController.viewSource)
                    textRole: "label"
                    valueRole: "id"
                    Component.onCompleted: currentIndex = Math.max(0, indexOfValue(mappingController.viewTarget))
                    onActivated: page.showView(sourceBox.currentValue, currentValue)
                }
            }
            Button {
                text: "⇄"
                flat: true
                Layout.alignment: Qt.AlignBottom
                ToolTip.text: "Swap source and target: the rules for the other direction"
                ToolTip.visible: hovered
                onClicked: page.showView(mappingController.viewTarget, mappingController.viewSource)
            }
            Connections {
                target: mappingController
                function onBoardChanged() {
                    if (!sourceBox.popup.visible) sourceBox.currentIndex = Math.max(0, sourceBox.indexOfValue(mappingController.viewSource))
                    if (!targetBox.popup.visible) targetBox.currentIndex = Math.max(0, targetBox.indexOfValue(mappingController.viewTarget))
                    if (!dirBox.popup.visible) dirBox.currentIndex = Math.max(0, dirBox.indexOfValue(mappingController.savedDirection))
                }
            }
            Item { Layout.fillWidth: true }
            Text { visible: mappingController.dirty; text: "● unsaved changes"; color: Theme.accent; font.pixelSize: 11 }
            Button { text: "Save board"; enabled: mappingController.dirty; onClicked: mappingController.save() }
            Button { text: "Cancel"; flat: true; enabled: mappingController.dirty; onClicked: mappingController.cancel() }
        }
        Button {
            id: helpToggle
            flat: true
            padding: 0
            checkable: true
            text: (checked ? "▾ " : "▸ ") + "How to edit the board"
            font.pixelSize: 11
        }
        Text {
            visible: helpToggle.checked
            Layout.leftMargin: 16
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 11
            text: "A sync run writes one side: the board shows what the target accepts from the source (⇄ shows the other direction). To add a rule either drag a field from the left-hand list onto a field on the right, or - easier over a long list - click the left-hand field once and then click the right-hand field to take it. Do the same onto a field that already has a rule to build a composite. Drag the same text field onto a second field to split it: e.g. Activity name onto Location and then onto Dive site takes \"Gozo, Blue Hole\" apart into both. Click a rule to change its policy or template. 🔒 fields cannot be written by their service; fields without a rule are not synced."
        }

        Rectangle {
            visible: mappingController.armedKey !== ""
            Layout.fillWidth: true
            implicitHeight: 26
            radius: 4
            color: Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.15)
            RowLayout {
                anchors { fill: parent; leftMargin: 8; rightMargin: 8 }
                Text { text: "Taking " + mappingController.armedLabel + " - click a field on the right to receive it"; color: Theme.text; font.pixelSize: 11; Layout.fillWidth: true }
                Button { text: "Cancel"; flat: true; onClicked: mappingController.clearArmed() }
            }
        }

        // ---- one panel per receiver ------------------------------------
        Repeater {
            id: panelRepeater
            objectName: "panelRepeater"
            model: mappingController.panels
            delegate: ReceiverPanel {
                required property var modelData
                panel: modelData
                Layout.fillWidth: true
            }
        }

        // ---- match keys ---------------------------------------------------
        Flow {
            Layout.fillWidth: true
            spacing: 8
            Text { text: "Match keys"; color: Theme.text; font.bold: true; font.pixelSize: 12 }
            Text { text: mappingController.matchKeys.length === 0 ? "none (start time only)" : ""; color: Theme.muted; font.pixelSize: 11; visible: text !== "" }
            Repeater {
                model: mappingController.matchKeys
                delegate: RowLayout {
                    required property var modelData
                    required property int index
                    spacing: 2
                    Text { text: (index + 1) + ". " + (mappingController.matchKeyRows[index] || ""); color: Theme.text; font.pixelSize: 11 }
                    Button { text: "↑"; flat: true; enabled: index > 0; onClicked: mappingController.moveMatchKeyUp(index) }
                    Button { text: "✕"; flat: true; onClicked: mappingController.removeMatchKey(index) }
                }
            }
            Text { text: mappingController.sourceName; color: Theme.muted; font.pixelSize: 11 }
            ComboBox { id: keyA; Layout.preferredWidth: 200; model: mappingController.matchKeyFieldsSource; textRole: "label"; valueRole: "value" }
            Text { text: "="; color: Theme.muted }
            Text { text: mappingController.targetName; color: Theme.muted; font.pixelSize: 11 }
            ComboBox { id: keyB; Layout.preferredWidth: 200; model: mappingController.matchKeyFieldsTarget; textRole: "label"; valueRole: "value" }
            Button { text: "Add match key"; flat: true; onClicked: { var problem = mappingController.addMatchKey(keyA.currentValue || "", keyB.currentValue || ""); if (problem) editorMessage.text = problem } }
            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "Dives are paired by start time first; a match key pairs the ones whose clocks disagree (e.g. a time-zone shift) within 24 hours."
                color: Theme.muted
                font.pixelSize: 11
            }
        }

        // ---- rule editor --------------------------------------------------
        Rectangle {
            id: editor
            visible: mappingController.selectedId !== ""
            Layout.fillWidth: true
            implicitHeight: editorColumn.implicitHeight + 20
            color: "transparent"
            border.color: Theme.accent
            radius: 6
            property var rule: mappingController.selectedRule
            ColumnLayout {
                id: editorColumn
                anchors { left: parent.left; right: parent.right; top: parent.top; margins: 10 }
                spacing: 6
                Text { text: "Rule " + (editor.rule.title || ""); color: Theme.text; font.bold: true }
                RowLayout {
                    spacing: 12
                    ColumnLayout {
                        spacing: 2
                        Text { text: "When the two sides differ"; color: Theme.muted; font.pixelSize: 11 }
                        ComboBox {
                            id: eConflict
                            Layout.preferredWidth: 360
                            model: mappingController.policies
                            textRole: "label"; valueRole: "value"
                            Connections { target: mappingController; function onSelectedChanged() { eConflict.currentIndex = Math.max(0, eConflict.indexOfValue(mappingController.selectedRule.conflict)) } }
                        }
                    }
                    LabeledField { id: eSep; visible: editor.rule.list_rule === true; label: "List separator"; fieldWidth: 60; text: editor.rule.separator || ", " }
                }
                Button {
                    id: templateToggle
                    visible: editor.rule.text_target === true && editor.rule.composite !== true
                    flat: true
                    padding: 0
                    checkable: true
                    checked: false
                    text: (checked ? "▾ " : "▸ ") + "Combine or reformat with a template"
                    font.pixelSize: 11
                    Connections { target: mappingController; function onSelectedChanged() { templateToggle.checked = false } }
                }
                ColumnLayout {
                    spacing: 2
                    visible: editor.rule.text_target === true && (editor.rule.composite === true || templateToggle.checked)
                    Text { text: "Template ({field} placeholders; empty = plain copy)"; color: Theme.muted; font.pixelSize: 11 }
                    RowLayout {
                        TextField {
                            id: eTemplate
                            Layout.fillWidth: true
                            text: editor.rule.template || ""
                            font.family: "Menlo"
                            selectByMouse: true
                            onTextEdited: mappingController.previewTemplate(text)
                        }
                        ComboBox {
                            id: fieldPicker
                            Layout.preferredWidth: 200
                            model: editor.rule.source || []
                        }
                        Button { text: "Insert"; onClicked: { eTemplate.insert(eTemplate.cursorPosition, "{" + fieldPicker.currentText + "}"); mappingController.previewTemplate(eTemplate.text) } }
                    }
                    Text { text: "Preview: " + mappingController.preview; color: Theme.text; font.family: "Menlo"; font.pixelSize: 11 }
                    Text { text: mappingController.previewProblems; color: Theme.danger; font.pixelSize: 11; visible: text !== ""; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                }
                ColumnLayout {
                    spacing: 4
                    visible: editor.rule.text_target === true && editor.rule.composite === true
                    CheckBox {
                        id: eSplitOn
                        objectName: "splitCheck"
                        text: editor.rule.split_label || "Split it back into its fields"
                        checked: editor.rule.split_active === true
                        onToggled: mappingController.previewReverse(checked ? (eReverse.text || "auto") : "")
                    }
                    ColumnLayout {
                        spacing: 4
                        visible: eSplitOn.checked
                        Layout.leftMargin: 24
                        RowLayout {
                            Text { text: "Policy of that split"; color: Theme.muted; font.pixelSize: 11 }
                            ComboBox {
                                id: eSplit
                                Layout.preferredWidth: 420
                                model: mappingController.splitPolicies
                                textRole: "label"; valueRole: "value"
                                Connections { target: mappingController; function onSelectedChanged() { eSplit.currentIndex = Math.max(0, eSplit.indexOfValue(mappingController.selectedRule.reverse_conflict || "")) } }
                            }
                        }
                        Text {
                            text: "The value is cut at the text between the fields in the template: {location}, {divesite} splits \"Gozo, Blue Hole\" at the first comma. A value without that text is left alone."
                            color: Theme.muted; font.pixelSize: 11; wrapMode: Text.WordWrap; Layout.fillWidth: true
                        }
                        Button {
                            id: advancedToggle
                            flat: true
                            padding: 0
                            checkable: true
                            checked: (editor.rule.reverse_custom || "") !== ""
                            text: (checked ? "▾ " : "▸ ") + "Custom split pattern (regex)"
                            font.pixelSize: 11
                        }
                        TextField {
                            id: eReverse
                            visible: advancedToggle.checked
                            Layout.fillWidth: true
                            text: editor.rule.reverse_custom || ""
                            font.family: "Menlo"
                            selectByMouse: true
                            placeholderText: "blank = split on the template, e.g. (?P<location>.+?), (?P<divesite>.+)"
                            onTextEdited: mappingController.previewReverse(text || "auto")
                        }
                    }
                }
                Button {
                    id: advancedRuleToggle
                    flat: true
                    padding: 0
                    checkable: true
                    text: (checked ? "▾ " : "▸ ") + "Advanced"
                    font.pixelSize: 11
                }
                LabeledField { id: eId; visible: advancedRuleToggle.checked; label: "Rule id (as stored in settings)"; fieldWidth: 200; text: editor.rule.id || "" }
                RowLayout {
                    spacing: 8
                    Button {
                        text: "Apply"
                        onClicked: {
                            var problem = mappingController.updateRule({ id: eId.text, conflict: eConflict.currentValue, separator: eSep.text, template: eTemplate.text, split: eSplitOn.checked, reverse: eReverse.text, reverse_conflict: eSplit.currentValue || "" })
                            if (problem) editorMessage.text = problem; else editorMessage.text = "Applied to the board (not saved yet)."
                        }
                    }
                    Button { text: "Close"; flat: true; onClicked: mappingController.clearSelection() }
                    Button { text: "Delete rule"; flat: true; onClicked: mappingController.deleteRule(mappingController.selectedReceiver, mappingController.selectedId) }
                    Text { id: editorMessage; color: Theme.muted }
                }
            }
        }

        RowLayout {
            spacing: 8
            Button { text: "Reset to defaults"; flat: true; onClicked: resetDialog.open() }
            Button { text: "Test mapping"; flat: true; enabled: !mappingController.busy; onClicked: mappingController.testMapping() }
            Text { text: mappingController.message; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        }
        ProgressBar { Layout.fillWidth: true; indeterminate: true; visible: mappingController.busy }

        // ---- test result --------------------------------------------------
        ColumnLayout {
            id: testResultBox
            visible: mappingController.testResult.ok === true
            property var result: mappingController.testResult
            Text { text: testResultBox.result.ok ? ("Direction " + testResultBox.result.directionality + " (writes " + testResultBox.result.receiver + "): fetched " + JSON.stringify(testResultBox.result.fetched) + ", " + testResultBox.result.matched + " matched pair(s). Read-only: nothing was written.") : ""; color: Theme.muted; font.pixelSize: 11 }
            Repeater {
                model: testResultBox.result.rows || []
                delegate: RowLayout {
                    required property var modelData
                    Text { text: modelData.dive_time; color: Theme.muted; Layout.preferredWidth: 140; font.pixelSize: 11 }
                    Text { text: modelData.link + (modelData.split ? " (split)" : ""); color: Theme.text; Layout.preferredWidth: 110; font.pixelSize: 11; elide: Text.ElideRight }
                    Text { text: modelData.receiver || ""; color: Theme.muted; Layout.preferredWidth: 80; font.pixelSize: 11 }
                    Text { text: modelData.source_key + " = " + JSON.stringify(modelData.source_value); color: Theme.text; Layout.preferredWidth: 240; elide: Text.ElideRight; font.pixelSize: 11 }
                    Text { text: modelData.target_key + " = " + JSON.stringify(modelData.target_value); color: Theme.text; Layout.preferredWidth: 240; elide: Text.ElideRight; font.pixelSize: 11 }
                    Text { text: modelData.result; color: modelData.conflict ? Theme.danger : (modelData.result.indexOf("write") === 0 ? Theme.accent : Theme.muted); font.pixelSize: 11 }
                }
            }
        }
    }

    Card {
        title: "Pair options - " + mappingController.pairLabel
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 11
            text: "Used by this pair's scheduled runs and the web dashboard's Sync now; the Sync page sets its own direction."
        }
        RowLayout {
            spacing: 16
            ColumnLayout {
                spacing: 2
                Text { text: "Scheduled runs write to"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: dirBox
                    Layout.preferredWidth: 160
                    model: mappingController.directions
                    textRole: "label"
                    valueRole: "value"
                    ToolTip.text: "The direction this pair's scheduled and default runs write (saved with the pair options). Independent of the source and target on view."
                    ToolTip.visible: hovered
                    Component.onCompleted: currentIndex = Math.max(0, indexOfValue(mappingController.savedDirection))
                }
            }
            LabeledField { id: graceField; label: "Grace window (min)"; fieldWidth: 90; text: String(mappingController.pairGrace) }
            ColumnLayout {
                spacing: 2
                Text { text: " "; color: Theme.muted; font.pixelSize: 11 }
                CheckBox {
                    id: propagateDeletesBox
                    text: "Propagate deletes"
                    checked: mappingController.pairPropagateDeletes
                    Connections { target: mappingController; function onBoardChanged() { propagateDeletesBox.checked = mappingController.pairPropagateDeletes } }
                }
            }
            ColumnLayout {
                spacing: 2
                Text { text: " "; color: Theme.muted; font.pixelSize: 11 }
                CheckBox {
                    id: createOnGarminBox
                    visible: mappingController.pairHasGarmin
                    text: "Create on Garmin"
                    checked: mappingController.pairCreateOnGarmin
                    Connections { target: mappingController; function onBoardChanged() { createOnGarminBox.checked = mappingController.pairCreateOnGarmin } }
                }
            }
            Item { Layout.fillWidth: true }
            Button { text: "Save pair options"; onClicked: mappingController.savePairOptions(dirBox.currentValue, parseInt(graceField.text) || 0, propagateDeletesBox.checked, createOnGarminBox.checked) }
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
    function showView(source, target) {
        mappingController.selectView(source, target)
    }
    Dialog {
        id: splitDialog
        objectName: "splitDialog"
        title: "Split into several fields?"
        modal: true
        standardButtons: Dialog.Yes | Dialog.No
        anchors.centerIn: Overlay.overlay
        property string question: ""
        property var args: []
        Text { text: splitDialog.question; color: Theme.text; wrapMode: Text.WordWrap; width: 420 }
        onAccepted: mappingController.makeSplit(args[0], args[1], args[2])
        onRejected: mappingController.createPlainRule(args[0], args[1], args[2])
    }
    Connections {
        target: mappingController
        function onAskSplit(question, fromKey, receiver, toKey) {
            splitDialog.question = question
            splitDialog.args = [fromKey, receiver, toKey]
            splitDialog.open()
        }
    }

    // ---- one receiver: "Y → X" (sender left, receiver right) --------------------------------
    component ReceiverPanel: Rectangle {
        id: panelRoot
        property var panel: ({})
        objectName: "panel-" + (panel.receiver || "")
        implicitHeight: panelColumn.implicitHeight + 20
        color: "transparent"
        border.color: Theme.border
        radius: 6
        ColumnLayout {
            id: panelColumn
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 10 }
            spacing: 6
            RowLayout {
                spacing: 8
                Text { text: panelRoot.panel.sender_name + " → " + panelRoot.panel.receiver_name; color: Theme.text; font.bold: true }
                Rectangle {
                    radius: 9; implicitHeight: 18; implicitWidth: badgeText.implicitWidth + 14
                    color: panelRoot.panel.scheduled ? Theme.accent : Theme.border
                    Text { id: badgeText; anchors.centerIn: parent; text: panelRoot.panel.scheduled ? "scheduled runs write here" : "scheduled runs write the other way"; color: panelRoot.panel.scheduled ? "white" : Theme.muted; font.pixelSize: 10 }
                }
            }
            Item {
                id: boardArea
                Layout.fillWidth: true
                Layout.preferredHeight: Math.max(receiverColumn.implicitHeight, senderColumn.implicitHeight) + 10

                ReceiverColumn {
                    id: receiverColumn
                    x: boardArea.width - width
                    width: (boardArea.width - 100) * 0.6
                    receiver: panelRoot.panel.receiver
                    otherReceiver: panelRoot.panel.sender
                    title: panelRoot.panel.receiver_name + " fields (drop here)"
                    fields: panelRoot.panel.fields
                    onXChanged: page.layoutGen++
                    onWidthChanged: page.layoutGen++
                    onImplicitHeightChanged: page.layoutGen++
                }
                SenderColumn {
                    id: senderColumn
                    x: 0
                    width: (boardArea.width - 100) * 0.4
                    receiver: panelRoot.panel.receiver
                    title: panelRoot.panel.sender_name + " fields (drag from here)"
                    fields: panelRoot.panel.sender_fields
                    onXChanged: page.layoutGen++
                    onWidthChanged: page.layoutGen++
                    onImplicitHeightChanged: page.layoutGen++
                }

                // One Shape per connector (a Repeater can only instantiate Items,
                // and ShapePath is not one).
                Item {
                    anchors.fill: parent
                    z: -1
                    // capture page now: the panel may be rebuilt (and its context gone) before the call runs
                    Component.onCompleted: { var pg = page; Qt.callLater(function () { pg.layoutGen++ }) }
                    Repeater {
                        model: panelRoot.segments()
                        delegate: Shape {
                            required property var modelData
                            anchors.fill: parent
                            ShapePath {
                                strokeWidth: modelData.selected ? 3.5 : 2
                                strokeColor: modelData.noop ? Theme.muted : Theme.accent
                                fillColor: "transparent"
                                strokeStyle: (modelData.noop || modelData.split) ? ShapePath.DashLine : ShapePath.SolidLine
                                dashPattern: modelData.split ? [5, 2] : [4, 4]
                                startX: modelData.x1; startY: modelData.y1
                                PathCubic { x: modelData.x2; y: modelData.y2; control1X: (modelData.x1 + modelData.x2) / 2; control1Y: modelData.y1; control2X: (modelData.x1 + modelData.x2) / 2; control2Y: modelData.y2 }
                            }
                        }
                    }
                }
            }
        }

        function segments() {
            var _gen = page.layoutGen  // dependency on row geometry, see layoutGen above
            var segs = []
            var rules = panelRoot.panel.rules || []
            for (var i = 0; i < rules.length; i++) {
                var r = rules[i]
                var to = receiverColumn.itemFor(r.target)
                if (!to) continue
                for (var j = 0; j < r.source.length; j++) {
                    var from = senderColumn.itemFor(r.source[j])
                    if (!from) continue
                    var a = from.mapToItem(boardArea, 0, 0)
                    var b = to.mapToItem(boardArea, 0, 0)
                    segs.push({
                        x1: a.x + from.width, y1: a.y + from.height / 2,
                        x2: b.x, y2: b.y + to.height / 2,
                        noop: r.conflict === "target_wins",
                        split: false,
                        selected: mappingController.selectedReceiver === panelRoot.panel.receiver && mappingController.selectedId === r.id
                    })
                }
            }
            // splits: the sender field taken apart into several receiver fields
            var splits = panelRoot.panel.splits || []
            for (var s = 0; s < splits.length; s++) {
                var sp = splits[s]
                var whole = senderColumn.itemFor(sp.target)
                if (!whole) continue
                for (var k = 0; k < sp.source.length; k++) {
                    var part = receiverColumn.itemFor(sp.source[k])
                    if (!part) continue
                    var p1 = whole.mapToItem(boardArea, 0, 0)
                    var p2 = part.mapToItem(boardArea, 0, 0)
                    segs.push({ x1: p1.x + whole.width, y1: p1.y + whole.height / 2, x2: p2.x, y2: p2.y + part.height / 2,
                                noop: false, split: true, selected: sp.selected })
                }
            }
            return segs
        }
    }

    // ---- the receiver's fields: drop targets, each showing its rule ---------
    component ReceiverColumn: ColumnLayout {
        id: col
        property string receiver: ""
        property string otherReceiver: ""
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
        Text { text: col.title; color: Theme.muted; font.pixelSize: 11 }
        Repeater {
            id: repeater
            model: col.fields
            delegate: Rectangle {
                id: fieldItem
                required property var modelData
                property string fieldKey: modelData.key
                objectName: "receiver-" + col.receiver + "-" + fieldKey
                Layout.fillWidth: true
                implicitHeight: 28
                radius: 5
                color: modelData.selected ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.15) : Theme.card
                border.color: dropArea.containsDrag ? (page.dragKey && page.dragRole === "sender" && mappingController.canDrop(page.dragKey, col.receiver, fieldKey) === "" ? Theme.accent : Theme.danger) : (modelData.linked ? Theme.accent : Theme.border)
                opacity: modelData.writable ? 1 : 0.75
                ToolTip.text: modelData.key + (modelData.rule_id ? "\nrule " + modelData.rule_id : "")
                ToolTip.visible: hover.hovered
                HoverHandler { id: hover }
                onXChanged: page.layoutGen++
                onYChanged: page.layoutGen++
                RowLayout {
                    anchors { fill: parent; leftMargin: 8; rightMargin: 4 }
                    Text { text: page.typeIcons[modelData.type] || "?"; color: Theme.muted; Layout.preferredWidth: 18 }
                    Text { text: modelData.label; color: Theme.text; elide: Text.ElideRight; Layout.preferredWidth: 130 }
                    Text { text: modelData.unit || ""; color: Theme.muted; font.pixelSize: 10 }
                    Text { text: modelData.writable ? "" : "🔒"; font.pixelSize: 10 }
                    Text {
                        text: modelData.rule_summary
                        color: Theme.muted
                        font.pixelSize: 11
                        font.strikeout: modelData.rule_noop
                        elide: Text.ElideLeft
                        horizontalAlignment: Text.AlignRight
                        Layout.fillWidth: true
                        TapHandler {
                            onTapped: {
                                if (mappingController.armedKey !== "") mappingController.connectArmed(col.receiver, fieldItem.fieldKey)
                                else if (modelData.rule_id) mappingController.selectRule(col.receiver, modelData.rule_id)
                            }
                        }
                    }
                    Button { text: "✕"; flat: true; visible: modelData.rule_id !== ""; implicitWidth: 24; implicitHeight: 24; onClicked: mappingController.deleteRule(col.receiver, modelData.rule_id) }
                    Text {
                        visible: modelData.split_id !== ""
                        text: modelData.split_summary
                        color: Theme.muted
                        font.pixelSize: 11
                        font.italic: true
                        elide: Text.ElideLeft
                        horizontalAlignment: Text.AlignRight
                        Layout.fillWidth: modelData.rule_id === ""
                        TapHandler {
                            onTapped: {
                                if (mappingController.armedKey !== "") mappingController.connectArmed(col.receiver, fieldItem.fieldKey)
                                else mappingController.selectRuleFrom(col.otherReceiver, modelData.split_id, col.receiver)
                            }
                        }
                    }
                }
                TapHandler {
                    onTapped: {
                        if (mappingController.armedKey !== "") mappingController.connectArmed(col.receiver, fieldItem.fieldKey)
                        else if (modelData.rule_id) mappingController.selectRule(col.receiver, modelData.rule_id)
                        else if (modelData.split_id) mappingController.selectRuleFrom(col.otherReceiver, modelData.split_id, col.receiver)
                    }
                }
                // A receiver field can be dragged onto a sender field as well:
                // the rule is the same either way (this field <- that one).
                Drag.active: dragHandler.active
                Drag.hotSpot.x: width / 2
                Drag.hotSpot.y: height / 2
                Drag.keys: ["field"]
                DragHandler {
                    id: dragHandler
                    enabled: modelData.writable
                    grabPermissions: PointerHandler.CanTakeOverFromItems | PointerHandler.CanTakeOverFromHandlersOfDifferentType | PointerHandler.ApprovesTakeOverByHandlersOfSameType
                    onActiveChanged: {
                        if (active) { page.dragKey = fieldItem.fieldKey; page.dragReceiver = col.receiver; page.dragRole = "receiver"; fieldItem.z = 10 }
                        else { page.finishDrag(fieldItem); fieldItem.z = 0; fieldItem.x = 0; fieldItem.y = fieldItem.y; fieldItem.Layout.fillWidth = false; fieldItem.Layout.fillWidth = true }
                    }
                }
                DropArea {
                    id: dropArea
                    anchors.fill: parent
                    keys: ["field"]
                    onDropped: function (drop) {
                        if (!page.dragKey || page.dragReceiver !== col.receiver) { drop.accepted = false; return }
                        if (page.dragRole === "sender") drop.accepted = mappingController.createRule(page.dragKey, col.receiver, fieldItem.fieldKey)
                        else drop.accepted = false
                    }
                }
            }
        }
    }

    // ---- the sender's fields: drag sources --------------------------------
    component SenderColumn: ColumnLayout {
        id: col
        property string receiver: ""
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
        Text { text: col.title; color: Theme.muted; font.pixelSize: 11 }
        Repeater {
            id: repeater
            model: col.fields
            delegate: Rectangle {
                id: fieldItem
                required property var modelData
                property string fieldKey: modelData.key
                objectName: "sender-" + col.receiver + "-" + fieldKey
                Layout.fillWidth: true
                implicitHeight: 28
                radius: 5
                color: modelData.armed ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.2) : Theme.card
                border.color: senderDrop.containsDrag ? (page.dragKey && page.dragRole === "receiver" && mappingController.canDrop(fieldKey, col.receiver, page.dragKey) === "" ? Theme.accent : Theme.danger) : ((modelData.linked || modelData.armed) ? Theme.accent : Theme.border)
                border.width: modelData.armed ? 2 : 1
                ToolTip.text: modelData.key
                ToolTip.visible: hover.hovered
                HoverHandler { id: hover }
                onXChanged: page.layoutGen++
                onYChanged: page.layoutGen++
                RowLayout {
                    anchors { fill: parent; leftMargin: 8; rightMargin: 8 }
                    Text { text: page.typeIcons[modelData.type] || "?"; color: Theme.muted; Layout.preferredWidth: 18 }
                    Text { text: modelData.label; color: Theme.text; Layout.fillWidth: true; elide: Text.ElideRight }
                    Text { text: modelData.unit || ""; color: Theme.muted; font.pixelSize: 10 }
                }
                TapHandler { onTapped: mappingController.armSource(col.receiver, fieldItem.fieldKey) }
                Drag.active: dragHandler.active
                Drag.hotSpot.x: width / 2
                Drag.hotSpot.y: height / 2
                Drag.keys: ["field"]
                DragHandler {
                    id: dragHandler
                    // Keep the drag even when the page's ScrollView would rather scroll.
                    grabPermissions: PointerHandler.CanTakeOverFromItems | PointerHandler.CanTakeOverFromHandlersOfDifferentType | PointerHandler.ApprovesTakeOverByHandlersOfSameType
                    onActiveChanged: {
                        if (active) { page.dragKey = fieldItem.fieldKey; page.dragReceiver = col.receiver; page.dragRole = "sender"; fieldItem.z = 10 }
                        else { page.finishDrag(fieldItem); fieldItem.z = 0; fieldItem.x = 0; fieldItem.y = fieldItem.y; fieldItem.Layout.fillWidth = false; fieldItem.Layout.fillWidth = true }
                    }
                }
                DropArea {
                    id: senderDrop
                    anchors.fill: parent
                    keys: ["field"]
                    onDropped: function (drop) {
                        if (!page.dragKey || page.dragReceiver !== col.receiver || page.dragRole !== "receiver") { drop.accepted = false; return }
                        drop.accepted = mappingController.createRule(fieldItem.fieldKey, col.receiver, page.dragKey)
                    }
                }
            }
        }
    }
}
