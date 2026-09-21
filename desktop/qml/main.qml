import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: window
    visible: true
    title: "DiveSync"
    width: Math.max(1100, Math.min(Screen.width * 0.85, 1600))
    height: Math.max(750, Math.min(Screen.height * 0.85, 1000))
    color: Theme.bg

    readonly property var sections: ["Sync", "Garmin Dives", "Divelogs Dives", "Mapping", "Conflicts", "Settings"]
    property int currentSection: Math.max(0, sections.indexOf(initialSection))

    onClosing: function (close) {
        if (mappingController.dirty) {
            close.accepted = false
            leaveDialog.open()
        }
    }
    Dialog {
        id: leaveDialog
        title: "Unsaved mapping changes"
        modal: true
        standardButtons: Dialog.Discard | Dialog.Cancel
        anchors.centerIn: Overlay.overlay
        Text { text: "The mapping board has unsaved changes. Discard them and quit?"; color: Theme.text }
        onDiscarded: { mappingController.cancel(); Qt.quit() }
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.preferredWidth: 200
            Layout.fillHeight: true
            color: Theme.sidebar
            ColumnLayout {
                anchors { left: parent.left; right: parent.right; top: parent.top }
                spacing: 2
                Text { text: "DiveSync"; color: Theme.sidebarText; font.bold: true; font.pixelSize: 18; Layout.margins: 16 }
                Repeater {
                    model: window.sections
                    delegate: Rectangle {
                        required property int index
                        required property string modelData
                        Layout.fillWidth: true
                        implicitHeight: 36
                        color: index === window.currentSection ? Theme.navActive : "transparent"
                        Text {
                            anchors { verticalCenter: parent.verticalCenter; left: parent.left; leftMargin: 16 }
                            text: modelData
                            color: Theme.sidebarText
                            font.pixelSize: 13
                        }
                        MouseArea { anchors.fill: parent; onClicked: window.currentSection = index }
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0
            Text {
                text: window.sections[window.currentSection]
                color: Theme.navActive
                font.bold: true
                font.pixelSize: 15
                Layout.margins: 16
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                contentWidth: availableWidth
                clip: true
                StackLayout {
                    id: stack
                    width: parent.width
                    currentIndex: window.currentSection
                    ColumnLayout { Layout.margins: 16; SyncPage { Layout.fillWidth: true } }
                    ColumnLayout { Layout.margins: 16; DivesPage { Layout.fillWidth: true; controller: garminDives } }
                    ColumnLayout { Layout.margins: 16; DivesPage { Layout.fillWidth: true; controller: divelogsDives } }
                    ColumnLayout { Layout.margins: 16; MappingPage { Layout.fillWidth: true } }
                    ColumnLayout { Layout.margins: 16; ConflictsPage { Layout.fillWidth: true } }
                    ColumnLayout { Layout.margins: 16; SettingsPage { Layout.fillWidth: true } }
                }
            }
        }
    }
}
