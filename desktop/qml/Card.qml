import QtQuick
import QtQuick.Layouts

Rectangle {
    id: card
    property string title: ""
    default property alias content: body.data
    color: Theme.card
    border.color: Theme.border
    radius: Theme.radius
    Layout.fillWidth: true
    implicitHeight: column.implicitHeight + Theme.pad * 2

    ColumnLayout {
        id: column
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: Theme.pad }
        spacing: 8
        Text {
            visible: card.title !== ""
            text: card.title
            color: Theme.muted
            font.pixelSize: 12
            font.capitalization: Font.AllUppercase
            font.letterSpacing: 1
        }
        ColumnLayout { id: body; Layout.fillWidth: true; spacing: 8 }
    }
}
