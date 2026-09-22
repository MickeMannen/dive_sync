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
            id: garminCard
            title: "Garmin Connect"
            ListModel { id: garminAccountsModel }
            function reload() {
                garminAccountsModel.clear()
                var accounts = settingsController.garminAccounts
                for (var i = 0; i < accounts.length; i++)
                    garminAccountsModel.append({username: accounts[i].username, password: "", token_dir: accounts[i].token_dir})
                if (accounts.length === 0) garminAccountsModel.append({username: "", password: "", token_dir: ""})
            }
            Component.onCompleted: reload()
            Connections { target: settingsController; function onCredentialsChanged() { garminCard.reload() } }
            Repeater {
                model: garminAccountsModel
                delegate: ColumnLayout {
                    required property int index
                    required property string username
                    required property string password
                    required property string token_dir
                    RowLayout {
                        LabeledField { fieldWidth: 220; label: "Username"; text: username; onEditingFinished: garminAccountsModel.setProperty(index, "username", text) }
                        LabeledField { fieldWidth: 220; label: "Password (leave blank to keep the saved one)"; secret: true; text: password; onEditingFinished: garminAccountsModel.setProperty(index, "password", text) }
                        LabeledField { fieldWidth: 320; label: "Token directory"; text: token_dir; onEditingFinished: garminAccountsModel.setProperty(index, "token_dir", text) }
                        Button { text: "Test"; onClicked: settingsController.testGarmin(username, password, token_dir) }
                        Button { text: "Remove"; flat: true; onClicked: garminAccountsModel.remove(index) }
                    }
                }
            }
            RowLayout {
                Button { text: "Add Garmin account"; flat: true; onClicked: garminAccountsModel.append({username: "", password: "", token_dir: ""}) }
                Button {
                    text: "Save Garmin accounts"
                    onClicked: {
                        var rows = []
                        for (var i = 0; i < garminAccountsModel.count; i++) rows.push(garminAccountsModel.get(i))
                        settingsController.saveGarminAccounts(rows)
                    }
                }
                Text { text: settingsController.garminStatus; color: Theme.muted }
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
                    divelogsAccountsModel.append({username: accounts[i].username, password: ""})
                if (accounts.length === 0) divelogsAccountsModel.append({username: "", password: ""})
            }
            Component.onCompleted: reload()
            Connections { target: settingsController; function onCredentialsChanged() { divelogsCard.reload() } }
            Repeater {
                model: divelogsAccountsModel
                delegate: RowLayout {
                    required property int index
                    required property string username
                    required property string password
                    LabeledField { fieldWidth: 220; label: "Username"; text: username; onEditingFinished: divelogsAccountsModel.setProperty(index, "username", text) }
                    LabeledField { fieldWidth: 220; label: "Password (leave blank to keep the saved one)"; secret: true; text: password; onEditingFinished: divelogsAccountsModel.setProperty(index, "password", text) }
                    Button { text: "Test"; onClicked: settingsController.testDivelogs(username, password) }
                    Button { text: "Remove"; flat: true; onClicked: divelogsAccountsModel.remove(index) }
                }
            }
            RowLayout {
                Button { text: "Add Divelogs account"; flat: true; onClicked: divelogsAccountsModel.append({username: "", password: ""}) }
                Button {
                    text: "Save Divelogs accounts"
                    onClicked: {
                        var rows = []
                        for (var i = 0; i < divelogsAccountsModel.count; i++) rows.push(divelogsAccountsModel.get(i))
                        settingsController.saveDivelogsAccounts(rows)
                    }
                }
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
        Card {
            title: "Submersion sync store"
            RowLayout {
                spacing: 6
                Text { text: "Store type"; color: Theme.muted; font.pixelSize: 11 }
                ComboBox {
                    id: subStoreType
                    model: ["s3", "folder"]
                    currentIndex: settingsController.submersionStoreType === "folder" ? 1 : 0
                }
            }
            ColumnLayout {
                visible: subStoreType.currentText === "s3"
                spacing: 8
                LabeledField { id: subEndpoint; label: "Endpoint URL"; fieldWidth: 420; text: settingsController.submersionEndpointUrl }
                LabeledField { id: subRegion; label: "Region"; fieldWidth: 220; text: settingsController.submersionRegion }
                LabeledField { id: subBucket; label: "Bucket"; fieldWidth: 320; text: settingsController.submersionBucket }
                LabeledField { id: subPrefix; label: "Prefix"; fieldWidth: 320; text: settingsController.submersionPrefix }
                LabeledField { id: subAccessKey; label: "Access key ID"; fieldWidth: 320; text: settingsController.submersionAccessKeyId }
                LabeledField { id: subSecretKey; label: "Secret access key (leave blank to keep the saved one)"; fieldWidth: 320; secret: true }
                CheckBox { id: subPathStyle; text: "Path-style addressing"; checked: settingsController.submersionPathStyle }
            }
            ColumnLayout {
                visible: subStoreType.currentText === "folder"
                spacing: 8
                LabeledField { id: subFolderPath; label: "Folder path"; fieldWidth: 420; text: settingsController.submersionFolderPath }
            }
            LabeledField { id: subPassphrase; label: "End-to-end encryption passphrase (leave blank if this library isn't E2E encrypted, or to keep the saved one)"; fieldWidth: 420; secret: true }
            RowLayout {
                Button {
                    text: "Test"
                    onClicked: settingsController.testSubmersion(subStoreType.currentText, subEndpoint.text, subRegion.text,
                        subBucket.text, subPrefix.text, subAccessKey.text, subSecretKey.text, subPathStyle.checked, subFolderPath.text,
                        subPassphrase.text)
                }
                Text { text: settingsController.submersionStatus; color: Theme.muted; Layout.fillWidth: true; wrapMode: Text.WordWrap }
            }
        }
        RowLayout {
            Button {
                text: "Save credentials"
                onClicked: {
                    settingsController.save(sEmail.text, sPass.text,
                        subStoreType.currentText, subEndpoint.text, subRegion.text, subBucket.text, subPrefix.text,
                        subAccessKey.text, subSecretKey.text, subPathStyle.checked, subFolderPath.text, subPassphrase.text)
                    sPass.text = ""; subSecretKey.text = ""; subPassphrase.text = ""
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
