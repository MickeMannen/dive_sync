import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: window
    visible: true
    title: "DiveSync"
    // 1100x750 is what the pages are laid out for (below that the dives table
    // starts scrolling horizontally), so it is both the preferred floor and
    // the enforced minimum. availableScreen is the launch screen's usable area
    // (menu bar and dock excluded, see desktop/app.py), and the floor is
    // clamped to it last: on a small display an unclamped 750 would put the
    // bottom of the window off-screen at launch.
    readonly property int screenWidth: availableScreen.width || Screen.desktopAvailableWidth
    readonly property int screenHeight: availableScreen.height || Screen.desktopAvailableHeight
    width: Math.min(Math.max(1100, Math.min(screenWidth * 0.85, 1600)), screenWidth)
    height: Math.min(Math.max(750, Math.min(screenHeight * 0.85, 1000)), screenHeight)
    minimumWidth: Math.min(1100, screenWidth)
    minimumHeight: Math.min(750, screenHeight)
    color: Theme.bg

    readonly property var sections: ["Sync", "Garmin Dives", "Divelogs Dives", "Subsurface Dives",
                                     "Mapping", "Conflicts", "Settings", "About"]
    property int currentSection: Math.max(0, sections.indexOf(initialSection))

    readonly property int unsavedDives: garminDives.pendingCount + divelogsDives.pendingCount + subsurfaceDives.pendingCount
    // Set once the diver chose to discard: quitting closes the window again,
    // and that second close must not ask a second time.
    property bool discardConfirmed: false
    function quitDiscarding() {
        window.discardConfirmed = true
        Qt.quit()
    }
    onClosing: function (close) {
        if (discardConfirmed) return
        if (mappingController.dirty) {
            close.accepted = false
            leaveDialog.open()
        } else if (unsavedDives > 0) {
            close.accepted = false
            leaveDivesDialog.open()
        }
    }
    Dialog {
        id: leaveDivesDialog
        objectName: "leaveDivesDialog"
        title: "Unsaved dive changes"
        modal: true
        standardButtons: Dialog.Discard | Dialog.Cancel
        anchors.centerIn: Overlay.overlay
        Text { text: "Some dives have unsaved changes. Discard them and quit?"; color: Theme.text }
        onDiscarded: window.quitDiscarding()
    }
    Dialog {
        id: leaveDialog
        title: "Unsaved mapping changes"
        modal: true
        standardButtons: Dialog.Discard | Dialog.Cancel
        anchors.centerIn: Overlay.overlay
        Text { text: "The mapping board has unsaved changes. Discard them and quit?"; color: Theme.text }
        // the board is clean after cancel(); closing again still asks about unsaved dives
        onDiscarded: { mappingController.cancel(); window.close() }
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
            // Each page brings its own scrolling rather than one ScrollView
            // wrapping the StackLayout. A StackLayout's implicit size is the
            // largest of ALL its children, so a single outer ScrollView sized
            // itself to the tallest page (Settings) and then showed a
            // scrollbar on every other page too - including the dives pages,
            // which scroll their own dive-data panel and want no second one.
            StackLayout {
                id: stack
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: window.currentSection

                ScrollView {
                    id: syncScroll
                    padding: 16
                    contentWidth: availableWidth
                    clip: true
                    ColumnLayout { width: syncScroll.availableWidth; SyncPage { Layout.fillWidth: true } }
                }
                // The dives pages fill the viewport instead of scrolling.
                Item {
                    ColumnLayout {
                        anchors { fill: parent; margins: 16 }
                        DivesPage { Layout.fillWidth: true; Layout.fillHeight: true; controller: garminDives }
                    }
                }
                Item {
                    ColumnLayout {
                        anchors { fill: parent; margins: 16 }
                        DivesPage { Layout.fillWidth: true; Layout.fillHeight: true; controller: divelogsDives }
                    }
                }
                Item {
                    ColumnLayout {
                        anchors { fill: parent; margins: 16 }
                        DivesPage { Layout.fillWidth: true; Layout.fillHeight: true; controller: subsurfaceDives }
                    }
                }
                ScrollView {
                    id: mappingScroll
                    padding: 16
                    contentWidth: availableWidth
                    clip: true
                    ColumnLayout { width: mappingScroll.availableWidth; MappingPage { Layout.fillWidth: true } }
                }
                ScrollView {
                    id: conflictsScroll
                    padding: 16
                    contentWidth: availableWidth
                    clip: true
                    ColumnLayout { width: conflictsScroll.availableWidth; ConflictsPage { Layout.fillWidth: true } }
                }
                ScrollView {
                    id: settingsScroll
                    padding: 16
                    contentWidth: availableWidth
                    clip: true
                    ColumnLayout { width: settingsScroll.availableWidth; SettingsPage { Layout.fillWidth: true } }
                }
                ScrollView {
                    id: aboutScroll
                    padding: 16
                    contentWidth: availableWidth
                    clip: true
                    ColumnLayout { width: aboutScroll.availableWidth; AboutPage { Layout.fillWidth: true } }
                }
            }
        }
    }
}
