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
    spacing: 2
    Text { text: field.label; color: Theme.muted; font.pixelSize: 11 }
    TextField {
        id: input
        Layout.preferredWidth: field.fieldWidth
        echoMode: field.secret ? TextInput.Password : TextInput.Normal
        selectByMouse: true
    }
}
