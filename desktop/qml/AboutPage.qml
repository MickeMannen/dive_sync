import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    spacing: 12

    Card {
        title: aboutController.name
        RowLayout {
            spacing: 12
            Text { text: "Version " + aboutController.version; color: Theme.text; font.pixelSize: 16; font.bold: true }
            Button { text: "Check for updates"; flat: true; onClicked: aboutController.checkForUpdates() }
            Text { text: aboutController.updateStatus; color: Theme.muted; font.pixelSize: 12 }
            Button {
                visible: aboutController.updateUrl !== ""
                text: "Download"
                onClicked: Qt.openUrlExternally(aboutController.updateUrl)
            }
        }
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.text
            font.pixelSize: 12
            text: "Syncs and edits dive logs between Garmin Connect, Divelogs.org and Subsurface. "
                  + "This is the desktop app; the Docker image runs the same sync on a schedule."
        }
        GridLayout {
            columns: 2
            columnSpacing: 16
            rowSpacing: 6
            Text { text: "Author"; color: Theme.muted; font.pixelSize: 12 }
            Text { text: aboutController.author; color: Theme.text; font.pixelSize: 12 }
            Text { text: "Project"; color: Theme.muted; font.pixelSize: 12 }
            Text {
                text: "<a href=\"" + aboutController.projectUrl + "\">" + aboutController.projectUrl + "</a>"
                textFormat: Text.RichText
                font.pixelSize: 12
                onLinkActivated: function (link) { Qt.openUrlExternally(link) }
                HoverHandler { cursorShape: Qt.PointingHandCursor }
            }
            Text { text: "Report a problem"; color: Theme.muted; font.pixelSize: 12 }
            Text {
                text: "<a href=\"" + aboutController.issuesUrl + "\">" + aboutController.issuesUrl + "</a>"
                textFormat: Text.RichText
                font.pixelSize: 12
                onLinkActivated: function (link) { Qt.openUrlExternally(link) }
                HoverHandler { cursorShape: Qt.PointingHandCursor }
            }
            Text { text: "Data folder"; color: Theme.muted; font.pixelSize: 12 }
            RowLayout {
                spacing: 8
                Text { text: aboutController.dataDir; color: Theme.text; font.pixelSize: 12 }
                Button { text: "Open"; flat: true; onClicked: Qt.openUrlExternally("file://" + aboutController.dataDir) }
            }
            Text { text: "System"; color: Theme.muted; font.pixelSize: 12 }
            Text { text: aboutController.platform; color: Theme.text; font.pixelSize: 12 }
        }
    }

    Card {
        title: "License - " + aboutController.licenseName
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.text
            font.pixelSize: 12
            text: "DiveSync is free software under the " + aboutController.licenseName + " license: use, copy, modify and share it, keeping the copyright notice. It comes without any warranty."
        }
        Button {
            id: licenseToggle
            flat: true
            padding: 0
            checkable: true
            text: (checked ? "▾ " : "▸ ") + "Full license text"
            font.pixelSize: 11
        }
        Text {
            visible: licenseToggle.checked
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.family: "Menlo"
            font.pixelSize: 11
            text: aboutController.licenseText
        }
    }

    Card {
        title: "Built with"
        Repeater {
            model: aboutController.components
            delegate: RowLayout {
                required property var modelData
                spacing: 16
                Text { text: modelData.name; color: Theme.text; font.pixelSize: 12; Layout.preferredWidth: 220 }
                Text { text: modelData.version; color: Theme.muted; font.pixelSize: 12 }
            }
        }
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 11
            text: "Garmin Connect, Divelogs.org and Subsurface are the names of their owners' services; DiveSync is an independent project and not affiliated with them."
        }
    }
}
