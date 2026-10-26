import QtQuick
import QtQuick.Controls.Basic

ToolPage {
    id: root
    objectName: "playerAccountPane"
    required property QtObject bridge
    property real uiScale: 1
    readonly property var account: bridge.playerAccount
    readonly property var state: account.state
    property int selected: 0
    property bool wasLoggedIn: false
    property bool wasPrompted: false
    property string chosenDeviceId: ""
    property int chosenBackupIndex: 0
    readonly property var deviceList: state.devices || []
    readonly property var currentDeviceEntry: {
        const list = deviceList
        if (!list || !list.length) return null
        const target = chosenDeviceId || state.backup_device || state.suggested_device || state.device_id || ""
        for (let i = 0; i < list.length; ++i) {
            if (list[i].device === target) return list[i]
        }
        return list[0]
    }
    readonly property var backupRevisions: currentDeviceEntry && currentDeviceEntry.backups ? currentDeviceEntry.backups : []
    readonly property var currentRevisionEntry: {
        const revs = backupRevisions
        if (!revs || !revs.length) return null
        const idx = Math.max(0, Math.min(revs.length - 1, chosenBackupIndex))
        return revs[idx]
    }
    readonly property var controls: state.logged_in
        ? [syncButton, historyButton, deviceBranchButton, backupRevisionButton, restoreAllButton, restoreSettingsButton, restoreLibraryButton, backupNowButton, deviceNameField, logoutButton, localButton, backButton]
        : [serverField].concat(pairingField.visible ? [pairingField] : []).concat([nameField, passwordField, submitButton, registerButton, localButton, backButton])
    pageScale: uiScale
    onAboutToShow: {
        serverField.text = root.bridge.settingsState.tactic_url
        passwordField.text = ""
        pairingField.text = ""
        deviceNameField.text = state.device_name || ""
        chosenDeviceId = state.backup_device || state.suggested_device || state.device_id || ""
        chosenBackupIndex = 0
        wasPrompted = !!state.backup_prompt
        selected = state.logged_in && state.backup_prompt && currentRevisionEntry !== null ? 4 : 0
        accountScroll.contentItem.contentY = 0
        if (state.logged_in && state.authorized) account.refreshBackups()
        Qt.callLater(revealSelected)
    }
    onAboutToHide: {
        pairingField.focus = false; pairingField.text = ""
        serverField.focus = false
        passwordField.text = ""
        nameField.focus = false
        passwordField.focus = false
        deviceNameField.focus = false
        bridge.keyboard.hide()
    }
    Connections {
        target: root.account
        function onChanged() {
            if (root.state.logged_in && !root.wasLoggedIn) {
                passwordField.text = ""
                deviceNameField.text = root.state.device_name || ""
                root.chosenDeviceId = root.state.suggested_device || root.state.backup_device || root.state.device_id || ""
                root.chosenBackupIndex = 0
                root.selected = root.state.backup_prompt && root.currentRevisionEntry !== null ? 4 : 0
                root.bridge.keyboard.hide()
                if (root.state.authorized) root.account.refreshBackups()
            } else if (!root.chosenDeviceId && (root.state.suggested_device || root.state.backup_device)) {
                root.chosenDeviceId = root.state.suggested_device || root.state.backup_device
            }
            if (root.state.logged_in && root.state.backup_prompt && !root.wasPrompted) {
                root.chosenDeviceId = root.state.suggested_device || root.state.backup_device || root.chosenDeviceId
                if (root.currentRevisionEntry !== null) root.selected = 4
            }
            if (!deviceNameField.activeFocus && root.state.device_name && !deviceNameField.text) {
                deviceNameField.text = root.state.device_name
            }
            root.wasLoggedIn = root.state.logged_in
            root.wasPrompted = !!root.state.backup_prompt
        }
    }
    function submit() { authenticate(false) }
    function register() { authenticate(true) }
    function authenticate(registering) {
        let address = serverField.text.trim().replace(/\/+$/, "")
        if (/^https?:\/\/[^/]+$/.test(address)) address += "/tactic"
        if (address !== bridge.settingsState.tactic_url && !bridge.applySettings(Object.assign({}, bridge.settingsState, {tactic_url: address}))) return
        if (pairingField.visible && pairingField.text.trim()) account.loginWithPairing(nameField.text, passwordField.text, registering, pairingField.text.trim())
        else account.login(nameField.text, passwordField.text, registering)
        passwordField.text = ""; serverField.focus = false; nameField.focus = false; passwordField.focus = false; bridge.keyboard.hide()
    }
    function cycleDevice(step) {
        const list = deviceList
        if (!list || !list.length) return
        const current = currentDeviceEntry ? currentDeviceEntry.device : ""
        let idx = 0
        for (let i = 0; i < list.length; ++i) {
            if (list[i].device === current) { idx = i; break }
        }
        idx = (idx + step + list.length) % list.length
        chosenDeviceId = list[idx].device
        chosenBackupIndex = 0
        account.selectBackupDevice(chosenDeviceId, deviceNameField.text)
    }
    function cycleRevision(step) {
        const revs = backupRevisions
        if (!revs || revs.length <= 1) return
        chosenBackupIndex = (chosenBackupIndex + step + revs.length) % revs.length
    }
    function handleAction(action) {
        if (action === "back" || action === "menu") { close(); return }
        if ((action === "left" || action === "right") && state.logged_in) {
            const activeControl = controls[selected]
            if (activeControl === deviceBranchButton) {
                cycleDevice(action === "right" ? 1 : -1)
                return
            }
            if (activeControl === backupRevisionButton && backupRevisions.length > 1) {
                cycleRevision(action === "right" ? 1 : -1)
                return
            }
        }
        if (["up", "down", "left", "right"].indexOf(action) >= 0) {
            serverField.focus = false; pairingField.focus = false; nameField.focus = false; passwordField.focus = false; deviceNameField.focus = false
            const step = action === "down" || action === "right" ? 1 : -1
            for (let i = 0; i < controls.length; ++i) {
                selected = (selected + step + controls.length) % controls.length
                if (controls[selected].visible && controls[selected].enabled) break
            }
        } else if (action === "activate" && controls[selected].enabled) {
            const field = controls[selected]
            if (field === serverField || field === pairingField || field === nameField || field === passwordField || field === deviceNameField) {
                field.forceActiveFocus()
                bridge.keyboard.show()
            } else {
                field.clicked()
            }
        }
    }
    component AccountButton: FluidButton {
        id: control
        property string glyph: "@play"
        width: parent.width; height: Math.max(54, 58 * root.uiScale)
        highlighted: root.controls[root.selected] === this
        Accessible.name: text
        contentItem: Item {
            Item {
                anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter
                width: 24 * root.uiScale; height: width
                KeyboardGlyph { width: parent.width; height: width; keyName: control.glyph; ink: uiTheme.themeColors.text }
            }
            Text {
                anchors.left: parent.left; anchors.leftMargin: 38 * root.uiScale
                anchors.right: parent.right; anchors.rightMargin: 62 * root.uiScale; anchors.verticalCenter: parent.verticalCenter
                text: control.text; color: control.enabled ? uiTheme.themeColors.text : uiTheme.themeColors.muted; font.pixelSize: 17 * root.uiScale; wrapMode: Text.WordWrap
            }
            Text {
                anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                text: control === backButton ? root.bridge.inputHints.back : control.highlighted ? root.bridge.inputHints.activate : ""
                color: uiTheme.themeColors.muted; font.pixelSize: 14 * root.uiScale; font.weight: Font.DemiBold
            }
        }
    }
    component AccountField: TextField {
        width: parent.width; height: Math.max(54, 58 * root.uiScale); padding: 16 * root.uiScale
        color: uiTheme.themeColors.text; placeholderTextColor: uiTheme.themeColors.muted
        font.pixelSize: 19 * root.uiScale; selectByMouse: true
        onActiveFocusChanged: if (activeFocus) root.selected = root.controls.indexOf(this)
        background: FluidSurface { radius: 16; border.width: 0; shadowEnabled: false; color: root.controls[root.selected] === parent ? uiTheme.themeColors.soft : uiTheme.themeColors.base }
    }
    component Caption: Text {
        width: parent.width; color: uiTheme.themeColors.muted; font.pixelSize: 15 * root.uiScale
        wrapMode: Text.WordWrap; textFormat: Text.PlainText
    }
    onSelectedChanged: Qt.callLater(revealSelected)
    function revealSelected() {
        const field = controls[selected]
        if (!visible || !field || !field.visible || field === backButton) return
        serverInfo.forceLayout(); accountForm.forceLayout()
        const y = field.mapToItem(accountForm, 0, 0).y
        accountScroll.contentItem.contentY = Math.max(0, Math.min(Math.max(0, accountScroll.contentItem.contentHeight - accountScroll.height),
            y < accountScroll.contentItem.contentY ? y : Math.max(accountScroll.contentItem.contentY, y + field.height - accountScroll.height)))
    }
    contentItem: Column {
        spacing: 16 * root.uiScale
        Text {
            id: heading; objectName: "playerAccountHeading"
            width: parent.width; text: root.state.logged_in ? qsTranslate("NGS", "Profile · ") + root.state.username : qsTranslate("NGS", "Sign in to NGS", "Вход в NGS")
            color: uiTheme.themeColors.text; font.pixelSize: 32 * root.uiScale; font.weight: Font.DemiBold; wrapMode: Text.WordWrap
        }
        ScrollView {
            id: accountScroll; objectName: "playerAccountScroll"
            width: parent.width; height: parent.height - heading.height - backButton.height - parent.spacing * 2
            clip: true; contentWidth: availableWidth; ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            Column {
                id: accountForm; width: Math.min(accountScroll.availableWidth, 760 * root.uiScale); spacing: 14 * root.uiScale
                FluidSurface {
                    width: parent.width; height: serverInfo.implicitHeight + 32 * root.uiScale
                    radius: 18; color: uiTheme.themeColors.base; glass: false; border.width: 0; shadowEnabled: false
                    Column {
                        id: serverInfo; x: 16 * root.uiScale; y: x; width: parent.width - x * 2; spacing: 6 * root.uiScale
                        Text { text: qsTranslate("NGS", "NGS server"); color: uiTheme.themeColors.text; font.pixelSize: 17 * root.uiScale; font.weight: Font.DemiBold }
                        Caption { objectName: "playerAccountServer"; visible: root.state.logged_in; text: root.bridge.settingsState.tactic_url; wrapMode: Text.WrapAnywhere }
                        AccountField { id: serverField; objectName: "playerServerAddress"; visible: !root.state.logged_in; enabled: !root.state.busy; maximumLength: 500; placeholderText: qsTranslate("NGS", "https://your-server"); text: root.bridge.settingsState.tactic_url }
                        Text { visible: pairingField.visible; text: qsTranslate("NGS", "Local server pairing code"); color: uiTheme.themeColors.text; font.pixelSize: 16 * root.uiScale }
                        AccountField { id: pairingField; objectName: "playerPairingCode"; visible: !root.state.logged_in && serverField.text.trim().startsWith("http://"); enabled: !root.state.busy; maximumLength: 128; placeholderText: qsTranslate("NGS", "Optional after pairing: 64-character code from NAS logs") }
                        Caption { visible: pairingField.visible; text: qsTranslate("NGS", "Without HTTPS, copy the pairing code once from the NAS administration logs. Do not obtain it from an untrusted web page. Internet connections require HTTPS.") }
                    }
                }
                Caption { visible: !root.state.logged_in; text: qsTranslate("NGS", "Enter your server address. Sign in or create an account to access games, statistics and server backups. New accounts require a password of at least 12 characters.") }
                Text { visible: nameField.visible; text: qsTranslate("NGS", "Username", "Логин"); color: uiTheme.themeColors.text; font.pixelSize: 17 * root.uiScale; font.weight: Font.DemiBold }
                AccountField { id: nameField; objectName: "playerUsername"; visible: !root.state.logged_in; enabled: !root.state.busy; maximumLength: 32; placeholderText: qsTranslate("NGS", "Username", "Имя пользователя") }
                Text { visible: passwordField.visible; text: qsTranslate("NGS", "Password"); color: uiTheme.themeColors.text; font.pixelSize: 17 * root.uiScale; font.weight: Font.DemiBold }
                AccountField { id: passwordField; objectName: "playerPassword"; visible: nameField.visible; enabled: nameField.enabled; maximumLength: 128; echoMode: TextInput.Password; placeholderText: qsTranslate("NGS", "Account password"); onAccepted: root.submit() }
                AccountButton { id: submitButton; objectName: "playerSubmit"; visible: nameField.visible; enabled: !root.state.busy; text: qsTranslate("NGS", "Sign in"); onClicked: root.submit() }
                AccountButton { id: registerButton; objectName: "playerRegister"; visible: nameField.visible; enabled: !root.state.busy; glyph: "@add"; text: qsTranslate("NGS", "Create account"); onClicked: root.register() }

                AccountButton { id: syncButton; visible: root.state.logged_in; enabled: !root.state.busy; glyph: "@sync"; text: qsTranslate("NGS", "Sync statistics and backup list"); onClicked: { root.account.sync(); root.account.refreshBackups() } }
                AccountButton { id: historyButton; objectName: "playerImportHistory"; visible: root.state.logged_in; enabled: !root.state.busy && root.state.legacy > 0; glyph: "@library"; text: qsTranslate("NGS", "Upload previous history · ") + root.state.legacy + qsTranslate("NGS", " sessions"); onClicked: root.account.importHistory() }

                FluidSurface {
                    visible: root.state.logged_in && root.state.backup_prompt
                    width: parent.width; height: promptCol.implicitHeight + 28 * root.uiScale
                    radius: 18; color: uiTheme.themeColors.soft; glass: false; border.width: 1; border.color: uiTheme.themeColors.accent; shadowEnabled: false
                    Column {
                        id: promptCol; x: 16 * root.uiScale; y: 14 * root.uiScale; width: parent.width - x * 2; spacing: 6 * root.uiScale
                        Text { text: qsTranslate("NGS", "Server backups and settings found"); color: uiTheme.themeColors.accent; font.pixelSize: 17 * root.uiScale; font.weight: Font.DemiBold; wrapMode: Text.WordWrap }
                        Caption { text: qsTranslate("NGS", "The recommended device branch is selected. Restore its library and settings below, or choose another device branch.") }
                    }
                }

                Text {
                    visible: root.state.logged_in
                    text: qsTranslate("NGS", "Server backup of library and settings")
                    color: uiTheme.themeColors.text; font.pixelSize: 20 * root.uiScale; font.weight: Font.DemiBold; wrapMode: Text.WordWrap
                }
                Caption {
                    visible: root.state.logged_in
                    text: qsTranslate("NGS", "Current device ID: ") + (root.state.device_id || "") + qsTranslate("NGS", "\nEach device has its own settings and library backup history. Select a branch to choose another device.")
                }
                AccountButton {
                    id: deviceBranchButton; objectName: "playerBackupDeviceButton"
                    visible: root.state.logged_in; enabled: !root.state.backup_busy && root.deviceList.length > 0
                    glyph: "@settings"
                    text: qsTranslate("NGS", "Device branch: ") + (root.currentDeviceEntry ? root.currentDeviceEntry.label : (root.state.device_id || "")) + (root.deviceList.length > 1 ? "  ⇄" : "")
                    onClicked: root.cycleDevice(1)
                }
                AccountButton {
                    id: backupRevisionButton; objectName: "playerBackupRevisionButton"
                    visible: root.state.logged_in; enabled: !root.state.backup_busy && root.backupRevisions.length > 0
                    glyph: "@library"
                    text: root.currentRevisionEntry
                        ? (qsTranslate("NGS", "Backup revision (") + (root.chosenBackupIndex + 1) + "/" + root.backupRevisions.length + "): " + root.currentRevisionEntry.label + (root.backupRevisions.length > 1 ? "  ⇄" : ""))
                        : qsTranslate("NGS", "Backup history: this branch has no backups yet")
                    onClicked: root.cycleRevision(1)
                }
                AccountButton {
                    id: restoreAllButton; objectName: "playerRestoreAllButton"
                    visible: root.state.logged_in; enabled: !root.state.backup_busy && root.currentRevisionEntry !== null
                    glyph: "@sync"
                    text: qsTranslate("NGS", "Restore library and settings from this branch")
                    onClicked: if (root.currentDeviceEntry && root.currentRevisionEntry) root.account.restoreBackup(root.currentDeviceEntry.device, root.currentRevisionEntry.id, true, true)
                }
                AccountButton {
                    id: restoreSettingsButton; objectName: "playerRestoreSettingsButton"
                    visible: root.state.logged_in; enabled: !root.state.backup_busy && root.currentRevisionEntry !== null
                    glyph: "@settings"
                    text: qsTranslate("NGS", "Restore settings only")
                    onClicked: if (root.currentDeviceEntry && root.currentRevisionEntry) root.account.restoreBackup(root.currentDeviceEntry.device, root.currentRevisionEntry.id, true, false)
                }
                AccountButton {
                    id: restoreLibraryButton; objectName: "playerRestoreLibraryButton"
                    visible: root.state.logged_in; enabled: !root.state.backup_busy && root.currentRevisionEntry !== null
                    glyph: "@library"
                    text: qsTranslate("NGS", "Restore local library only")
                    onClicked: if (root.currentDeviceEntry && root.currentRevisionEntry) root.account.restoreBackup(root.currentDeviceEntry.device, root.currentRevisionEntry.id, false, true)
                }
                AccountButton {
                    id: backupNowButton; objectName: "playerBackupNowButton"
                    visible: root.state.logged_in; enabled: !root.state.backup_busy
                    glyph: "@play"
                    text: qsTranslate("NGS", "Back up to this branch now")
                    onClicked: root.account.backupNow(root.currentDeviceEntry ? root.currentDeviceEntry.device : root.state.device_id, deviceNameField.text)
                }
                Text { visible: root.state.logged_in; text: qsTranslate("NGS", "Device name in backups"); color: uiTheme.themeColors.text; font.pixelSize: 17 * root.uiScale; font.weight: Font.DemiBold }
                AccountField {
                    id: deviceNameField; objectName: "playerDeviceName"
                    visible: root.state.logged_in; enabled: !root.state.backup_busy
                    maximumLength: 64; placeholderText: qsTranslate("NGS", "For example: AOKZOE-A1")
                    onAccepted: {
                        root.account.selectBackupDevice(root.currentDeviceEntry ? root.currentDeviceEntry.device : root.state.device_id, deviceNameField.text)
                        deviceNameField.focus = false
                        root.bridge.keyboard.hide()
                    }
                }
                Caption {
                    objectName: "playerBackupStatus"
                    visible: root.state.logged_in
                    text: root.state.backup_status || ""
                    color: root.state.backup_busy ? uiTheme.themeColors.accent : uiTheme.themeColors.muted
                }

                AccountButton { id: logoutButton; visible: root.state.logged_in; enabled: !root.state.busy && !root.state.backup_busy; glyph: "@power"; text: qsTranslate("NGS", "Sign out"); onClicked: root.account.logout() }
                Caption { visible: root.bridge.localMode; text: qsTranslate("NGS", "Local library is active. Connect and sign in only if you want NAS downloads, shared statistics and server backups.") }
                AccountButton { id: localButton; objectName: "playerUseLocalLibrary"; visible: !root.bridge.localMode; enabled: !root.state.busy && !root.state.backup_busy; glyph: "@library"; text: qsTranslate("NGS", "Use without a server"); onClicked: if (root.bridge.useLocalLibrary()) root.close() }
                ActivityIndicator { objectName: "playerSyncActivity"; running: root.visible && (root.state.busy || root.state.backup_busy); visible: running; width: 32 * root.uiScale; height: width }
                Caption { objectName: "playerSyncStatus"; text: root.state.status + (root.state.logged_in ? qsTranslate("NGS", "\nWaiting to upload: ") + root.state.pending + qsTranslate("NGS", " sessions") : ""); color: root.state.busy ? uiTheme.themeColors.accent : uiTheme.themeColors.muted }
                Caption { visible: !root.state.logged_in; text: qsTranslate("NGS", "Your password is not saved. The sign-in token is stored in the data folder; do not share that folder.") }
            }
        }
        AccountButton { id: backButton; width: Math.min(parent.width, 760 * root.uiScale); text: qsTranslate("NGS", "Back"); glyph: "@back"; onClicked: root.close() }
    }
}
