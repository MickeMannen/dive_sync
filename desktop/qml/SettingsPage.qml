import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ColumnLayout {
    spacing: 12

    Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.muted
            font.pixelSize: 12
            text: settingsController.hasCredentials
                  ? "Credentials are stored in your OS keychain, not in a plain file."
                  : "Welcome! Enter your Garmin Connect and/or Divelogs.org credentials below to get started - they're stored in your OS keychain, not in a plain file."
        }
        Card {
            id: garminCard
            title: "Garmin Connect"
            ListModel { id: garminAccountsModel }
            function reload() {
                garminAccountsModel.clear()
                var accounts = settingsController.garminAccounts
                for (var i = 0; i < accounts.length; i++)
                    garminAccountsModel.append({username: accounts[i].username, password: "",
                                                hasPassword: accounts[i].has_password})
                if (accounts.length === 0) garminAccountsModel.append({username: "", password: "", hasPassword: false})
            }
            Component.onCompleted: reload()
            Connections { target: settingsController; function onCredentialsChanged() { garminCard.reload() } }
            Repeater {
                model: garminAccountsModel
                delegate: RowLayout {
                    required property int index
                    required property string username
                    required property string password
                    required property bool hasPassword
                    // Commit on every keystroke, not on blur: "Save Garmin
                    // accounts" below reads the model, and a password still
                    // sitting uncommitted in a focused field would be saved
                    // blank (the Test buttons dodge this by reading .text).
                    LabeledField { id: uName; fieldWidth: 220; label: "Username"; text: username; onTextChanged: garminAccountsModel.setProperty(index, "username", text) }
                    LabeledField { id: uPass; fieldWidth: 220; label: "Password"; placeholder: hasPassword ? "saved - leave blank to keep it" : ""; secret: true; text: password; onTextChanged: garminAccountsModel.setProperty(index, "password", text) }
                    // Read the fields' live text directly rather than the
                    // committed model value (only updated on blur/Enter -
                    // clicking Test right after typing would otherwise
                    // race ahead of that commit and test blank/stale values).
                    Button { text: "Test"; onClicked: settingsController.testGarmin(uName.text, uPass.text) }
                    Button { text: "Remove"; flat: true; onClicked: garminAccountsModel.remove(index) }
                    // Marks which row is the half-saved one when several are
                    // listed; inline, so showing it never reflows the page.
                    // Tied to what is stored, not to the live field, so typing
                    // a fix doesn't make it flicker.
                    Text {
                        visible: username !== "" && !hasPassword
                        text: "⚠ no password stored"
                        color: Theme.danger
                        font.pixelSize: 11
                    }
                }
            }
            // Always on screen (never `visible: text !== ""`): a status line
            // that appears only once you press Test would shift everything
            // below it down at exactly the moment you are reading it.
            Text { text: settingsController.garminStatus; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            RowLayout {
                Button {
                    text: "Save"
                    onClicked: {
                        // ListModel.get(i) returns a model-data object that PySide6
                        // marshals as a QObject, not a dict (rows[i].get(...) then
                        // fails in Python) - copy into a plain JS object instead.
                        var rows = []
                        for (var i = 0; i < garminAccountsModel.count; i++) {
                            var row = garminAccountsModel.get(i)
                            rows.push({username: row.username, password: row.password})
                        }
                        settingsController.saveGarminAccounts(rows)
                    }
                }
                Button { text: "Add another account"; flat: true; onClicked: garminAccountsModel.append({username: "", password: "", hasPassword: false}) }
            }
        }
        Card {
            id: divelogsCard
            title: "Divelogs.org"
            ListModel { id: divelogsAccountsModel }
            function reload() {
                divelogsAccountsModel.clear()
                var accounts = settingsController.divelogsAccounts
                for (var i = 0; i < accounts.length; i++)
                    divelogsAccountsModel.append({username: accounts[i].username, password: "",
                                                  hasPassword: accounts[i].has_password})
                if (accounts.length === 0) divelogsAccountsModel.append({username: "", password: "", hasPassword: false})
            }
            Component.onCompleted: reload()
            Connections { target: settingsController; function onCredentialsChanged() { divelogsCard.reload() } }
            Repeater {
                model: divelogsAccountsModel
                delegate: RowLayout {
                    required property int index
                    required property string username
                    required property string password
                    required property bool hasPassword
                    // See the Garmin rows above: commit on keystroke so Save
                    // never reads a stale blank password out of the model.
                    LabeledField { id: dName; fieldWidth: 220; label: "Username"; text: username; onTextChanged: divelogsAccountsModel.setProperty(index, "username", text) }
                    LabeledField { id: dPass; fieldWidth: 220; label: "Password"; placeholder: hasPassword ? "saved - leave blank to keep it" : ""; secret: true; text: password; onTextChanged: divelogsAccountsModel.setProperty(index, "password", text) }
                    // See the Garmin Test button above: read live text, not
                    // the committed (blur/Enter-only) model value.
                    Button { text: "Test"; onClicked: settingsController.testDivelogs(dName.text, dPass.text) }
                    Button { text: "Remove"; flat: true; onClicked: divelogsAccountsModel.remove(index) }
                    Text {
                        visible: username !== "" && !hasPassword
                        text: "⚠ no password stored"
                        color: Theme.danger
                        font.pixelSize: 11
                    }
                }
            }
            // See the Garmin card: this line is permanent, only its text changes.
            Text { text: settingsController.divelogsStatus; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            RowLayout {
                Button {
                    text: "Save"
                    onClicked: {
                        // See the Garmin save button above: get(i) is not a dict.
                        var rows = []
                        for (var i = 0; i < divelogsAccountsModel.count; i++) {
                            var row = divelogsAccountsModel.get(i)
                            rows.push({username: row.username, password: row.password})
                        }
                        settingsController.saveDivelogsAccounts(rows)
                    }
                }
                Button { text: "Add another account"; flat: true; onClicked: divelogsAccountsModel.append({username: "", password: "", hasPassword: false}) }
            }
        }
        Card {
            title: "Subsurface Cloud"
            RowLayout {
                LabeledField { id: sEmail; label: "Email"; fieldWidth: 220; text: settingsController.subsurfaceEmail }
                LabeledField { id: sPass; label: "Password"; fieldWidth: 220; secret: true
                               placeholder: settingsController.subsurfaceEmail !== "" ? "saved - leave blank to keep it" : "" }
                Button { text: "Test"; onClicked: settingsController.testSubsurface(sEmail.text, sPass.text) }
            }
            Text { text: settingsController.subsurfaceStatus; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            RowLayout {
                Button {
                    text: "Save"
                    onClicked: { settingsController.saveSubsurface(sEmail.text, sPass.text); sPass.text = "" }
                }
            }
        }
        Text { visible: text !== ""; text: settingsController.message; color: Theme.muted }

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
