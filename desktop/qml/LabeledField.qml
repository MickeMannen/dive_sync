import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: field
    property string label: ""
    property alias text: input.text
    property alias placeholder: input.placeholderText
    property alias readOnly: input.readOnly
    property bool secret: false
    property int fieldWidth: 220
    // fillWidth: the field shares its layout's width (fieldWidth is then the
    // preferred width, 90 the floor) instead of keeping a fixed one - the
    // Dive edit card's grid, four fields across a pane narrower than four
    // fixed fields.
    property bool fillWidth: false
    signal editingFinished()
    spacing: 2
    Layout.fillWidth: field.fillWidth
    Layout.preferredWidth: field.fillWidth ? field.fieldWidth : -1
    Layout.minimumWidth: field.fillWidth ? 90 : -1
    Text { text: field.label; color: Theme.muted; font.pixelSize: 11; elide: Text.ElideRight; Layout.fillWidth: field.fillWidth }
    TextField {
        id: input
        Layout.preferredWidth: field.fieldWidth
        Layout.fillWidth: field.fillWidth
        echoMode: field.secret ? TextInput.Password : TextInput.Normal
        selectByMouse: true
        // A read-only field looks exactly like an editable one otherwise.
        opacity: readOnly ? 0.65 : 1.0
        onEditingFinished: field.editingFinished()
    }
}
