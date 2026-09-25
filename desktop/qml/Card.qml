import QtQuick
import QtQuick.Layouts

Rectangle {
    id: card
    property string title: ""
    default property alias content: body.data
    // Controls that belong beside the title rather than in the body - a
    // card's own Refresh button, say. Assign one item or a list of them.
    property alias headerContent: headerExtras.data
    color: Theme.card
    border.color: Theme.border
    radius: Theme.radius
    Layout.fillWidth: true
    implicitHeight: column.implicitHeight + Theme.pad * 2

    ColumnLayout {
        id: column
        // Filled, not just pinned to the top, so a card given a height by its
        // layout (Layout.fillHeight) passes that height on and a child with
        // fillHeight of its own can grow into it.
        anchors { fill: parent; margins: Theme.pad }
        spacing: 8
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            visible: card.title !== "" || headerExtras.children.length > 0
            Text {
                visible: card.title !== ""
                text: card.title
                color: Theme.muted
                font.pixelSize: 12
                font.capitalization: Font.AllUppercase
                font.letterSpacing: 1
            }
            // fills the row, so a header item can push later ones to the right
            // with an Item { Layout.fillWidth: true } of its own
            RowLayout { id: headerExtras; spacing: 8; Layout.fillWidth: true }
        }
        ColumnLayout { id: body; Layout.fillWidth: true; spacing: 8 }
    }
}
