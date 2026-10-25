import QtQuick
import QtQuick.Controls.Basic

MotionPopup {
    id: root
    objectName: "settingsPane"
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    readonly property bool updateAvailable: bridge.updatesState.available
    property real uiScale: 1
    property int page: -1
    property int displayedPage: -1
    property real navigationProgress: 0
    property real detailReveal: 1
    property int menuIndex: 0
    property int focusSide: 0
    property int selectedField: -1
    property int choiceField: -1
    property int choiceCursor: 0
    property int editingField: -1
    property string childPage: ""
    property int inputInstallAction: 0
    readonly property color ink: uiTheme.themeColors.text
    readonly property color muted: uiTheme.themeColors.muted
    readonly property var sections: [qsTranslate("NGS", "Users and account"), qsTranslate("NGS", "Display and appearance"), qsTranslate("NGS", "Storage and installation"), qsTranslate("NGS", "Controllers and input"), qsTranslate("NGS", "System"), qsTranslate("NGS", "Catalog and publishing")]
    readonly property var sectionFields: [[2, 15, 16, 17, 13, 23], [42, 11, 12, 21, 22, 4, 24, 25, 26, 27], [0, 1, 28, 29, 39], [10, 19, 20, 40, 41, 14, 30, 31, 32], [5, 8, 33, 34, 35, 36, 38], [3, 9, 18, 37]]
    readonly property var accentKeys: ["green", "blue", "violet", "amber", "rose"]
    readonly property var accentLabels: [qsTranslate("NGS", "Green"), qsTranslate("NGS", "Blue"), qsTranslate("NGS", "Violet"), qsTranslate("NGS", "Amber"), qsTranslate("NGS", "Rose")]
    readonly property var graphicsKeys: ["d3d11", "d3d12", "vulkan", "opengl", "software"]
    readonly property var graphicsLabels: [qsTranslate("NGS", "Direct3D 11 (default)"), "Direct3D 12", "Vulkan", "OpenGL", "Software (CPU)"]
    readonly property var animationKeys: ["normal", "fast", "off"]
    readonly property var animationLabels: [qsTranslate("NGS", "Normal"), qsTranslate("NGS", "Fast"), qsTranslate("NGS", "Off")]
    readonly property var defaultSectionKeys: ["last", "library", "catalog", "installers"]
    readonly property var defaultSectionLabels: [qsTranslate("NGS", "Last opened"), qsTranslate("NGS", "My games"), qsTranslate("NGS", "Catalog"), qsTranslate("NGS", "Installers")]
    readonly property var windowModeKeys: ["fullscreen", "windowed"]
    readonly property var windowModeLabels: [qsTranslate("NGS", "Fullscreen"), qsTranslate("NGS", "Windowed")]
    readonly property var reserveValues: [0.5, 2.0, 5.0, 10.0]
    readonly property var reserveLabels: [qsTranslate("NGS", "512 MiB"), qsTranslate("NGS", "2 GiB"), qsTranslate("NGS", "5 GiB"), qsTranslate("NGS", "10 GiB")]
    readonly property var deadzoneValues: [10000, 16000, 22000]
    readonly property var deadzoneLabels: [qsTranslate("NGS", "Small (15%)"), qsTranslate("NGS", "Medium (25%)"), qsTranslate("NGS", "Large (35%)")]
    readonly property var repeatKeys: ["normal", "fast", "slow"]
    readonly property var repeatLabels: [qsTranslate("NGS", "Normal"), qsTranslate("NGS", "Fast"), qsTranslate("NGS", "Slow")]
    readonly property real animScale: bridge.animationScale !== undefined ? bridge.animationScale : 1.0
    readonly property var fields: [destination, addDestination, server, metadata, scaleControl, autostart, saveButton, backButton, updatesButton, catalogizerButton, bindingsButton, themeControl, accentControl, accountButton, inputHostButton, username, password, loginButton, installerCatalogizerButton, keyboardToggle, overlayToggle, graphicsControl, disableOverlaysToggle, autoBackupToggle, animationSpeedControl, backdropBlurToggle, defaultSectionControl, windowModeControl, autoStartDownloadsToggle, reserveSpaceControl, vibrationToggle, deadzoneControl, repeatSpeedControl, autoCheckUpdatesToggle, minimizeToTrayToggle, freezeOnSleepToggle, autoTrimMemoryToggle, knownGamesUrlField, productionToggle, organizeButton, edgeSwipeToggle, edgeAutoHideToggle, languageControl]
    onPageChanged: {
        if (page >= 0) {
            displayedPage = page
            if (visible && navigationProgress > .99) detailEntrance.restart()
        }
        navigationMotion.to = page >= 0 ? 1 : 0
        if (visible && animScale > 0.01) navigationMotion.restart()
        else {
            navigationMotion.stop(); navigationProgress = navigationMotion.to
            if (page < 0) displayedPage = -1
        }
    }
    NumberAnimation {
        id: navigationMotion; objectName: "settingsNavigationMotion"
        target: root; property: "navigationProgress"; duration: Math.round(360 * root.animScale); easing.type: Easing.OutCubic
        onFinished: if (root.page < 0) root.displayedPage = -1
    }
    NumberAnimation {
        id: detailEntrance; target: root; property: "detailReveal"; from: 0; to: 1
        duration: Math.round(280 * root.animScale); easing.type: Easing.OutCubic
    }
    signal updatesRequested()
    signal catalogizerRequested(bool installers)
    signal bindingsRequested()
    signal accountRequested()
    x: 0; y: 0; width: parent.width; height: parent.height
    modal: true; focus: true; padding: 0
    closePolicy: Popup.NoAutoClose
    enterAnimation: Transition { NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Math.round(180 * root.animScale) } }
    exitAnimation: Transition { NumberAnimation { property: "opacity"; from: 1; to: 0; duration: Math.round(140 * root.animScale) } }
    Overlay.modal: Rectangle { color: root.childPage ? "transparent" : uiTheme.themeColors.scrim }
    background: FluidSurface {
        radius: 0; color: uiTheme.themeColors.glass; shadowEnabled: false; glass: false
        opacity: root.childPage ? 0 : 1
        Behavior on opacity { NumberAnimation { duration: Math.round(180 * root.animScale) } }
    }
    function syncStateControls(state) {
        languageControl.currentIndex = state.language === 'ru' ? 1 : 0
        themeControl.currentIndex = state.theme === "light" ? 1 : 0
        accentControl.currentIndex = Math.max(0, accentKeys.indexOf(state.accent_color))
        graphicsControl.currentIndex = Math.max(0, graphicsKeys.indexOf(state.graphics_api || "d3d11"))
        disableOverlaysToggle.checked = state.disable_performance_overlays !== false
        animationSpeedControl.currentIndex = Math.max(0, animationKeys.indexOf(state.animation_speed || "normal"))
        backdropBlurToggle.checked = state.backdrop_blur !== false
        defaultSectionControl.currentIndex = Math.max(0, defaultSectionKeys.indexOf(state.default_section || "last"))
        windowModeControl.currentIndex = Math.max(0, windowModeKeys.indexOf(state.window_mode || "fullscreen"))
        autoBackupToggle.checked = state.auto_backup !== false
        autoStartDownloadsToggle.checked = state.auto_start_downloads !== false
        reserveSpaceControl.currentIndex = Math.max(0, reserveValues.indexOf(Number(state.free_space_reserve_gb || 0.5)))
        vibrationToggle.checked = state.gamepad_vibration !== false
        deadzoneControl.currentIndex = Math.max(0, deadzoneValues.indexOf(Number(state.gamepad_deadzone || 16000)))
        repeatSpeedControl.currentIndex = Math.max(0, repeatKeys.indexOf(state.gamepad_repeat_speed || "normal"))
        autoCheckUpdatesToggle.checked = state.auto_check_updates !== false
        minimizeToTrayToggle.checked = state.minimize_to_tray !== false
        freezeOnSleepToggle.checked = state.freeze_on_sleep !== false
        autoTrimMemoryToggle.checked = Boolean(state.auto_trim_game_memory)
        productionToggle.checked = Boolean(state.production_mode)
    }
    onAboutToShow: {
        const state = bridge.settingsState
        destination.text = state.games_directories.join("\n")
        server.text = state.tactic_url
        knownGamesUrlField.text = state.known_games_url || ""
        metadata.checked = state.resolve_local_metadata
        scaleControl.value = Math.round(state.ui_scale * 100)
        autostart.checked = state.autostart
        keyboardToggle.checked = bridge.keyboard.enabled
        overlayToggle.checked = bridge.keyboard.options.overlay_enabled
        edgeSwipeToggle.checked = bridge.keyboard.options.edge_swipe_enabled
        edgeAutoHideToggle.checked = bridge.keyboard.options.edge_auto_hide
        syncStateControls(state)
        childPage = ""
        selectedField = -1; page = -1; menuIndex = updateAvailable ? 4 : 0; focusSide = 0; choiceField = -1
        body.contentY = 0
        navigationMotion.stop(); detailEntrance.stop()
        navigationProgress = 0; displayedPage = -1; detailReveal = 1
        Qt.callLater(() => {
            menuList.positionViewAtIndex(menuIndex, ListView.Contain)
            bridge.refreshAutostart()
        })
    }
    onAboutToHide: { inputInstallDialog.close(); navigationMotion.stop(); detailEntrance.stop(); endEditing(); password.text = "" }
    onSelectedFieldChanged: {
        if (selectedField < 0) return
        for (let i = 0; i < sectionFields.length; ++i) {
            if (sectionFields[i].indexOf(selectedField) >= 0) {
                if (page !== i) { page = i; menuIndex = i; body.contentY = 0 }
                break
            }
        }
        focusSide = 1
    }
    Connections {
        target: root.bridge
        function onSettingsChanged() {
            if (!root.visible) return
            const state = root.bridge.settingsState
            root.syncStateControls(state)
            if (!destination.activeFocus) destination.text = state.games_directories.join("\n")
            if (!knownGamesUrlField.activeFocus) knownGamesUrlField.text = state.known_games_url || ""
            scaleControl.value = Math.round(state.ui_scale * 100)
            metadata.checked = state.resolve_local_metadata
            autostart.checked = state.autostart
        }
    }
    Connections {
        target: root.bridge.keyboard
        function onChanged() {
            keyboardToggle.checked = root.bridge.keyboard.enabled
            overlayToggle.checked = root.bridge.keyboard.options.overlay_enabled
            edgeSwipeToggle.checked = root.bridge.keyboard.options.edge_swipe_enabled
            edgeAutoHideToggle.checked = root.bridge.keyboard.options.edge_auto_hide
        }
    }
    Connections {
        target: root.bridge.playerAccount
        function onChanged() {
            if (root.page === 0 && root.bridge.playerAccount.state.logged_in && [15, 16, 17].indexOf(root.selectedField) >= 0)
                root.selectedField = 13
        }
    }
    function endEditing() {
        destination.focus = false; server.focus = false; username.focus = false; password.focus = false; knownGamesUrlField.focus = false
        editingField = -1; bridge.keyboard.hide()
    }
    function save() {
        endEditing()
        return bridge.applySettings({
            games_directories: destination.text.split("\n").filter(v => v.trim().length > 0),
            tactic_url: server.text,
            resolve_local_metadata: metadata.checked,
            ui_scale: scaleControl.value / 100,
            theme: themeControl.currentIndex === 1 ? "light" : "dark",
            accent_color: accentKeys[Math.max(0, accentControl.currentIndex)],
            graphics_api: graphicsKeys[Math.max(0, graphicsControl.currentIndex)],
            disable_performance_overlays: disableOverlaysToggle.checked,
            animation_speed: animationKeys[Math.max(0, animationSpeedControl.currentIndex)],
            backdrop_blur: backdropBlurToggle.checked,
            default_section: defaultSectionKeys[Math.max(0, defaultSectionControl.currentIndex)],
            window_mode: windowModeKeys[Math.max(0, windowModeControl.currentIndex)],
            auto_backup: autoBackupToggle.checked,
            auto_start_downloads: autoStartDownloadsToggle.checked,
            free_space_reserve_gb: reserveValues[Math.max(0, reserveSpaceControl.currentIndex)],
            gamepad_vibration: vibrationToggle.checked,
            gamepad_deadzone: deadzoneValues[Math.max(0, deadzoneControl.currentIndex)],
            gamepad_repeat_speed: repeatKeys[Math.max(0, repeatSpeedControl.currentIndex)],
            auto_check_updates: autoCheckUpdatesToggle.checked,
            minimize_to_tray: minimizeToTrayToggle.checked,
            freeze_on_sleep: freezeOnSleepToggle.checked,
            auto_trim_game_memory: autoTrimMemoryToggle.checked,
            production_mode: productionToggle.checked,
            known_games_url: knownGamesUrlField.text
        })
    }
    function applyAppearance() {
        bridge.setAppearance(themeControl.currentIndex === 1 ? "light" : "dark", accentKeys[Math.max(0, accentControl.currentIndex)])
    }
    function applyGraphics() {
        bridge.setGraphicsOptions(graphicsKeys[Math.max(0, graphicsControl.currentIndex)], disableOverlaysToggle.checked)
    }
    function applyQuick() {
        bridge.setQuickOptions({
            animation_speed: animationKeys[Math.max(0, animationSpeedControl.currentIndex)],
            backdrop_blur: backdropBlurToggle.checked,
            default_section: defaultSectionKeys[Math.max(0, defaultSectionControl.currentIndex)],
            window_mode: windowModeKeys[Math.max(0, windowModeControl.currentIndex)],
            auto_backup: autoBackupToggle.checked,
            auto_start_downloads: autoStartDownloadsToggle.checked,
            free_space_reserve_gb: reserveValues[Math.max(0, reserveSpaceControl.currentIndex)],
            gamepad_vibration: vibrationToggle.checked,
            gamepad_deadzone: deadzoneValues[Math.max(0, deadzoneControl.currentIndex)],
            gamepad_repeat_speed: repeatKeys[Math.max(0, repeatSpeedControl.currentIndex)],
            auto_check_updates: autoCheckUpdatesToggle.checked,
            minimize_to_tray: minimizeToTrayToggle.checked,
            freeze_on_sleep: freezeOnSleepToggle.checked,
            auto_trim_game_memory: autoTrimMemoryToggle.checked,
            production_mode: productionToggle.checked
        })
    }
    function setStartup(enabled) { bridge.setAutostart(enabled); autostart.checked = bridge.settingsState.autostart }
    function openChild(kind) {
        endEditing()
        if (kind === "account" && !save()) return
        childPage = kind
        if (kind === "account") accountRequested()
        else if (kind === "bindings") bindingsRequested()
        else if (kind === "updates") updatesRequested()
        else if (kind === "catalogizer" || kind === "installerCatalogizer") catalogizerRequested(kind === "installerCatalogizer")
    }
    function finishChild(kind) {
        if (childPage !== kind) return
        childPage = ""
        Qt.callLater(revealSelected)
    }
    function availableFields() {
        if (page < 0) return []
        return sectionFields[page].filter(i => fields[i].visible && fields[i].enabled).concat([6, 7])
    }
    function enterSection(index) {
        endEditing(); menuIndex = index; page = index; body.contentY = 0
        const available = availableFields()
        selectedField = index === 4 && updateAvailable ? 8 : available.length ? available[0] : 7
        focusSide = 1
    }
    function revealSelected() {
        if (selectedField < 0 || selectedField === 6 || selectedField === 7) return
        const field = fields[selectedField]
        const y = field.mapToItem(body.contentItem, 0, 0).y
        const height = selectedField === 40 ? Math.min(body.height - 4, field.height + edgeSwipeWarning.height + 4 * root.uiScale) : field.height
        if (y < body.contentY) body.contentY = Math.max(0, y - 4)
        else if (y + height > body.contentY + body.height)
            body.contentY = Math.max(0, Math.min(body.contentHeight - body.height, y + height - body.height + 4))
    }
    function goBack() {
        if (page >= 0) { endEditing(); focusSide = 0; page = -1; selectedField = -1; menuList.positionViewAtIndex(menuIndex, ListView.Contain); return }
        close()
    }
    function activateField() {
        if (selectedField < 0 || !fields[selectedField].enabled) return
        if ([0, 2, 37].indexOf(selectedField) >= 0) {
            editingField = selectedField; fields[selectedField].forceActiveFocus(); bridge.keyboard.show()
        } else if (selectedField === 1) folderDialog.openFolder(destination.text.split("\n")[0])
        else if (selectedField === 3) metadata.checked = !metadata.checked
        else if (selectedField === 5) setStartup(!autostart.checked)
        else if (selectedField === 6) save()
        else if (selectedField === 7) goBack()
        else if (selectedField === 8) openChild("updates")
        else if (selectedField === 9) openChild("catalogizer")
        else if (selectedField === 10) openChild("bindings")
        else if ([11, 12, 21, 24, 26, 27, 29, 31, 32, 42].indexOf(selectedField) >= 0) fields[selectedField].popup.open()
        else if (selectedField === 13) openChild("account")
        else if (selectedField === 14) explainInputInstall()
        else if (selectedField === 18) openChild("installerCatalogizer")
        else if (selectedField === 39) bridge.organizeLibrary()
        else if (selectedField === 19) bridge.keyboard.setEnabled(!bridge.keyboard.enabled)
        else if (selectedField === 20) bridge.keyboard.setOverlayEnabled(!bridge.keyboard.options.overlay_enabled)
        else if (selectedField === 40) bridge.keyboard.setEdgeSwipeEnabled(!bridge.keyboard.options.edge_swipe_enabled)
        else if (selectedField === 41) bridge.keyboard.setEdgeAutoHide(!bridge.keyboard.options.edge_auto_hide)
        else if (selectedField === 22) { disableOverlaysToggle.checked = !disableOverlaysToggle.checked; applyGraphics() }
        else if ([23, 25, 28, 30, 33, 34, 35, 36, 38].indexOf(selectedField) >= 0) { fields[selectedField].checked = !fields[selectedField].checked; applyQuick() }
    }
    function explainInputInstall() {
        endEditing()
        inputInstallAction = 0
        inputInstallDialog.open()
    }
    function confirmInputInstall() {
        inputInstallDialog.close()
        bridge.installInputHost()
    }
    function handleAction(action) {
        if (inputInstallDialog.visible) {
            if (action === "back" || action === "menu") inputInstallDialog.close()
            else if (action === "up" || action === "down") inputInstallAction = 1 - inputInstallAction
            else if (action === "activate") {
                if (inputInstallAction === 1) confirmInputInstall()
                else inputInstallDialog.close()
            }
            return
        }
        if (childPage) return
        if (folderDialog.visible) { folderDialog.handleAction(action); return }
        if (choiceField >= 0) {
            const field = fields[choiceField]
            if (action === "back" || action === "menu") field.popup.close()
            else if (action === "up" || action === "down") choiceCursor = (choiceCursor + (action === "down" ? 1 : field.count - 1)) % field.count
            else if (action === "activate") {
                field.currentIndex = choiceCursor
                if (choiceField === 42) bridge.setLanguage(choiceCursor === 1 ? 'ru' : 'en')
                else if (choiceField === 21) applyGraphics()
                else if (choiceField === 11 || choiceField === 12) applyAppearance()
                else applyQuick()
                field.popup.close()
            }
            return
        }
        if (action === "menu") { close(); return }
        if (action === "back") { goBack(); return }
        if (page < 0 || focusSide === 0) {
            if (action === "up" || action === "down") {
                menuIndex = Math.max(0, Math.min(sections.length - 1, menuIndex + (action === "down" ? 1 : -1)))
                if (page >= 0) { page = menuIndex; body.contentY = 0 }
                menuList.positionViewAtIndex(menuIndex, ListView.Contain)
            } else if (action === "activate" || action === "right") enterSection(menuIndex)
            return
        }
        if (action === "up" || action === "down") {
            endEditing()
            const available = availableFields(), index = available.indexOf(selectedField)
            selectedField = available[Math.max(0, Math.min(available.length - 1, index + (action === "down" ? 1 : -1)))]
            revealSelected()
        } else if ((action === "left" || action === "right") && selectedField === 4) {
            scaleControl.value = Math.max(80, Math.min(120, scaleControl.value + (action === "right" ? 5 : -5)))
        } else if (action === "left") { endEditing(); focusSide = 0; selectedField = -1 }
        else if (action === "activate") activateField()
    }
    component Caption: Text {
        color: root.muted; font.pixelSize: 17 * root.uiScale; font.weight: Font.Light
        wrapMode: Text.WordWrap; width: parent.width
    }
    component FocusBox: FluidSurface {
        id: focusBox
        property bool selected: false
        color: selected ? uiTheme.themeColors.soft : "transparent"; radius: 16 * root.uiScale
        border.width: 0
        shadowEnabled: false; glass: false
        FocusFrame {
            anchors.fill: parent; z: 100; radius: focusBox.radius
            selected: focusBox.selected || (focusBox.parent && focusBox.parent.hovered === true)
        }
        Behavior on color { ColorAnimation { duration: Math.round(160 * root.animScale) } }
    }
    component SettingsButton: FluidButton {
        id: rowButton
        property int fieldIndex: -1
        property string glyph: ""
        focusPolicy: Qt.NoFocus
        highlighted: root.focusSide === 1 && root.selectedField === fieldIndex
        width: parent.width; implicitHeight: Math.max(66 * root.uiScale, contentItem.implicitHeight + 32 * root.uiScale)
        padding: 16 * root.uiScale
        leftPadding: (glyph ? 54 : 16) * root.uiScale
        rightPadding: (rowButton.highlighted && root.hints.device === "gamepad" && fieldIndex !== 6 && fieldIndex !== 7 ? 56 : 16) * root.uiScale + (fieldIndex === 8 && root.updateAvailable ? 32 : 0)
        background: FocusBox { selected: rowButton.highlighted }
        contentItem: Text { text: rowButton.text; color: rowButton.enabled ? root.ink : root.muted; font.pixelSize: 21 * root.uiScale; font.weight: Font.Light; verticalAlignment: Text.AlignVCenter; wrapMode: rowButton.glyph ? Text.WordWrap : Text.NoWrap; elide: Text.ElideRight }
        Item { visible: !!rowButton.glyph; width: 24 * root.uiScale; height: width; x: 16 * root.uiScale; anchors.verticalCenter: parent.verticalCenter
            KeyboardGlyph { keyName: rowButton.glyph; ink: root.ink; width: parent.width; height: width }
        }
        FluidSurface {
            visible: rowButton.fieldIndex !== 6 && rowButton.fieldIndex !== 7 && opacity > 0.01
            opacity: rowButton.highlighted && root.hints.device === "gamepad" ? 1 : 0
            scale: 0.85 + 0.15 * opacity
            Behavior on opacity { NumberAnimation { duration: Math.round(180 * root.animScale); easing.type: Easing.OutBack; easing.overshoot: 1.2 } }
            anchors.right: parent.right; anchors.rightMargin: 16 * root.uiScale + (rowButton.fieldIndex === 8 && root.updateAvailable ? 32 : 0)
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(28 * root.uiScale, btnHintText.implicitWidth + 16 * root.uiScale); height: 28 * root.uiScale; radius: height / 2
            color: uiTheme.themeColors.accent; border.width: 0; shadowEnabled: false
            Text { id: btnHintText; anchors.centerIn: parent; text: root.hints.activate; color: uiTheme.themeColors.accentText; font.pixelSize: 13 * root.uiScale; font.weight: Font.Bold }
        }
        Rectangle { x: 16 * root.uiScale; y: parent.height - 1; width: parent.width - x * 2; height: 1; color: uiTheme.themeColors.line }
        onPressed: if (fieldIndex !== 7) root.selectedField = fieldIndex
    }
    component SettingsToggle: CheckBox {
        id: toggle
        property int fieldIndex
        property string glyph: ""
        readonly property bool selectedByPad: root.focusSide === 1 && root.selectedField === toggle.fieldIndex && root.hints.device === "gamepad"
        width: parent.width; implicitHeight: Math.max(70 * root.uiScale, contentItem.implicitHeight + 32 * root.uiScale); focusPolicy: Qt.NoFocus
        leftPadding: (glyph ? 54 : 16) * root.uiScale; rightPadding: (selectedByPad ? 116 : 80) * root.uiScale
        background: FocusBox { selected: root.focusSide === 1 && root.selectedField === toggle.fieldIndex }
        indicator: Rectangle {
            width: 44 * root.uiScale; height: 24 * root.uiScale; radius: height / 2
            x: toggle.width - width - 16 * root.uiScale; y: (toggle.height - height) / 2
            color: toggle.checked ? uiTheme.themeColors.accent : uiTheme.themeColors.line
            Rectangle { width: 18 * root.uiScale; height: width; radius: width / 2; y: 3 * root.uiScale; x: (toggle.checked ? 23 : 3) * root.uiScale; color: toggle.checked ? uiTheme.themeColors.accentText : root.ink; Behavior on x { NumberAnimation { duration: Math.round(130 * root.animScale) } } }
        }
        contentItem: Text { text: toggle.text; color: root.ink; font.pixelSize: 21 * root.uiScale; font.weight: Font.Light; wrapMode: Text.WordWrap; verticalAlignment: Text.AlignVCenter }
        Item { visible: !!toggle.glyph; width: 24 * root.uiScale; height: width; x: 16 * root.uiScale; anchors.verticalCenter: parent.verticalCenter
            KeyboardGlyph { keyName: toggle.glyph; ink: root.ink; width: parent.width; height: width }
        }
        FluidSurface {
            visible: opacity > 0.01
            opacity: toggle.selectedByPad ? 1 : 0
            scale: 0.85 + 0.15 * opacity
            Behavior on opacity { NumberAnimation { duration: Math.round(180 * root.animScale); easing.type: Easing.OutBack; easing.overshoot: 1.2 } }
            anchors.right: parent.right; anchors.rightMargin: 72 * root.uiScale
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(26 * root.uiScale, toggleHintText.implicitWidth + 14 * root.uiScale); height: 26 * root.uiScale; radius: height / 2
            color: uiTheme.themeColors.accent; border.width: 0; shadowEnabled: false
            Text { id: toggleHintText; anchors.centerIn: parent; text: root.hints.activate; color: uiTheme.themeColors.accentText; font.pixelSize: 12 * root.uiScale; font.weight: Font.Bold }
        }
        onPressed: root.selectedField = fieldIndex
        MouseArea {
            anchors.fill: parent
            onPressed: root.selectedField = toggle.fieldIndex
            onClicked: { toggle.checked = !toggle.checked; toggle.clicked() }
        }
    }
    component SettingsField: TextField {
        property int fieldIndex
        width: parent.width; height: 58 * root.uiScale; padding: 16 * root.uiScale
        color: root.ink; placeholderTextColor: root.muted; font.pixelSize: 20 * root.uiScale; selectByMouse: true
        background: FocusBox { color: uiTheme.themeColors.base; selected: root.focusSide === 1 && root.selectedField === parent.fieldIndex }
        onActiveFocusChanged: if (activeFocus) { root.selectedField = fieldIndex; root.editingField = fieldIndex }
    }
    component Choice: FluidComboBox {
        id: choice
        property int fieldIndex
        property string label
        width: parent.width; height: 70 * root.uiScale; focusPolicy: Qt.NoFocus
        background: FocusBox { selected: root.focusSide === 1 && root.selectedField === choice.fieldIndex }
        contentItem: Item {
            Text { anchors.left: parent.left; anchors.leftMargin: 16 * root.uiScale; anchors.verticalCenter: parent.verticalCenter; width: parent.width * .5; text: choice.label; color: root.ink; font.pixelSize: 21 * root.uiScale; font.weight: Font.Light }
            Text { anchors.right: parent.right; anchors.rightMargin: 36 * root.uiScale; anchors.verticalCenter: parent.verticalCenter; width: parent.width * .45; text: choice.displayText; color: root.muted; font.pixelSize: 18 * root.uiScale; horizontalAlignment: Text.AlignRight; elide: Text.ElideRight }
        }
        indicator: Text { x: parent.width - width - 16 * root.uiScale; y: (parent.height - height) / 2; text: "›"; color: root.ink; font.pixelSize: 26 * root.uiScale }
        delegate: ItemDelegate {
            required property int index
            required property string modelData
            width: choice.popup.width - 12; height: 56 * root.uiScale
            highlighted: root.choiceCursor === index
            background: FocusBox { selected: parent.highlighted }
            contentItem: Text { text: modelData; color: root.ink; font.pixelSize: 20 * root.uiScale; verticalAlignment: Text.AlignVCenter }
            onClicked: {
                choice.currentIndex = index
                if (choice.fieldIndex === 42) root.bridge.setLanguage(index === 1 ? 'ru' : 'en')
                else if (choice.fieldIndex === 21) root.applyGraphics()
                else if (choice.fieldIndex === 11 || choice.fieldIndex === 12) root.applyAppearance()
                else root.applyQuick()
                choice.popup.close()
            }
        }
        popup: MotionPopup {
            x: choice.width * .42; y: choice.height + 4; width: choice.width * .58; padding: 6
            implicitHeight: choice.count * 56 * root.uiScale + 12
            background: FluidSurface { color: uiTheme.themeColors.glass; border.color: uiTheme.themeColors.glassEdge; radius: 20 }
            contentItem: ListView { clip: true; model: choice.delegateModel; currentIndex: root.choiceCursor }
            onAboutToShow: { root.selectedField = choice.fieldIndex; root.choiceField = choice.fieldIndex; root.choiceCursor = choice.currentIndex }
            onAboutToHide: root.choiceField = -1
        }
    }
    contentItem: Item {
        opacity: folderDialog.visible || root.childPage ? 0 : 1
        enabled: !root.childPage
        Behavior on opacity { NumberAnimation { duration: Math.round(180 * root.animScale) } }
        DirectoryPicker { id: folderDialog; parent: root.parent; bridge: root.bridge; onChosen: function(path) { destination.text = destination.text.trim() + (destination.text.trim().length ? "\n" : "") + path } }
        Text {
            id: title; objectName: "settingsTitle"
            x: root.width * .05 - root.navigationProgress * 18 * root.uiScale; y: root.height * .067
            text: qsTranslate("NGS", "Settings"); opacity: Math.max(0, 1 - root.navigationProgress * 2)
            Accessible.ignored: opacity < .01
            color: root.ink; font.pixelSize: 36 * root.uiScale; font.weight: Font.Light
        }
        Text {
            objectName: "settingsSectionTitle"
            x: root.width * .05 + (1 - root.navigationProgress) * 24 * root.uiScale; y: title.y
            text: root.displayedPage >= 0 ? root.sections[root.displayedPage] : ""
            opacity: Math.max(0, root.navigationProgress * 2 - 1) * root.detailReveal
            Accessible.ignored: opacity < .01
            color: root.ink; font.pixelSize: 36 * root.uiScale; font.weight: Font.Light
        }
        FluidButton {
            objectName: "settingsBackTop"; anchors.right: parent.right; anchors.rightMargin: parent.width * .05; y: title.y
            width: 52 * root.uiScale; height: width; text: "‹"; font.pixelSize: 30 * root.uiScale
            background: FocusBox { selected: parent.hovered }
            contentItem: Text { text: parent.text; color: root.ink; font: parent.font; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
            onClicked: root.goBack()
        }
        ListView {
            id: menuList; objectName: "settingsCategories"
            x: parent.width * (.155 - .105 * root.navigationProgress)
            y: parent.height * .175
            width: parent.width * (.69 - .435 * root.navigationProgress)
            height: footer.y - y - 24 * root.uiScale
            clip: true; model: root.sections; spacing: 4 * root.uiScale
            currentIndex: root.menuIndex; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}
            delegate: FluidButton {
                id: category
                required property int index
                required property string modelData
                objectName: "settingsCategory-" + index
                readonly property bool currentSection: root.page === index
                Accessible.description: currentSection ? qsTranslate("NGS", "Open section") : ""
                Accessible.name: modelData + (index === 4 && root.updateAvailable ? qsTranslate("NGS", " · NGS update available") : "")
                x: 6; width: menuList.width - 12; height: Math.max(64, 78 * root.uiScale)
                highlighted: root.focusSide === 0 && root.menuIndex === index
                padding: 12 * root.uiScale; focusPolicy: Qt.NoFocus
                background: FocusBox {
                    selected: category.highlighted
                    color: selected ? uiTheme.themeColors.soft : category.currentSection ? uiTheme.themeColors.button : "transparent"
                    Rectangle {
                        objectName: "settingsCurrent-" + category.index
                        visible: category.currentSection
                        x: 0; anchors.verticalCenter: parent.verticalCenter
                        width: 3 * root.uiScale; height: 24 * root.uiScale; radius: width / 2
                        color: uiTheme.themeColors.accent
                    }
                }
                contentItem: Row {
                    spacing: 16 * root.uiScale
                    Canvas {
                        objectName: "settingsIcon-" + category.index
                        width: 28 * root.uiScale; height: width; anchors.verticalCenter: parent.verticalCenter
                        property color ink: root.ink
                        onInkChanged: requestPaint()
                        onVisibleChanged: if (visible) requestPaint()
                        onWidthChanged: requestPaint()
                        onHeightChanged: requestPaint()
                        onPaint: {
                            const c = getContext("2d"); c.reset(); c.scale(width/24,height/24); c.strokeStyle = root.ink; c.lineWidth = 1.6; c.lineCap = "round"; c.lineJoin = "round"; c.beginPath()
                            if (category.index === 0) { c.arc(12,7,4,0,Math.PI*2); c.moveTo(4,22); c.bezierCurveTo(4,12,20,12,20,22) }
                            else if (category.index === 1) { c.rect(2,3,20,14); c.moveTo(8,22); c.lineTo(16,22); c.moveTo(12,17); c.lineTo(12,22) }
                            else if (category.index === 2) { c.rect(3,3,18,18); c.moveTo(3,15); c.lineTo(21,15); c.moveTo(16,18); c.lineTo(18,18) }
                            else if (category.index === 3) { c.moveTo(6,7); c.bezierCurveTo(2,7,0,22,5,19); c.lineTo(9,15); c.lineTo(15,15); c.bezierCurveTo(24,25,24,7,18,7); c.closePath(); c.moveTo(5,11); c.lineTo(11,11); c.moveTo(8,8); c.lineTo(8,14); c.moveTo(17,10); c.lineTo(17,11) }
                            else if (category.index === 4) { c.rect(4,3,16,18); c.moveTo(8,7); c.lineTo(16,7); c.moveTo(8,12); c.lineTo(16,12); c.moveTo(8,17); c.lineTo(12,17) }
                            else { c.rect(3,3,7,18); c.rect(14,3,7,18); c.moveTo(6,7); c.lineTo(7,7); c.moveTo(17,7); c.lineTo(18,7) }
                            c.stroke()
                        }
                    }
                    Text { width: parent.width - 44 * root.uiScale - (category.index === 4 && root.updateAvailable ? 32 : 0) - (category.highlighted && root.hints.device === "gamepad" ? 40 * root.uiScale : 0); height: parent.height; text: category.modelData; color: root.page === category.index || root.page < 0 || category.highlighted ? root.ink : root.muted; font.pixelSize: 22 * root.uiScale; font.weight: Font.Light; verticalAlignment: Text.AlignVCenter; wrapMode: Text.WordWrap }
                }
                FluidSurface {
                    visible: opacity > 0.01
                    opacity: category.highlighted && root.hints.device === "gamepad" ? 1 : 0
                    scale: 0.85 + 0.15 * opacity
                    Behavior on opacity { NumberAnimation { duration: Math.round(180 * root.animScale); easing.type: Easing.OutBack; easing.overshoot: 1.2 } }
                    anchors.right: parent.right; anchors.rightMargin: 16 * root.uiScale + (category.index === 4 && root.updateAvailable ? 32 : 0)
                    anchors.verticalCenter: parent.verticalCenter
                    width: Math.max(28 * root.uiScale, catHintText.implicitWidth + 16 * root.uiScale); height: 28 * root.uiScale; radius: height / 2
                    color: uiTheme.themeColors.accent; border.width: 0; shadowEnabled: false
                    Text { id: catHintText; anchors.centerIn: parent; text: root.hints.activate; color: uiTheme.themeColors.accentText; font.pixelSize: 13 * root.uiScale; font.weight: Font.Bold }
                }
                NotificationBadge {
                    objectName: "settingsSystemUpdateBadge-" + category.index
                    count: category.index === 4 && root.updateAvailable ? 1 : 0
                    anchors.right: parent.right; anchors.rightMargin: 16 * root.uiScale; anchors.verticalCenter: parent.verticalCenter
                }
                Rectangle { opacity: 1 - root.navigationProgress; x: 56 * root.uiScale; y: parent.height - 1; width: parent.width - x - 16 * root.uiScale; height: 1; color: uiTheme.themeColors.line }
                onClicked: root.enterSection(index)
            }
        }
        Flickable {
            id: body; objectName: "settingsDetails"
            visible: root.displayedPage >= 0; enabled: root.page >= 0
            opacity: root.navigationProgress * root.detailReveal
            x: parent.width * (.355 + .10 * (1 - root.navigationProgress)) + (1 - root.detailReveal) * 18 * root.uiScale
            y: menuList.y + 4 * root.uiScale
            width: parent.width * .59; height: footer.y - y - 64 * root.uiScale
            clip: true; contentWidth: width; contentHeight: content.height + 12 * root.uiScale
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}
            Column {
                id: content; x: 4 * root.uiScale; width: body.width - 12 * root.uiScale; spacing: 12 * root.uiScale
                Column {
                    visible: root.displayedPage === 0; width: parent.width; spacing: 16 * root.uiScale
                    Caption { text: qsTranslate("NGS", "Server address") }
                    SettingsField { id: server; objectName: "settingsServer"; fieldIndex: 2; maximumLength: 512; onAccepted: root.save() }
                    // Field indices 15–17 stay reserved; sign-in lives in the account subpage.
                    SettingsField { id: username; objectName: "settingsUsername"; fieldIndex: 15; visible: false; enabled: false }
                    SettingsField { id: password; objectName: "settingsPassword"; fieldIndex: 16; visible: false; enabled: false; echoMode: TextInput.Password }
                    SettingsButton { id: loginButton; objectName: "settingsLogin"; fieldIndex: 17; visible: false; enabled: false }
                    SettingsButton { id: accountButton; objectName: "openPlayerAccount"; fieldIndex: 13; text: root.bridge.playerAccount.state.logged_in ? qsTranslate("NGS", "Profile and backups · ") + root.bridge.playerAccount.state.username + "  ›" : qsTranslate("NGS", "Sign in to NGS  ›"); onClicked: root.openChild("account") }
                    SettingsToggle { id: autoBackupToggle; objectName: "settingsAutoBackup"; fieldIndex: 23; text: qsTranslate("NGS", "Automatically back up library and settings"); onClicked: root.applyQuick() }
                    Caption { text: root.bridge.playerAccount.state.status + (root.bridge.playerAccount.state.logged_in && root.bridge.playerAccount.state.backup_status ? "\n" + root.bridge.playerAccount.state.backup_status : "") }
                }
                Column {
                    visible: root.displayedPage === 1; width: parent.width; spacing: 4 * root.uiScale
                    Choice { id: languageControl; objectName: "settingsLanguage"; fieldIndex: 42; label: qsTranslate("NGS", "Language"); model: ["English", "Русский"] }
                    Caption { text: qsTranslate("NGS", "NGS restarts to apply the language. The running game will not be interrupted.") }
                    Choice { id: themeControl; objectName: "settingsTheme"; fieldIndex: 11; label: qsTranslate("NGS", "Theme"); model: [qsTranslate("NGS", "Dark"), qsTranslate("NGS", "Light")] }
                    Choice { id: accentControl; objectName: "settingsAccent"; fieldIndex: 12; label: qsTranslate("NGS", "Accent color"); model: root.accentLabels }
                    Choice { id: graphicsControl; objectName: "settingsGraphicsApi"; fieldIndex: 21; label: qsTranslate("NGS", "Graphics API"); model: root.graphicsLabels }
                    SettingsToggle { id: disableOverlaysToggle; objectName: "settingsDisableOverlays"; fieldIndex: 22; text: qsTranslate("NGS", "Hide third-party overlays and FPS counters"); onClicked: root.applyGraphics() }
                    Caption { text: qsTranslate("NGS", "Blocks third-party hooks in the NGS window (RTSS, Afterburner, Adrenalin and Vulkan layers) while preserving game overlays. NGS restarts automatically after changes, or after the current game closes.") }
                    Slider {
                        id: scaleControl; objectName: "settingsUiScale"; width: parent.width; height: 108 * root.uiScale
                        from: 80; to: 120; stepSize: 5; snapMode: Slider.SnapAlways; focusPolicy: Qt.NoFocus
                        background: Item {
                            FocusBox { anchors.fill: parent; selected: root.focusSide === 1 && root.selectedField === 4 }
                            Text { x: 16 * root.uiScale; y: 16 * root.uiScale; text: qsTranslate("NGS", "Interface scale"); color: root.ink; font.pixelSize: 21 * root.uiScale; font.weight: Font.Light }
                            Text { anchors.right: parent.right; anchors.rightMargin: 16 * root.uiScale; y: 16 * root.uiScale; text: scaleControl.value + "%"; color: root.muted; font.pixelSize: 19 * root.uiScale }
                            Rectangle { x: 18 * root.uiScale; y: 78 * root.uiScale; width: parent.width - x*2; height: 2; color: uiTheme.themeColors.line }
                        }
                        handle: Rectangle { x: 18 * root.uiScale + scaleControl.visualPosition * (scaleControl.width - 50 * root.uiScale); y: 69 * root.uiScale; width: 18 * root.uiScale; height: width; radius: width / 2; color: root.ink }
                        onPressedChanged: if (pressed) root.selectedField = 4
                    }
                    Choice { id: animationSpeedControl; objectName: "settingsAnimationSpeed"; fieldIndex: 24; label: qsTranslate("NGS", "Animation speed"); model: root.animationLabels }
                    SettingsToggle { id: backdropBlurToggle; objectName: "settingsBackdropBlur"; fieldIndex: 25; text: qsTranslate("NGS", "Blur backgrounds behind windows and menus"); onClicked: root.applyQuick() }
                    Choice { id: defaultSectionControl; objectName: "settingsDefaultSection"; fieldIndex: 26; label: qsTranslate("NGS", "Startup section"); model: root.defaultSectionLabels }
                    Choice { id: windowModeControl; objectName: "settingsWindowMode"; fieldIndex: 27; label: qsTranslate("NGS", "Window mode"); model: root.windowModeLabels }
                    Caption { objectName: "settingsRefreshRate"; text: qsTranslate("NGS", "Display refresh rate: ") + Math.round(root.bridge.displayRefreshRate) + qsTranslate("NGS", " Hz") }
                }
                Column {
                    visible: root.displayedPage === 2; width: parent.width; spacing: 16 * root.uiScale
                    Caption { text: qsTranslate("NGS", "Game installation folders") }
                    TextArea {
                        id: destination; objectName: "settingsDestination"; width: parent.width; height: 170 * root.uiScale; padding: 16 * root.uiScale
                        color: root.ink; font.pixelSize: 20 * root.uiScale; selectByMouse: true; wrapMode: TextEdit.NoWrap
                        placeholderText: "games\nD:\\Games\nE:\\Games"
                        background: FocusBox { color: uiTheme.themeColors.base; selected: root.focusSide === 1 && root.selectedField === 0 }
                        onActiveFocusChanged: if (activeFocus) { root.selectedField = 0; root.editingField = 0 }
                    }
                    SettingsButton { id: addDestination; fieldIndex: 1; text: qsTranslate("NGS", "Add folder  +"); onClicked: folderDialog.openFolder(destination.text.split("\n")[0]) }
                    SettingsToggle { id: autoStartDownloadsToggle; objectName: "settingsAutoStartDownloads"; fieldIndex: 28; text: qsTranslate("NGS", "Automatically resume downloads at startup"); onClicked: root.applyQuick() }
                    Choice { id: reserveSpaceControl; objectName: "settingsReserveSpace"; fieldIndex: 29; label: qsTranslate("NGS", "Free space reserve"); model: root.reserveLabels }
                    Caption { text: qsTranslate("NGS", "The first folder is used by default. Installed games stay where they are.") }
                    SettingsButton { id: organizeButton; objectName: "organizeLibraryButton"; fieldIndex: 39; glyph: "@library"; text: qsTranslate("NGS", "Organize library"); enabled: !root.bridge.operationBusy && !root.bridge.runningState.running; onClicked: root.bridge.organizeLibrary() }
                    Caption { text: qsTranslate("NGS", "Use readable folder and library names for games downloaded through NGS. Independently installed games and NAS files are not changed.") }
                }
                Column {
                    visible: root.displayedPage === 3; width: parent.width; spacing: 4 * root.uiScale
                    SettingsButton { id: bindingsButton; objectName: "openBindingsButton"; fieldIndex: 10; text: qsTranslate("NGS", "Buttons and gamepad  ›"); onClicked: root.openChild("bindings") }
                    SettingsToggle { id: keyboardToggle; objectName: "settingsKeyboardEnabled"; fieldIndex: 19; glyph: "@keyboard"; text: qsTranslate("NGS", "NGS keyboard"); onClicked: root.bridge.keyboard.setEnabled(checked) }
                    Caption { text: qsTranslate("NGS", "Enables the NGS on-screen keyboard, tap-to-show and manual activation.") }
                    SettingsToggle { id: overlayToggle; objectName: "settingsOverlayEnabled"; fieldIndex: 20; glyph: "@overlay"; text: qsTranslate("NGS", "NGS quick settings"); onClicked: root.bridge.keyboard.setOverlayEnabled(checked) }
                    Caption { text: qsTranslate("NGS", "Audio, brightness and other quick settings. Turning this off closes the panel and disables all activation methods, including the edge handle.") }
                    SettingsToggle { id: edgeSwipeToggle; objectName: "settingsEdgeSwipeEnabled"; fieldIndex: 40; glyph: "@overlay"; text: qsTranslate("NGS", "Quick settings edge handle"); enabled: overlayToggle.checked; onClicked: root.bridge.keyboard.setEdgeSwipeEnabled(checked) }
                    Caption { id: edgeSwipeWarning; objectName: "settingsEdgeSwipeWarning"; text: qsTranslate("NGS", "While NGS is active in fullscreen, the edge handle disables Windows edge gestures, including Start. Turn off the handle to restore them. The Start button still works.") }
                    SettingsToggle { id: edgeAutoHideToggle; objectName: "settingsEdgeAutoHide"; fieldIndex: 41; glyph: "@overlay"; text: qsTranslate("NGS", "Auto-hide the handle after 4 seconds"); enabled: overlayToggle.checked && edgeSwipeToggle.checked; onClicked: root.bridge.keyboard.setEdgeAutoHide(checked) }
                    Caption { text: qsTranslate("NGS", "The handle slides away when idle; swipe from the same top-edge area to reveal it. Turn off auto-hide to keep it visible.") }
                    SettingsButton {
                        id: inputHostButton; objectName: "installInputHostButton"; fieldIndex: 14
                        glyph: "@settings"; text: qsTranslate("NGS", "Install / update NGS Input")
                        onClicked: root.explainInputInstall()
                    }
                    Caption { text: qsTranslate("NGS", "Allows the keyboard to appear above Start and Windows system windows. Configure layouts, size and opacity through the keyboard's settings button.") }
                    SettingsToggle { id: vibrationToggle; objectName: "settingsGamepadVibration"; fieldIndex: 30; text: qsTranslate("NGS", "Gamepad vibration (haptic feedback)"); onClicked: root.applyQuick() }
                    Choice { id: deadzoneControl; objectName: "settingsGamepadDeadzone"; fieldIndex: 31; label: qsTranslate("NGS", "Stick deadzone"); model: root.deadzoneLabels }
                    Choice { id: repeatSpeedControl; objectName: "settingsGamepadRepeat"; fieldIndex: 32; label: qsTranslate("NGS", "Navigation repeat speed"); model: root.repeatLabels }
                }
                Column {
                    visible: root.displayedPage === 4; width: parent.width; spacing: 4 * root.uiScale
                    SettingsToggle { id: autostart; objectName: "settingsAutostart"; fieldIndex: 5; text: qsTranslate("NGS", "Start with Windows"); enabled: root.bridge.settingsState.autostart_supported; onClicked: root.setStartup(checked) }
                    SettingsButton {
                        id: updatesButton; objectName: "openUpdatesButton"; fieldIndex: 8
                        text: root.updateAvailable ? qsTranslate("NGS", "NGS updates · new version available") : qsTranslate("NGS", "NGS updates  ›")
                        onClicked: root.openChild("updates")
                        NotificationBadge {
                            objectName: "settingsUpdatesBadge"; count: root.updateAvailable ? 1 : 0
                            anchors.right: parent.right; anchors.rightMargin: 16 * root.uiScale; anchors.verticalCenter: parent.verticalCenter
                        }
                    }
                    SettingsToggle { id: autoCheckUpdatesToggle; objectName: "settingsAutoCheckUpdates"; fieldIndex: 33; text: qsTranslate("NGS", "Automatically check for updates"); onClicked: root.applyQuick() }
                    SettingsToggle { id: minimizeToTrayToggle; objectName: "settingsMinimizeToTray"; fieldIndex: 34; text: qsTranslate("NGS", "Minimize NGS to the system tray"); onClicked: root.applyQuick() }
                    SettingsToggle { id: freezeOnSleepToggle; objectName: "settingsFreezeOnSleep"; fieldIndex: 35; text: qsTranslate("NGS", "Freeze the running game before sleep"); onClicked: root.applyQuick() }
                    SettingsToggle { id: autoTrimMemoryToggle; objectName: "settingsAutoTrimMemory"; fieldIndex: 36; text: qsTranslate("NGS", "Trim game memory when freezing"); onClicked: root.applyQuick() }
                    SettingsToggle { id: productionToggle; objectName: "settingsProductionMode"; fieldIndex: 38; text: qsTranslate("NGS", "Production mode"); onClicked: root.applyQuick() }
                    Caption { text: qsTranslate("NGS", "Disables diagnostic logs, tracing and diagnostic uploads. NGS restarts automatically, or after the current game closes.") }
                    Caption { text: autostart.enabled ? qsTranslate("NGS", "Startup settings apply immediately for the current Windows user.") : qsTranslate("NGS", "Automatic startup is available in the Windows build of NGS.") }
                }
                Column {
                    visible: root.displayedPage === 5; width: parent.width; spacing: 4 * root.uiScale
                    SettingsToggle { id: metadata; fieldIndex: 3; text: qsTranslate("NGS", "Find artwork for local games") }
                    SettingsButton { id: catalogizerButton; objectName: "openCatalogizerButton"; fieldIndex: 9; glyph: "@library"; text: qsTranslate("NGS", "Ready-to-play game catalog manager"); onClicked: root.openChild("catalogizer") }
                    SettingsButton { id: installerCatalogizerButton; objectName: "openInstallerCatalogizerButton"; fieldIndex: 18; glyph: "@library"; text: qsTranslate("NGS", "Installer package catalog manager"); onClicked: root.openChild("installerCatalogizer") }
                    Caption { text: qsTranslate("NGS", "Game matching database update URL (JSON, for example on GitHub)") }
                    SettingsField { id: knownGamesUrlField; objectName: "settingsKnownGamesUrl"; fieldIndex: 37; maximumLength: 512; placeholderText: "https://raw.githubusercontent.com/.../known_games.json"; onAccepted: root.save() }
                    Caption { text: qsTranslate("NGS", "Add games and publish them to the server catalog.") }
                }
            }
        }
        Item {
            id: footer; x: parent.width * .05; y: parent.height - height - 24 * root.uiScale
            width: parent.width * .90; height: 52 * root.uiScale
            SettingsButton { id: backButton; objectName: "settingsBack"; fieldIndex: 7; width: 180 * root.uiScale; height: parent.height; text: qsTranslate("NGS", "Back · ") + root.hints.back; onClicked: root.goBack() }
            Caption { visible: root.page < 0 && root.hints.device === "gamepad"; x: backButton.width + 24 * root.uiScale; anchors.verticalCenter: parent.verticalCenter; width: parent.width - x; text: root.hints.activate + qsTranslate("NGS", "   Select") }
            SettingsButton { id: saveButton; objectName: "settingsSave"; fieldIndex: 6; anchors.right: parent.right; width: 190 * root.uiScale; height: parent.height; visible: root.displayedPage >= 0; enabled: root.page >= 0; opacity: root.navigationProgress; text: qsTranslate("NGS", "Apply"); onClicked: root.save() }
        }
        Caption {
            objectName: "settingsMessage"; visible: root.displayedPage >= 0; opacity: root.navigationProgress * root.detailReveal
            x: body.x + 12 * root.uiScale; y: footer.y - height - 8 * root.uiScale; width: body.width - 24 * root.uiScale
            text: root.bridge.operationBusy ? root.bridge.operationStatus : root.bridge.settingsStatus; font.pixelSize: 14 * root.uiScale; maximumLineCount: 2; elide: Text.ElideRight
        }
    }

    MotionPopup {
        id: inputInstallDialog; objectName: "inputInstallDialog"
        parent: root.parent
        anchors.centerIn: parent
        width: Math.min(parent.width - 32, 520 * root.uiScale)
        height: inputInstallContent.implicitHeight + padding * 2
        padding: 24 * root.uiScale; modal: true; focus: true
        closePolicy: Popup.NoAutoClose
        Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim }
        background: FluidSurface { color: uiTheme.themeColors.glass; radius: 24; elevation: 1 }
        onAboutToShow: root.childPage = "inputInstall"
        onClosed: root.finishChild("inputInstall")
        contentItem: Column {
            id: inputInstallContent; spacing: 16 * root.uiScale
            Text { text: qsTranslate("NGS", "NGS Input installation"); width: parent.width; wrapMode: Text.WordWrap; color: root.ink; font.pixelSize: 24 * root.uiScale }
            Caption { text: qsTranslate("NGS", "Installs the component for the keyboard above Start and system windows. Windows may request administrator permission. The built-in keyboard already works without installation.") }
            FluidButton {
                objectName: "inputInstallCancel"; width: parent.width; height: Math.max(48, 54 * root.uiScale)
                text: qsTranslate("NGS", "Back · ") + root.hints.back; leftPadding: 46 * root.uiScale
                highlighted: root.inputInstallAction === 0
                Item { width: 24; height: 24; x: 14; anchors.verticalCenter: parent.verticalCenter
                    KeyboardGlyph { keyName: "@back"; ink: root.ink; width: parent.width; height: width }
                }
                onClicked: inputInstallDialog.close()
            }
            FluidButton {
                objectName: "inputInstallConfirm"; width: parent.width; height: Math.max(48, 54 * root.uiScale)
                text: qsTranslate("NGS", "Install / update · ") + root.hints.activate; leftPadding: 46 * root.uiScale
                highlighted: root.inputInstallAction === 1
                Item { width: 24; height: 24; x: 14; anchors.verticalCenter: parent.verticalCenter
                    KeyboardGlyph { keyName: "@settings"; ink: root.ink; width: parent.width; height: width }
                }
                onClicked: root.confirmInputInstall()
            }
        }
    }
}
