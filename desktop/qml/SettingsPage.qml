import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ColumnLayout {
    spacing: 12

    Card {
        title: "Settings"
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 11
            text: settingsController.hasCredentials
                  ? "Credentials are stored in your OS keychain, not in a plain file."
                  : "Welcome! Enter your Garmin Connect and/or Divelogs.org credentials below to get started - they're stored in your OS keychain, not in a plain file."
        }
        Card {
            title: "Garmin Connect"
            LabeledField { id: gUser; label: "Username"; fieldWidth: 320; text: settingsController.garminUsername }
            LabeledField { id: gPass; label: "Password (leave blank to keep the saved one)"; fieldWidth: 320; secret: true }
            LabeledField { id: gToken; label: "Token directory"; fieldWidth: 420; text: settingsController.garminTokenDir }
            RowLayout {
                Button { text: "Test"; onClicked: settingsController.testGarmin(gUser.text, gPass.text, gToken.text) }
                Text { text: settingsController.garminStatus; color: Theme.muted }
            }
        }
        Card {
            title: "Divelogs.org"
            LabeledField { id: dUser; label: "Username"; fieldWidth: 320; text: settingsController.divelogsUsername }
            LabeledField { id: dPass; label: "Password (leave blank to keep the saved one)"; fieldWidth: 320; secret: true }
            RowLayout {
                Button { text: "Test"; onClicked: settingsController.testDivelogs(dUser.text, dPass.text) }
                Text { text: settingsController.divelogsStatus; color: Theme.muted }
            }
        }
        Card {
            title: "Subsurface Cloud"
            LabeledField { id: sEmail; label: "Email"; fieldWidth: 320; text: settingsController.subsurfaceEmail }
            LabeledField { id: sPass; label: "Password (leave blank to keep the saved one)"; fieldWidth: 320; secret: true }
            RowLayout {
                Button { text: "Test"; onClicked: settingsController.testSubsurface(sEmail.text, sPass.text) }
                Text { text: settingsController.subsurfaceStatus; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            }
        }
        RowLayout {
            Button {
                text: "Save credentials"
                onClicked: {
                    settingsController.save(gUser.text, gPass.text, gToken.text, dUser.text, dPass.text, sEmail.text, sPass.text)
                    gPass.text = ""; dPass.text = ""; sPass.text = ""
                }
            }
            Text { text: settingsController.message; color: Theme.muted }
        }
    }

    Card {
        title: "Sync profile"
        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 11
            text: "The whole sync configuration (direction, filters, mapping board, pairs, jobs; never credentials) as one file, to move it between this app and the Docker status page."
        }
        RowLayout {
            spacing: 8
            Button { text: "Export…"; onClicked: exportDialog.open() }
            Button { text: "Import…"; onClicked: importDialog.open() }
            Button { id: applyButton; text: "Apply import"; visible: false; onClicked: { profileMessage.text = settingsController.applyProfile(); visible = false } }
            Text { id: profileMessage; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
        }
        Text {
            visible: settingsController.profileSummary !== ""
            text: settingsController.profileSummary
            color: Theme.text
            font.family: "Menlo"
            font.pixelSize: 11
        }
    }

    FileDialog {
        id: exportDialog
        title: "Export sync profile"
        fileMode: FileDialog.SaveFile
        nameFilters: ["JSON files (*.json)"]
        currentFile: "file:///" + "dive_sync_profile.json"
        onAccepted: profileMessage.text = settingsController.exportProfile(selectedFile.toString())
    }
    FileDialog {
        id: importDialog
        title: "Import sync profile"
        fileMode: FileDialog.OpenFile
        nameFilters: ["JSON files (*.json)"]
        onAccepted: {
            profileMessage.text = settingsController.checkProfile(selectedFile.toString())
            applyButton.visible = settingsController.profileSummary !== "" && profileMessage.text.indexOf("Review") === 0
        }
    }
}
