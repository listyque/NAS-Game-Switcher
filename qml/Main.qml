import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Effects
import "FocusNavigation.js" as Navigation

ApplicationWindow {
    id: window
    objectName: "launcherWindow"
    required property QtObject bridge
    readonly property var hints: bridge.inputHints
    width: 1280
    height: 800
    minimumWidth: 800
    minimumHeight: 500
    visible: false
    color: uiTheme.themeColors.background
    palette {
        window: uiTheme.themeColors.surface; windowText: uiTheme.themeColors.text
        base: uiTheme.themeColors.base; text: uiTheme.themeColors.text
        button: uiTheme.themeColors.button; buttonText: uiTheme.themeColors.text
        highlight: uiTheme.themeColors.accent; highlightedText: uiTheme.themeColors.accentText
    }
    title: "NGS"
    Item {
        id: loginGate
        property int selectedAction: 0
        anchors.fill: parent; z: 1000; visible: bridge.loginRequired
        Rectangle { anchors.fill: parent; color: uiTheme.themeColors.background }
        MouseArea { anchors.fill: parent }
        Column {
            visible: !window.modalVisible
            anchors.centerIn: parent; width: Math.min(parent.width - 64, 600); spacing: 18
            Text { width: parent.width; text: qsTranslate("NGS", "Sign in to NGS", "Вход в NGS"); color: uiTheme.themeColors.text; font.pixelSize: 36; horizontalAlignment: Text.AlignHCenter }
            Text { width: parent.width; text: bridge.playerAccount.state.status; color: uiTheme.themeColors.muted; font.pixelSize: 18; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter }
            FluidButton { objectName: "loginAccountButton"; width: parent.width; height: 56; text: qsTranslate("NGS", "Sign in to NGS", "Войти в аккаунт NGS"); highlighted: loginGate.selectedAction === 0; onClicked: playerAccountPane.open() }
            FluidButton { objectName: "loginSettingsButton"; width: parent.width; height: 52; text: qsTranslate("NGS", "Settings · Server"); highlighted: loginGate.selectedAction === 1; onClicked: settingsPane.open() }
            FluidButton { objectName: "loginLocalButton"; width: parent.width; height: 56; text: qsTranslate("NGS", "Use without a server"); highlighted: loginGate.selectedAction === 2; onClicked: bridge.useLocalLibrary() }
        }
    }
    property int section: bridge.initialUi.section
    property int selectedIndex: bridge.initialUi.game_index
    readonly property bool startupReady: selectionReady && (section === 2 || backdrop.ready)
    property bool selectionReady: false
    property real pendingScroll: -1
    property real pendingGridScroll: -1
    property bool revealGridSelection: true
    readonly property bool gridMode: bridge.gamesView === "grid"
    property var searchQueries: ({})
    readonly property string searchQuery: searchQueries[String(section)] || ""
    property int focusZone: 2
    property int navigationIndex: section === 3 ? 8 : section
    readonly property var gameSections: bridge.localMode ? [0] : [0, 1, 3]
    property int statusIndex: 0
    readonly property var statusKeys: ["clock", "wifi", "bluetooth", "battery", "brightness", "volume"]
    readonly property int activeDownloads: downloadQueue.jobs.filter(job => job.state === "queued" || job.state === "running").length
    readonly property int failedDownloads: downloadQueue.jobs.filter(job => job.state === "error").length
    readonly property bool updateAvailable: bridge.updatesState.available
    readonly property string downloadsLabel: qsTranslate("NGS", "Downloads · active: ") + activeDownloads + qsTranslate("NGS", " · failed: ") + failedDownloads
    property int heroAction: 0
    property int menuAction: 0
    readonly property var menuItems: ["Settings", "Minimize", "Quit"]
    property int sortAction: 0
    readonly property var sortModes: ["recent", "added", "name", "genre"]
    readonly property var sortLabels: [qsTranslate("NGS", "Recently played"), qsTranslate("NGS", "Date added"), qsTranslate("NGS", "Title", "По названию"), qsTranslate("NGS", "Genre", "По жанру")]
    readonly property var sourceGames: section === 0 ? bridge.localGames : section === 3 ? bridge.installerGames : bridge.games
    readonly property var displayGames: filterGames(sourceGames, searchQuery)
    property var filterStates: ({})
    readonly property var currentFilters: filterStates[String(section)] || ({})
    readonly property string installationFilter: section === 1 && bridge.hideInstalled ? "missing" : currentFilters.installation || "all"
    readonly property string genreFilter: currentFilters.genre || ""
    readonly property int activeFilterCount: (installationFilter !== "all" ? 1 : 0) + (genreFilter ? 1 : 0)
    readonly property var filterStatusKeys: section === 0 ? ["all", "unplayed", "unavailable"] : ["all", "installed", "missing"]
    readonly property var filterStatusLabels: section === 0 ? [qsTranslate("NGS", "All games"), qsTranslate("NGS", "Not played yet"), qsTranslate("NGS", "Unavailable")] : section === 3 ? [qsTranslate("NGS", "All packages"), qsTranslate("NGS", "Downloaded"), qsTranslate("NGS", "Not downloaded")] : [qsTranslate("NGS", "All games"), qsTranslate("NGS", "Installed"), qsTranslate("NGS", "Not installed")]
    readonly property var filterSource: section === 1 ? bridge.catalogGames : sourceGames
    readonly property var filterGenres: Array.from(new Set(filterSource.map(game => String(game.genre || "").trim()).filter(value => value.length).concat(genreFilter ? [genreFilter] : []))).sort((a, b) => a.localeCompare(b))
    readonly property bool hasGames: displayGames.length > 0
    readonly property var emptyGame: ({id: "", name: searchQuery ? qsTranslate("NGS", "Nothing found") : bridge.localMode ? qsTranslate("NGS", "My games") : qsTranslate("NGS", "Game catalog"), subtitle: searchQuery ? qsTranslate("NGS", "Try another title") : activeFilterCount ? qsTranslate("NGS", "No games match these filters") : qsTranslate("NGS", "Your next adventure starts here"),
        description: searchQuery ? qsTranslate("NGS", "No games in this section match “") + searchQuery + qsTranslate("NGS", "”. Clear the search or try another query.") : activeFilterCount ? qsTranslate("NGS", "Reset the filters to see all games.") : bridge.localMode ? qsTranslate("NGS", "Add a game folder or scan installed store games. Your library and playtime stay on this device. No server or account is required.") : qsTranslate("NGS", "Published games appear after connecting to your server. The cached catalog is available offline."),
        genre: "", accent: uiTheme.themeColors.accent, image: "", backgroundImage: ""})
    property var selected: displayGames[selectedIndex] || emptyGame
    readonly property bool actionEnabled: !bridge.operationBusy || (!selectedInstalled && !installerReady && bridge.downloadActive)
    readonly property bool selectedQueued: bridge.downloadIds.indexOf(selected.id) >= 0
    readonly property bool selectedInstaller: section === 3 && hasGames
    readonly property bool installerReady: selectedInstaller && bridge.installerIds.indexOf(selected.id) >= 0
    readonly property string primaryActionText: bridge.frozenIds.indexOf(selected.id) >= 0 || bridge.runningIds.indexOf(selected.id) >= 0 ? qsTranslate("NGS", "Resume") : selectedInstaller ? installerReady ? qsTranslate("NGS", "Install") : selectedQueued ? qsTranslate("NGS", "In downloads") : qsTranslate("NGS", "Download package") : selectedInstalled ? qsTranslate("NGS", "Play") : selectedQueued ? qsTranslate("NGS", "In downloads") : qsTranslate("NGS", "Download", "Копировать")
    readonly property bool selectedInstalled: bridge.installedIds.indexOf(selected.id) >= 0
    readonly property bool selectedRemoving: bridge.removingIds.indexOf(selected.id) >= 0
    readonly property bool selectedExternal: bridge.externalIds.indexOf(selected.id) >= 0
    readonly property bool selectedUnavailable: bridge.unavailableIds.indexOf(selected.id) >= 0
    readonly property bool selectedRemovable: selectedExternal || bridge.removableIds.indexOf(selected.id) >= 0
    readonly property bool canAct: hasGames && !selectedRemoving && !selectedUnavailable && (installerReady || selectedInstalled || !!selected.manifest || selected.delivery === "on_request")
    property int detailsAction: 0
    property bool detailsMore: false
    property bool detailsResetConfirm: false
    property string detailsResetId: ""
    property var detailsStatistics: ({total_seconds: 0, launches: 0, longest_seconds: 0, status: ""})
    function duration(seconds) { const n = Math.max(0, Math.floor(seconds || 0)); return Math.floor(n / 3600) + ":" + String(Math.floor(n / 60) % 60).padStart(2, "0") + ":" + String(n % 60).padStart(2, "0") }
    readonly property var detailsButtons: [detailsPrimary, detailsBack, detailsRemove, detailsReveal, metadataButton, optionsButton, detailsAdd, detailsScroll, detailsMoreButton, detailsResetButton, detailsResetCancel]
    readonly property bool detailsLaunchSelected: detailsAction === 0
    property string removeId: ""
    property string removeName: ""
    property string removeLocation: ""
    property int removeAction: 0
    readonly property bool removeSelected: removeAction > 0
    property bool removeExternal: false
    property bool removeInProgress: false
    property color accent: uiTheme.themeColors.accent
    property var heroGame: displayGames[bridge.initialUi.game_index] || emptyGame
    property real heroOpacity: 1
    property real heroReveal: 1
    property real intro: 1
    property bool motionReady: false
    property bool gameInProgress: false
    property bool frozenLibraryVisible: false
    property bool launchPending: false
    onGameInProgressChanged: {
        if (!gameInProgress) { launchMotion.stop(); launchPending = false }
        else if (!launchMotion.running) launchPending = false
    }
    readonly property bool modalVisible: (playerAccountPane.visible && playerAccountPane.enabled) || (statusPanel.visible && statusPanel.enabled) || (details.visible && details.enabled) || (exitDialog.visible && exitDialog.enabled) || (removeDialog.visible && removeDialog.enabled) || (localScanner.visible && localScanner.enabled) || (settingsPane.visible && settingsPane.enabled) || (metadataPicker.visible && metadataPicker.enabled) || (installDestination.visible && installDestination.enabled) || (updatesPane.visible && updatesPane.enabled) || (librarySortMenu.visible && librarySortMenu.enabled) || (gameFiltersPane.visible && gameFiltersPane.enabled) || (launchOptions.visible && launchOptions.enabled) || (bindingsPane.visible && bindingsPane.enabled) || (gameSearch.visible && gameSearch.enabled) || (padKeyboardEditor.visible && padKeyboardEditor.enabled) || (keyboardLayoutEditor.visible && keyboardLayoutEditor.enabled) || catalogizerPane.editing
    readonly property string selectionHelp: {
        if (modalVisible || (catalogizerPane.visible && catalogizerPane.enabled) || frozenScreen.visible) return ""
        const accept = hints.activate + " — "
        if (focusZone === 0) {
            if (navigationIndex === 0) return qsTranslate("NGS", "My games section") + (section === 0 ? qsTranslate("NGS", " · selected", " · выбран") : " · " + accept + qsTranslate("NGS", "open", "перейти"))
            if (navigationIndex === 1) return qsTranslate("NGS", "Catalog section") + (section === 1 ? qsTranslate("NGS", " · selected", " · выбран") : " · " + accept + qsTranslate("NGS", "open", "перейти"))
            if (navigationIndex === 8) return qsTranslate("NGS", "Installers section") + (section === 3 ? qsTranslate("NGS", " · selected", " · выбран") : " · " + accept + qsTranslate("NGS", "open", "перейти"))
            if (navigationIndex === 2) return downloadsLabel + " · " + accept + qsTranslate("NGS", "open", "открыть")
            if (navigationIndex === 9) return qsTranslate("NGS", "Search games") + (searchQuery ? ": «" + searchQuery + "»" : "") + " · " + accept + qsTranslate("NGS", "enter a title")
            if (navigationIndex === 10) return qsTranslate("NGS", "Library view: ") + (gridMode ? qsTranslate("NGS", "grid") : qsTranslate("NGS", "ribbon")) + " · " + accept + qsTranslate("NGS", "switch")
            if (navigationIndex === 3) return (section === 0 ? qsTranslate("NGS", "Add a game from this device") : section === 3 ? qsTranslate("NGS", "Installer catalog manager") : qsTranslate("NGS", "Game catalog manager")) + " · " + accept + qsTranslate("NGS", "open", "открыть")
            if (section !== 2 && navigationIndex === 6)
                return qsTranslate("NGS", "Game filters") + (activeFilterCount ? ": " + activeFilterCount : "") + " · " + accept + qsTranslate("NGS", "open", "открыть")
            if (section === 0 && navigationIndex === 7)
                return qsTranslate("NGS", "Sort games: ") + sortLabels[sortModes.indexOf(bridge.librarySort)] + " · " + accept + qsTranslate("NGS", "change order")
            if (navigationIndex === 5) return (updateAvailable ? qsTranslate("NGS", "Settings: NGS update available") : qsTranslate("NGS", "Settings")) + " · " + accept + qsTranslate("NGS", "open", "открыть")
            if (navigationIndex === 4) return qsTranslate("NGS", "Minimize NGS to the tray · ") + accept + qsTranslate("NGS", "hide window")
        }
        if (focusZone === -1) {
            const labels = {clock: qsTranslate("NGS", "Date and time"), wifi: qsTranslate("NGS", "Wi-Fi connection"), bluetooth: qsTranslate("NGS", "Bluetooth devices"),
                battery: qsTranslate("NGS", "Battery and power"), brightness: qsTranslate("NGS", "Screen brightness"), volume: qsTranslate("NGS", "Volume and audio")}
            return labels[statusKeys[statusIndex]] + " · " + systemLabel(statusKeys[statusIndex]) + " · " + accept + qsTranslate("NGS", "open NGS panel")
        }
        if (section !== 2 && searchQuery) return qsTranslate("NGS", "Search: “") + searchQuery + qsTranslate("NGS", "” · Found: ") + displayGames.length + " · " + hints.back + qsTranslate("NGS", " — clear search")
        return ""
    }
    readonly property real uiScale: Math.min(width / 1440, height / 900) * bridge.settingsState.ui_scale
    readonly property real animScale: bridge.animationScale !== undefined ? bridge.animationScale : 1.0
    readonly property real toolScale: Math.max(.85, uiScale)
    property string clock: Qt.formatTime(new Date(), "hh:mm")

    onSectionChanged: {
        if ((section === 2 && navigationIndex === 6) || (section !== 0 && navigationIndex === 7) || (section === 2 && navigationIndex === 3)) navigationIndex = section === 3 ? 8 : section
        if (selectionReady) restoreSelection(true)
    }
    onSelectedIndexChanged: {
        if (selectionReady) {
            revealGridSelection = true
            rememberSelection()
            Qt.callLater(keepSelectionVisible)
        }
    }
    onGridModeChanged: {
        revealGridSelection = true
        const state = bridge.sectionUi(section)
        pendingScroll = state.scroll_x; pendingGridScroll = state.scroll_y
        if (gridMode && focusZone === 1 && section !== 2) focusZone = 2
        if (gridMode) { heroTransition.stop(); heroGame = selected; heroOpacity = 1; heroReveal = 1 }
        Qt.callLater(keepSelectionVisible)
    }
    onSelectedChanged: {
        if (gridMode || animScale <= 0.01) { heroTransition.stop(); heroGame = selected; heroOpacity = 1; heroReveal = 1 }
        else if (motionReady && !gameInProgress) heroTransition.restart()
    }
    Behavior on accent { enabled: !window.gameInProgress; ColorAnimation { duration: Math.round(360 * window.animScale); easing.type: Easing.InOutQuad } }
    Component.onCompleted: {
        selectionReady = true
        restoreSelection(true)
        heroGame = selected
        motionReady = true
    }
    NumberAnimation { id: arrival; target: window; property: "intro"; from: 1; to: 1; duration: 0 }
    SequentialAnimation {
        id: heroTransition
        NumberAnimation { target: window; property: "heroOpacity"; to: 0; duration: Math.round(90 * window.animScale); easing.type: Easing.OutQuad }
        ScriptAction { script: { window.heroGame = window.selected; window.heroReveal = 0 } }
        ParallelAnimation {
            NumberAnimation { target: window; property: "heroOpacity"; to: 1; duration: Math.round(240 * window.animScale); easing.type: Easing.OutQuad }
            NumberAnimation { target: window; property: "heroReveal"; to: 1; duration: Math.round(430 * window.animScale); easing.type: Easing.OutCubic }
        }
    }

    function restoreSelection(preserveViewport = false) {
        const state = bridge.sectionUi(section)
        const collection = section === 0 ? bridge.localGames : section === 3 ? bridge.installerGames : bridge.games
        const games = filterGames(collection, searchQueries[String(section)] || "")
        const ready = selectionReady
        selectionReady = false
        pendingScroll = state.scroll_x
        pendingGridScroll = state.scroll_y
        const index = games.findIndex(game => game.id === state.game_id)
        selectedIndex = index >= 0 ? index : Math.max(0, Math.min(state.game_index, games.length - 1))
        const game = games[selectedIndex]
        bridge.rememberUi(section, game ? collection.findIndex(entry => entry.id === game.id) : -1)
        revealGridSelection = !preserveViewport
        selectionReady = ready
        Qt.callLater(keepSelectionVisible)
    }
    function rememberSelection() {
        if (section === 2) { bridge.rememberUi(section, 0); return }
        const game = displayGames[selectedIndex]
        if (game) bridge.rememberUi(section, sourceGames.findIndex(entry => entry.id === game.id))
        else bridge.rememberUi(section, searchQuery ? -1 : 0)
    }
    function keepSelectionVisible() { if (gridMode) gameGrid.keepSelectionVisible(); else cards.keepSelectionVisible() }
    function selectAddedGame(targetSection, identity) {
        const collection = targetSection === 0 ? bridge.localGames : targetSection === 3 ? bridge.installerGames : bridge.games
        const index = collection.findIndex(game => game.id === identity)
        if (index < 0) return
        selectionReady = false
        const queries = Object.assign({}, searchQueries)
        delete queries[String(targetSection)]
        searchQueries = queries
        const filters = Object.assign({}, filterStates)
        delete filters[String(targetSection)]
        filterStates = filters
        if (targetSection === 1) bridge.setHideInstalled(false)
        changeSection(targetSection)
        selectedIndex = index
        selectionReady = true
        pendingScroll = -1; pendingGridScroll = -1
        revealGridSelection = true
        rememberSelection()
        Qt.callLater(keepSelectionVisible)
    }
    function searchWords(text) {
        return String(text || "").toLowerCase().replace(/[\s_\-–—:'’"™®.,!?()[\]\/\\]+/g, " ").trim().split(" ").filter(word => word.length)
    }
    function filterGames(collection, query, mode = installationFilter, genre = genreFilter) {
        const words = searchWords(query)
        if (!words.length && mode === "all" && !genre) return collection
        const installed = new Set(section === 3 ? bridge.installerIds : bridge.installedIds)
        const played = new Set(bridge.playedIds), unavailable = new Set(bridge.unavailableIds)
        return collection.filter(game => {
            if (genre && String(game.genre || "").trim() !== genre) return false
            if (mode === "installed" && !installed.has(game.id)) return false
            if (mode === "missing" && installed.has(game.id)) return false
            if (mode === "unplayed" && played.has(game.id)) return false
            if (mode === "unavailable" && !unavailable.has(game.id)) return false
            const name = searchWords(game.name).join(" ")
            return words.every(word => name.indexOf(word) >= 0)
        })
    }
    function applyFilters(mode, genre) {
        selectionReady = false
        if (section === 1) bridge.setHideInstalled(mode === "missing")
        const next = Object.assign({}, filterStates)
        next[String(section)] = {installation: section === 1 && mode === "missing" ? "all" : mode, genre: genre}
        filterStates = next
        restoreSelection()
        selectionReady = true
    }
    function openFilters() { gameFiltersPane.openFilters(installationFilter, genreFilter) }
    function applySearch(query) {
        selectionReady = false
        const next = Object.assign({}, searchQueries)
        next[String(section)] = query.trim().slice(0, 160)
        searchQueries = next
        restoreSelection()
        selectionReady = true
        if (section !== 2) focusZone = 2
    }
    function openSearch() { if (section !== 2) { gameSearch.query = searchQuery; gameSearch.open() } }
    function changeSection(next) {
        if (bridge.localMode && (next === 1 || next === 3)) next = 0
        section = (next + 4) % 4
        navigationIndex = section === 3 ? 8 : section
        focusZone = gridMode && section !== 2 ? 2 : section === 1 || (section !== 2 && hasGames) ? 2 : 1
    }
    function moveSection(delta) {
        const index = gameSections.indexOf(section)
        changeSection(index < 0 ? (delta > 0 ? 3 : 1) : gameSections[(index + delta + gameSections.length) % gameSections.length])
    }
    function headerControls() {
        return statusKeys.map((key, i) => ({item: statusButtons.itemAt(i), zone: -1, index: i}))
            .concat([{item: searchButton, zone: 0, index: 9}, {item: viewButton, zone: 0, index: 10},
                {item: downloadsButton, zone: 0, index: 2}, {item: minimizeButton, zone: 0, index: 4}, {item: menuButton, zone: 0, index: 5}])
            .filter(entry => entry.item && entry.item.visible && entry.item.enabled)
    }
    function selectHeader(entry) {
        focusZone = entry.zone
        if (entry.zone === -1) statusIndex = entry.index; else navigationIndex = entry.index
    }
    function moveControl(delta) {
        const entries = headerControls()
        const i = entries.findIndex(entry => entry.zone === focusZone && entry.index === (focusZone === -1 ? statusIndex : navigationIndex))
        selectHeader(entries[(i < 0 ? delta > 0 ? 0 : entries.length - 1 : i + delta + entries.length) % entries.length])
    }
    function moveHeader(action) {
        const entries = headerControls().concat([0, 1, 2].map(i => ({item: sectionButtons.itemAt(i), zone: 0, index: i === 2 ? 8 : i})))
            .concat([{item: librarySortButton, zone: 0, index: 7}, {item: installedFilter, zone: 0, index: 6}, {item: addGameButton, zone: 0, index: 3}])
            .filter(entry => entry.item && entry.item.visible && entry.item.enabled)
        const i = entries.findIndex(entry => entry.zone === focusZone && entry.index === (focusZone === -1 ? statusIndex : navigationIndex))
        const target = Navigation.next(entries.map(entry => entry.item), i, action, scene)
        if (target !== i && target >= 0) { selectHeader(entries[target]); return true }
        return false
    }
    function openMenu() { menuAction = 0; exitDialog.open() }
    function menuLabel(item) {
        return ({Settings: qsTranslate("NGS", "Settings · ") + hints.settings, Minimize: qsTranslate("NGS", "Minimize · ") + hints.minimize,
            Quit: qsTranslate("NGS", "Exit NGS") + (menuItems[menuAction] === "Quit" ? " · " + hints.activate : "")})[item]
    }
    function activateMenu(item) {
        exitDialog.close()
        if (item === "Settings") settingsPane.open()
        else if (item === "Minimize") bridge.minimize()
        else if (item === "Quit") bridge.quit()
    }
    function openDetails() {
        if (hasGames) { detailsMore = false; detailsResetConfirm = false; detailsAction = canAct && actionEnabled ? 0 : 8; detailsStatistics = selectedInstaller ? ({}) : bridge.gameStatistics(selected.id); details.open() }
        else if (searchQuery) applySearch("")
        else if (activeFilterCount) applyFilters("all", "")
        else bridge.refreshCatalog()
    }
    function openEmptyLibrary() {
        if (bridge.localMode) { changeSection(0); localScanner.openScanner() }
        else changeSection(1)
    }
    function openLaunchOptions() {
        const identity = selected.id, name = selected.name
        details.close()
        launchOptions.openGame(identity, name)
    }
    function runSelected() {
        if (!canAct || !actionEnabled || launchPending) return
        if (bridge.frozenIds.indexOf(selected.id) >= 0 || bridge.runningIds.indexOf(selected.id) >= 0) { details.close(); bridge.returnToGame(); return }
        if (selectedQueued && !selectedInstalled) { details.close(); changeSection(2); return }
        const fromDetails = details.visible
        details.close()
        if (selectedInstalled && !selectedInstaller) {
            if (launchMotion.running) return
            launchTile.card = selected
            launchTile.identity = selected.id; launchTile.source = selected.image || "../assets/local.svg"
            const card = gridMode ? gameGrid.currentItem : cards.currentItem
            const poster = fromDetails ? detailsPresentation.posterItem : card ? card.posterItem : null
            const origin = poster ? poster.mapToItem(window.contentItem, 0, 0) : Qt.point(width / 2 - 100, height / 2 - 150)
            const corner = poster ? poster.mapToItem(window.contentItem, poster.width, poster.height) : Qt.point(origin.x + 200, origin.y + 300)
            launchTile.startX = origin.x; launchTile.startY = origin.y
            launchTile.startWidth = corner.x - origin.x
            bridge.performGameAction(launchTile.identity)
        } else if (selectedInstaller) bridge.performInstallerAction(selected.id)
        else bridge.performGameAction(selected.id)
    }
    function addInstalledGame() {
        const identity = selected.id
        details.close()
        localScanner.openScanner(identity)
    }
    function openCatalogizer(installers) {
        if (bridge.localMode) { playerAccountPane.open(); return }
        catalogizerPane.bridge = installers ? window.bridge.installerCatalogizer : window.bridge.remoteCatalogizer
        catalogizerPane.open()
        catalogizerPane.bridge.openServer(window.bridge.settingsState.tactic_url)
    }
    function openRemove() {
        if (!selectedRemovable || bridge.operationBusy) return
        removeId = selected.id
        removeName = selected.name
        removeExternal = selectedExternal
        removeInProgress = selectedRemoving
        removeLocation = bridge.installLocation(removeId)
        removeAction = 0
        details.close()
        removeDialog.open()
    }
    function confirmRemove(deleteFiles) {
        if (deleteFiles) bridge.removeGame(removeId)
        else bridge.forgetGame(removeId)
        removeDialog.close()
        if (deleteFiles) changeSection(2)
    }
    function systemLabel(key) {
        const state = bridge.systemState
        if (key === "clock") return clock
        if (key === "wifi" || key === "bluetooth") {
            const labels = {on: qsTranslate("NGS", "on", "вкл."), off: qsTranslate("NGS", "off", "выкл."), absent: qsTranslate("NGS", "none"), unknown: "?"}
            return (key === "wifi" ? "Wi-Fi: " : "Bluetooth: ") + (labels[state[key]] || "?")
        }
        if (key === "brightness") return qsTranslate("NGS", "Screen brightness")
        if (key === "volume") return state.muted ? qsTranslate("NGS", "Audio: muted") : qsTranslate("NGS", "Audio: ") + (state.volume >= 0 ? state.volume + "%" : "?")
        if (!state.has_battery) return qsTranslate("NGS", "Power: plugged in")
        return qsTranslate("NGS", "Battery: ") + (state.battery >= 0 ? state.battery + "%" : "?") + (state.charging ? " +" : state.plugged ? qsTranslate("NGS", " · plugged in") : "")
    }
    function showSystemPanel(key) {
        const button = statusButtons.itemAt(statusKeys.indexOf(key))
        if (button) {
            const point = button.mapToItem(statusPanel.parent, button.width / 2, button.height + 8)
            statusPanel.anchorX = point.x; statusPanel.anchorY = point.y
        }
        statusPanel.showPanel(key)
    }
    function handleAction(action) {
        if (playerAccountPane.visible && playerAccountPane.enabled) { playerAccountPane.handleAction(action); return }
        if (frozenScreen.visible) {
            frozenDashboard.handleAction(action)
            return
        }
        if (keyboardLayoutEditor.visible && keyboardLayoutEditor.enabled) { keyboardLayoutEditor.handleAction(action); return }
        if ((padKeyboardEditor.visible && padKeyboardEditor.enabled)) { padKeyboardEditor.handleAction(action); return }
        if (launchPending) { if (action === "back") bridge.cancelOperation(); return }
        if (gameInProgress || bridge.launchingGame) { runningDashboard.handleAction(action); return }
        if (statusPanel.visible && statusPanel.enabled) { statusPanel.handleAction(action); return }
        if (gameFiltersPane.visible && gameFiltersPane.enabled) { gameFiltersPane.handleAction(action); return }
        if ((librarySortMenu.visible && librarySortMenu.enabled)) {
            if (action === "up" || action === "left") sortAction = (sortAction + sortModes.length) % (sortModes.length + 1)
            else if (action === "down" || action === "right") sortAction = (sortAction + 1) % (sortModes.length + 1)
            else if (action === "activate") { if (sortAction === sortModes.length || bridge.setLibrarySort(sortModes[sortAction])) librarySortMenu.close() }
            else if (action === "back" || action === "menu") librarySortMenu.close()
            return
        }
        if ((catalogizerPane.visible && catalogizerPane.enabled)) { catalogizerPane.handleAction(action); return }
        if ((gameSearch.visible && gameSearch.enabled)) { gameSearch.handleAction(action); return }
        if (action === "minimize") { exitDialog.close(); bridge.minimize(); return }
        if ((bindingsPane.visible && bindingsPane.enabled)) { bindingsPane.handleAction(action); return }
        if ((updatesPane.visible && updatesPane.enabled)) { updatesPane.handleAction(action); return }
        if ((installDestination.visible && installDestination.enabled)) { installDestination.handleAction(action); return }
        if ((settingsPane.visible && settingsPane.enabled)) { settingsPane.handleAction(action); return }
        if (bridge.loginRequired) {
            if (action === "up" || action === "down" || action === "left" || action === "right")
                loginGate.selectedAction = (loginGate.selectedAction + (action === "up" || action === "left" ? 2 : 1)) % 3
            else if (action === "settings" || action === "menu") settingsPane.open()
            else if (action === "activate") {
                if (loginGate.selectedAction === 1) settingsPane.open()
                else if (loginGate.selectedAction === 2) bridge.useLocalLibrary()
                else playerAccountPane.open()
            } else if (action === "back") bridge.quit()
            return
        }
        if ((metadataPicker.visible && metadataPicker.enabled)) { metadataPicker.handleAction(action); return }
        if ((launchOptions.visible && launchOptions.enabled)) { launchOptions.handleAction(action); return }
        if (action === "settings") { exitDialog.close(); settingsPane.open(); return }
        if (action === "downloads") { exitDialog.close(); details.close(); changeSection(2); return }
        if ((localScanner.visible && localScanner.enabled)) { localScanner.handleAction(action); return }
        if ((removeDialog.visible && removeDialog.enabled)) {
            const count = removeExternal || removeInProgress ? 2 : 3
            if (action === "left" || action === "up") removeAction = (removeAction + count - 1) % count
            else if (action === "right" || action === "down") removeAction = (removeAction + 1) % count
            else if (action === "back" || action === "menu") removeDialog.close()
            else if (action === "activate") { if (removeSelected) confirmRemove(removeInProgress || removeAction === 2); else removeDialog.close() }
            return
        }
        if ((exitDialog.visible && exitDialog.enabled)) {
            if (action === "toggle_installed") { exitDialog.close(); bridge.minimize(); return }
            if (action === "refresh") { exitDialog.close(); settingsPane.open(); return }
            if (["left", "right", "up", "down"].indexOf(action) >= 0)
                menuAction = (menuAction + (action === "left" || action === "up" ? menuItems.length - 1 : 1)) % menuItems.length
            else if (action === "back" || action === "menu")
                exitDialog.close()
            else if (action === "activate") activateMenu(menuItems[menuAction])
            return
        }
        if ((details.visible && details.enabled)) {
            if (action === "refresh" && selectedExternal) { details.close(); bridge.findMetadata(selected.id); return }
            if (["back", "menu"].indexOf(action) >= 0) details.close()
            else if (action === "activate") {
                if (detailsLaunchSelected && canAct) runSelected()
                else if (detailsAction === 2) openRemove()
                else if (detailsAction === 3) bridge.revealGame(selected.id)
                else if (detailsAction === 4) { details.close(); bridge.findMetadata(selected.id) }
                else if (detailsAction === 5) openLaunchOptions()
                else if (detailsAction === 6) addInstalledGame()
                else if (detailsAction === 8) { detailsMore = !detailsMore; detailsAction = detailsMore && optionsButton.visible ? 5 : 8 }
                else if (detailsAction === 9) detailsResetButton.clicked()
                else if (detailsAction === 10) detailsResetCancel.clicked()
                else if (detailsAction !== 7) details.close()
            } else if (["left", "right", "up", "down"].indexOf(action) >= 0) {
                if (detailsAction === 7 && (action === "up" || action === "down")) {
                    const view = detailsScroll.contentItem
                    const nextY = Math.max(0, Math.min(Math.max(0, view.contentHeight - view.height), view.contentY + (action === "down" ? 60 : -60) * uiScale))
                    if (nextY !== view.contentY) { view.contentY = nextY; return }
                }
                const next = Navigation.next(detailsButtons, detailsAction, action, details.contentItem)
                if (next !== detailsAction) detailsAction = next
                else if (action === "up" || action === "down") {
                    const view = detailsScroll.contentItem
                    view.contentY = Math.max(0, Math.min(Math.max(0, view.contentHeight - view.height), view.contentY + (action === "down" ? 60 : -60) * uiScale))
                }
            }
            return
        }
        if (action === "previous_control" || action === "next_control") { moveControl(action === "next_control" ? 1 : -1); return }
        if (section === 2 && focusZone > 0 && downloadQueue.handleAction(action)) return
        if (focusZone <= 0 && ["left", "right", "up", "down"].indexOf(action) >= 0) {
            if (moveHeader(action) || action !== "down") return
            focusZone = gridMode && section !== 2 ? 2 : 1; return
        }
        if (action === "search") { openSearch(); return }
        if (gridMode && section !== 2 && focusZone === 2 && (action === "up" || action === "down")) {
            if (action === "up" && selectedIndex < gameGrid.columns) { focusZone = 0; navigationIndex = section === 3 ? 8 : section }
            else if (hasGames) {
                const lastRow = Math.floor((displayGames.length - 1) / gameGrid.columns)
                selectedIndex = action === "down" && Math.floor(selectedIndex / gameGrid.columns) === lastRow
                    ? selectedIndex % gameGrid.columns
                    : Math.max(0, Math.min(displayGames.length - 1, selectedIndex + (action === "up" ? -gameGrid.columns : gameGrid.columns)))
            }
            return
        }
        if (action === "toggle_installed" && section === 0) localScanner.openScanner()
        else if (action === "toggle_installed" && (section === 1 || section === 3)) openCatalogizer(section === 3)
        else if (action === "refresh") { if (section === 0) bridge.refreshLocalLibrary(); else if (section === 3) bridge.refreshInstallers(); else bridge.refreshCatalog() }
        else if (action === "menu") {
            if (hasGames && section !== 2) openDetails(); else openMenu()
        } else if (action === "next_section") moveSection(1)
        else if (action === "previous_section") moveSection(-1)
        else if (action === "back") {
            if (searchQuery && focusZone > 0) applySearch("")
            else if (focusZone <= 0) openMenu()
            else focusZone = 0
        } else if (action === "up") { focusZone = Math.max(-1, focusZone - 1); if (focusZone === 0) navigationIndex = section === 3 ? 8 : section }
        else if (action === "down") focusZone = gridMode && section !== 2 ? 2 : Math.min(section === 1 || (section !== 2 && hasGames) ? 2 : 1, focusZone + 1)
        else if (action === "left" || action === "right") {
            const delta = action === "right" ? 1 : -1
            if (focusZone === -1) statusIndex = (statusIndex + delta + statusKeys.length) % statusKeys.length
            else if (focusZone === 0) {
                const actions = section === 0 ? [0, 1, 8, 3, 7, 6, 9, 10, 2, 4, 5] : section === 1 ? [0, 1, 6, 8, 9, 10, 3, 2, 4, 5] : section === 3 ? [0, 1, 8, 9, 10, 3, 6, 2, 4, 5] : [0, 1, 8, 2, 4, 5]
                navigationIndex = actions[(Math.max(0, actions.indexOf(navigationIndex)) + delta + actions.length) % actions.length]
            }
            else if (focusZone === 1 && section !== 2) {
                const actions = (canAct ? [0] : []).concat(hasGames || searchQuery || activeFilterCount ? [1] : []).concat(installerReady ? [3] : [])
                if (actions.length) heroAction = actions[(Math.max(0, actions.indexOf(heroAction)) + delta + actions.length) % actions.length]
                else focusZone = 0
            }
            else if (section !== 2 && focusZone === 2 && hasGames) {
                selectedIndex = (selectedIndex + delta + displayGames.length) % displayGames.length
            }
        } else if (action === "activate") {
            if (focusZone === -1) showSystemPanel(statusKeys[statusIndex])
            else if (focusZone === 0) {
                if (navigationIndex < 3) { changeSection(navigationIndex); focusZone = 0 }
                else if (navigationIndex === 8) { changeSection(3); focusZone = 0 }
                else if (navigationIndex === 9) openSearch()
                else if (navigationIndex === 10) bridge.setGamesView(gridMode ? "ribbon" : "grid")
                else if (navigationIndex === 3) { if (section === 0) localScanner.openScanner(); else openCatalogizer(section === 3) }
                else if (navigationIndex === 4) bridge.minimize()
                else if (navigationIndex === 5) settingsPane.open()
                else if (navigationIndex === 6) openFilters()
                else librarySortMenu.open()
            }
            else if (section === 2 && bridge.operationBusy) bridge.cancelOperation()
            else if (section === 2) changeSection(bridge.installedIds.length ? 0 : 1)
            else if (section === 3 && !hasGames) openCatalogizer(true)
            else if (section === 0 && !hasGames) openEmptyLibrary()
            else if (focusZone === 1) {
                if (heroAction === 2 && selectedRemovable) openRemove()
                else if (heroAction === 0 && canAct) runSelected()
                else if (heroAction === 3 && installerReady) addInstalledGame()
                else openDetails()
            }
            else runSelected()
        }
    }

    Connections {
        target: window.bridge
        function onAction(action) { window.handleAction(action) }
        function onGameLibraryRequested() { window.frozenLibraryVisible = true }
        function onGamePanelRequested() {
            window.frozenLibraryVisible = false
            if (window.gameInProgress && window.launchPending) {
                launchMotion.stop(); launchTile.progress = 2; window.launchPending = false
            }
        }
        function onGameLaunchRequested(identity) {
            if (launchTile.identity !== identity) {
                launchTile.card = window.bridge.localGames.find(card => card.id === identity)
                    || window.bridge.games.find(card => card.id === identity) || {}
                launchTile.identity = identity
                launchTile.source = launchTile.card.image || "../assets/local.svg"
                launchTile.startWidth = 200
                launchTile.startX = window.width / 2 - 100
                launchTile.startY = window.height / 2 - 150
            }
            window.frozenLibraryVisible = false
            launchTile.progress = 0
            window.launchPending = true
            launchMotion.restart()
        }
        function onGameStarted() { window.frozenLibraryVisible = false }
        function onGameReturned() { window.frozenLibraryVisible = false }
        function onOperationChanged() {
            if (window.launchPending && !window.bridge.launchingGame && !window.gameInProgress) {
                launchMotion.stop(); window.launchPending = false
            }
        }
        function onInstallRequested(identity, name) { installDestination.choose(identity, name) }
        function onDownloadEnqueued() { installDestination.close(); window.changeSection(2) }
        function onGameAdded(targetSection, identity) { window.selectAddedGame(targetSection, identity) }
        function onGamesChanged() {
            if (window.section === 1) {
                details.close()
                window.restoreSelection()
                if (window.motionReady && !window.gameInProgress && !window.gridMode) heroTransition.restart()
            }
        }
        function onLibraryChanged() {
            if (window.section === 0) {
                window.restoreSelection()
                if (window.motionReady && !window.gameInProgress && !window.gridMode) heroTransition.restart()
            }
        }
        function onInstallersChanged() {
            if (window.section === 3) {
                window.restoreSelection()
                if (window.motionReady && !window.gameInProgress && !window.gridMode) heroTransition.restart()
            }
        }
    }
    Timer {
        objectName: "systemStatusTimer"
        interval: 30000
        running: window.visible && window.active && window.visibility !== Window.Minimized && !window.gameInProgress
        repeat: true
        triggeredOnStart: true
        onTriggered: window.bridge.refreshSystemStatus()
    }
    Timer {
        objectName: "clockMinuteTimer"
        interval: 60000 - (Date.now() % 60000)
        running: window.visible && window.active && window.visibility !== Window.Minimized && !window.gameInProgress
        repeat: true
        triggeredOnStart: true
        onTriggered: {
            window.clock = Qt.formatTime(new Date(), "hh:mm")
            interval = 60000 - (Date.now() % 60000)
        }
    }

    component ActionButton: FluidButton {
        id: control
        property bool filled: false
        focusPolicy: Qt.NoFocus
        implicitHeight: Math.min(58, scene.height * .064)
        implicitWidth: 210
        font.pixelSize: 17
        font.weight: Font.DemiBold
        background: FluidSurface {
            radius: height / 2
            color: control.down ? uiTheme.themeColors.hover : control.filled ? (control.highlighted ? Qt.darker(window.accent, 1.1) : window.accent) : control.highlighted ? uiTheme.themeColors.soft : uiTheme.themeColors.button
            border.width: 0
            Behavior on color { ColorAnimation { duration: 180 } }
            Behavior on border.color { ColorAnimation { duration: 200 } }
        }
        contentItem: Text {
            text: control.text
            font: control.font
            color: control.filled ? uiTheme.themeColors.accentText : uiTheme.themeColors.text
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }

    component HeaderButton: FluidButton {
        id: control
        focusPolicy: Qt.NoFocus
        background: FluidSurface {
            radius: height / 2
            color: control.down ? uiTheme.themeColors.hover : control.highlighted ? uiTheme.themeColors.soft : "transparent"
            border.width: 0
        }
        contentItem: Text { text: control.text; color: uiTheme.themeColors.text; font.pixelSize: 14; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
    }

    component GameCard: ItemDelegate {
        id: card
        required property var modelData
        required property int index
        property bool chosen: window.selectedIndex === index && window.focusZone === 2 && !window.modalVisible && !catalogizerPane.visible
        readonly property real posterHeight: Math.floor(height - 58)
        readonly property real posterWidth: Math.floor(posterHeight * 2 / 3)
        property alias posterItem: posterFace
        property color glowColor: window.accent
        width: posterWidth + 32
        padding: 6
        focusPolicy: Qt.NoFocus
        Accessible.name: modelData.name
        background: Item {}
        contentItem: Item {
            FluidSurface {
                id: posterFace
                objectName: "cardPoster-" + card.index
                radius: 6
                pressed: card.down; focused: card.chosen; hovered: card.hovered
                shadowEnabled: false
                elevation: card.chosen ? 1 : 0
                property real squeeze: card.down ? 1 : 0
                Behavior on squeeze { NumberAnimation { duration: Math.round((card.down ? 75 : 360) * window.animScale); easing.type: card.down ? Easing.OutQuad : Easing.OutBack; easing.overshoot: .65 } }
                transform: Scale { origin.x: posterFace.width / 2; origin.y: posterFace.height / 2; xScale: 1 + posterFace.squeeze * .012; yScale: 1 - posterFace.squeeze * .04 }
                x: 10; y: card.chosen ? 8 : 16
                width: card.posterWidth; height: card.posterHeight
                scale: card.chosen ? 1 : 0.96
                Behavior on scale { NumberAnimation { duration: Math.round(400 * window.animScale); easing.type: Easing.OutBack; easing.overshoot: .65 } }
                color: "transparent"
                Behavior on y { NumberAnimation { duration: Math.round(340 * window.animScale); easing.type: Easing.OutBack; easing.overshoot: .65 } }
                Rectangle {
                    objectName: "cardGlow-" + card.index
                    anchors.fill: parent; anchors.margins: -4
                    z: -1; radius: posterFace.radius + 4; color: "transparent"
                    border.width: 2; border.color: card.glowColor
                    opacity: card.chosen ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Math.round(220 * window.animScale); easing.type: Easing.OutCubic } }
                    RectangularShadow {
                        anchors.fill: parent; z: -1; radius: parent.radius
                        blur: 12; color: card.glowColor; opacity: .45
                        visible: card.chosen; cached: true
                    }
                }
                Item {
                    id: posterContents
                    objectName: "cardPosterContents-" + card.index
                    anchors.fill: parent
                    // shortcut: software keeps square artwork; add a painted mask if software rounding is required.
                    layer.enabled: GraphicsInfo.api !== GraphicsInfo.Software
                    layer.smooth: true
                    layer.effect: MultiEffect {
                        maskEnabled: true
                        maskThresholdMin: .5; maskSpreadAtMin: 1
                        maskSource: Rectangle {
                            parent: posterFace
                            width: posterContents.width; height: posterContents.height
                            radius: posterFace.radius; color: "white"
                            layer.enabled: true; layer.smooth: true; visible: false
                        }
                    }
                    Image {
                        objectName: "cardImage-" + card.index
                        anchors.fill: parent
                        source: card.modelData.image; fillMode: Image.PreserveAspectCrop
                        sourceSize.width: 240; sourceSize.height: 360
                        asynchronous: true
                    }
                    Rectangle {
                        objectName: "cardDim-" + card.index
                        anchors.fill: parent
                        color: "#09101c"; opacity: card.chosen ? 0 : .2
                        Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
                    }
                    Rectangle {
                        anchors.fill: parent
                        gradient: Gradient { GradientStop { position: 0.75; color: "transparent" } GradientStop { position: 1; color: "#50091420" } }
                    }
                    Text {
                        visible: !!card.modelData.art
                        x: 18; anchors.bottom: parent.bottom; anchors.bottomMargin: 20
                        width: parent.width - 32
                        objectName: "cardArtworkTitle-" + card.index
                        text: card.modelData.name.replace(" ", "\n")
                        wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                        textFormat: Text.PlainText
                        color: card.chosen ? uiTheme.themeColors.accent : "#f6f3e8"; font.pixelSize: Math.min(24, card.posterWidth * .12); font.weight: Font.Bold; font.letterSpacing: 1
                    }
                    Rectangle {
                        id: cardStatusBadge
                        readonly property string statusText: window.bridge.frozenIds.indexOf(card.modelData.id) >= 0 ? qsTranslate("NGS", "FROZEN") : window.bridge.unavailableIds.indexOf(card.modelData.id) >= 0 ? qsTranslate("NGS", "UNAVAILABLE") : window.bridge.removingIds.indexOf(card.modelData.id) >= 0 ? qsTranslate("NGS", "REMOVING…") : window.bridge.playedIds.indexOf(card.modelData.id) < 0 ? qsTranslate("NGS", "NEW") : ""
                        anchors.top: parent.top; anchors.right: parent.right; anchors.margins: 10
                        width: Math.min(128, parent.width - 12); height: 28; radius: 5; color: uiTheme.themeColors.accent
                        visible: statusText.length > 0 && (window.bridge.installedIds.indexOf(card.modelData.id) >= 0 || window.bridge.removingIds.indexOf(card.modelData.id) >= 0)
                        Text {
                            objectName: "cardStatus-" + card.index
                            anchors.centerIn: parent
                            text: cardStatusBadge.statusText
                            color: uiTheme.themeColors.accentText; font.pixelSize: Math.min(11, parent.width / 11); font.weight: Font.DemiBold
                        }
                    }
                }
            }
            Text {
                objectName: "cardCaption-" + card.index
                x: 5; y: card.posterHeight + 30; text: card.modelData.art ? card.modelData.genre : card.modelData.name
                textFormat: Text.PlainText
                width: parent.width - 10; elide: Text.ElideRight
                color: card.chosen ? uiTheme.themeColors.accent : uiTheme.themeColors.muted; font.pixelSize: 12
                Behavior on color { ColorAnimation { duration: 240 } }
            }
        }
        onClicked: {
            window.focusZone = 2
            if (window.selectedIndex === index) window.runSelected()
            else window.selectedIndex = index
        }
        HeaderButton {
            objectName: "cardInfo-" + card.index
            x: posterFace.x + posterFace.width - width - 8; y: posterFace.y + posterFace.height - height - 8
            width: 36; height: 36; text: "i"; focusPolicy: Qt.NoFocus
            font.pixelSize: 18; font.weight: Font.DemiBold
            background: FluidSurface { radius: 18; color: uiTheme.themeColors.surface; border.width: 1; border.color: uiTheme.themeColors.line; shadowEnabled: false }
            Accessible.name: qsTranslate("NGS", "Game information and statistics")
            onClicked: { window.selectedIndex = card.index; window.focusZone = 2; window.openDetails() }
        }
    }

    Item {
        id: scene
        objectName: "launcherScene"
        layer.enabled: window.bridge.backdropBlur && (window.modalVisible || catalogizerPane.visible) && GraphicsInfo.api !== GraphicsInfo.Software
        layer.smooth: true
        layer.effect: MultiEffect { blurEnabled: true; blurMax: 64; blur: 1; autoPaddingEnabled: false }
        visible: window.visible && window.visibility !== Window.Minimized && !window.gameInProgress
        width: window.width / window.uiScale
        height: window.height / window.uiScale
        scale: window.uiScale
        transformOrigin: Item.TopLeft
        opacity: window.intro

        GameBackdrop {
            id: backdrop
            objectName: "gameBackdrop"
            anchors.fill: parent
            visible: window.section !== 2
            source: window.section === 2 ? "../assets/downloads.svg" : window.selected.backgroundImage || window.selected.image
            opacity: 0.85
            animationScale: window.animScale
            pan: window.section === 2 ? 0 : (2 - window.selectedIndex % 5) * 10
            scale: 1.05
        }
        Rectangle {
            anchors.fill: parent; visible: window.section === 2
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0; color: uiTheme.themeColors.background }
                GradientStop { position: 1; color: uiTheme.themeColors.ambient }
            }
        }
        Rectangle {
            objectName: "sceneHorizontalShade"
            anchors.fill: parent
            opacity: window.modalVisible || catalogizerPane.visible ? .3 : 1
            Behavior on opacity { NumberAnimation { duration: 160 } }
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0; color: Qt.rgba(window.color.r, window.color.g, window.color.b, 0.949) }
                GradientStop { position: 0.42; color: Qt.rgba(window.color.r, window.color.g, window.color.b, 0.761) }
                GradientStop { position: 0.82; color: Qt.rgba(window.color.r, window.color.g, window.color.b, 0.082) }
                GradientStop { position: 1; color: Qt.rgba(window.color.r, window.color.g, window.color.b, 0.208) }
            }
        }
        Rectangle {
            objectName: "sceneVerticalShade"
            anchors.fill: parent
            opacity: window.modalVisible || catalogizerPane.visible ? .3 : 1
            Behavior on opacity { NumberAnimation { duration: 160 } }
            gradient: Gradient {
                GradientStop { position: 0; color: Qt.rgba(window.color.r, window.color.g, window.color.b, 0.188) }
                GradientStop { position: 0.42; color: Qt.rgba(window.color.r, window.color.g, window.color.b, 0.000) }
                GradientStop { position: 0.64; color: Qt.rgba(window.color.r, window.color.g, window.color.b, 0.851) }
                GradientStop { position: 1; color: Qt.rgba(window.color.r, window.color.g, window.color.b, 1.000) }
            }
        }

        Item {
            id: foreground
            objectName: "foregroundContent"
            anchors.fill: parent
            opacity: window.modalVisible || catalogizerPane.visible ? 0 : 1
            Behavior on opacity { NumberAnimation { duration: 160 } }
            property real depth: window.modalVisible ? 1 : 0
            Behavior on depth { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }
            transform: [
                Translate { y: (1 - window.intro) * 20 },
                Scale { origin.x: foreground.width / 2; origin.y: foreground.height / 2; xScale: 1 - foreground.depth * .018; yScale: xScale }
            ]
        Row {
            x: 64; y: 66; spacing: 10
            Image { objectName: "projectLogo"; source: "../assets/ngs.svg"; width: 40; height: 40; sourceSize.width: 80; sourceSize.height: 80; fillMode: Image.PreserveAspectFit; anchors.verticalCenter: parent.verticalCenter }
            Text { text: "NGS"; color: uiTheme.themeColors.text; font.pixelSize: 29; font.weight: Font.Bold; font.letterSpacing: 4 }
        }
        Text { objectName: "projectSubtitle"; x: 64; y: 104; text: "NAS Game Switcher"; color: uiTheme.themeColors.muted; font.pixelSize: 12; font.letterSpacing: 1 }
        Item {
            id: navigation
            objectName: "headerNavigation"
            x: 250; y: 68
            width: 410; height: 52
            Text {
                objectName: "previousSectionHint"
                anchors.right: parent.left; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter
                text: window.hints.previous_section; color: uiTheme.themeColors.muted; font.pixelSize: 12; font.weight: Font.DemiBold
                opacity: window.hints.device === "gamepad" && window.focusZone === 0 ? 0.85 : 0
                Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
            }
            Text {
                objectName: "nextSectionHint"
                anchors.left: parent.right; anchors.leftMargin: 10; anchors.verticalCenter: parent.verticalCenter
                text: window.hints.next_section; color: uiTheme.themeColors.muted; font.pixelSize: 12; font.weight: Font.DemiBold
                opacity: window.hints.device === "gamepad" && window.focusZone === 0 ? 0.85 : 0
                Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
            }
            Rectangle {
                objectName: "navigationPill"
                visible: window.section !== 2
                x: window.section === 3 ? 260 : window.section * 130
                width: window.section === 3 ? 150 : 130; height: 52; radius: 26
                color: uiTheme.themeColors.line
                Behavior on x { SmoothedAnimation { velocity: 1500; duration: 340 } }
            }
            Repeater {
                id: sectionButtons
                model: [qsTranslate("NGS", "My games"), qsTranslate("NGS", "Catalog"), qsTranslate("NGS", "Installers")]
                FluidButton {
                    required property string modelData
                    required property int index
                    readonly property int targetSection: [0, 1, 3][index]
                    visible: !window.bridge.localMode || index === 0
                    x: index * 130
                    width: index === 2 ? 150 : 130; height: 52
                    text: modelData
                    focusPolicy: Qt.NoFocus
                    highlighted: window.focusZone === 0 && window.navigationIndex === (targetSection === 3 ? 8 : targetSection)
                    background: FluidSurface {
                        objectName: "tabFocus-" + parent.targetSection
                        radius: 26
                        color: parent.down ? uiTheme.themeColors.line : "transparent"
                        border.width: parent.highlighted ? 2 : 0
                        border.color: window.accent
                    }
                    contentItem: Text {
                        text: parent.text; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                        color: window.section === parent.targetSection ? uiTheme.themeColors.text : uiTheme.themeColors.muted
                        font.pixelSize: 16; font.weight: window.section === parent.targetSection ? Font.DemiBold : Font.Normal
                        Behavior on color { ColorAnimation { duration: 180 } }
                    }
                    onClicked: window.changeSection(targetSection)
                }
            }
        }
        Row {
            id: libraryTools; objectName: "libraryTools"
            anchors.right: parent.right; anchors.rightMargin: 64
            y: 70; spacing: 8; height: 48
            visible: window.section !== 2
            Text {
                objectName: "libraryGameCount"
                visible: scene.width >= 1400
                id: libraryCount; width: 70; height: 48; verticalAlignment: Text.AlignVCenter; horizontalAlignment: Text.AlignRight
                text: (window.searchQuery || window.activeFilterCount ? qsTranslate("NGS", "Found: ") : qsTranslate("NGS", "Games: ")) + window.displayGames.length
                color: uiTheme.themeColors.muted; font.pixelSize: 14
            }
            HeaderButton {
                id: librarySortButton; objectName: "librarySortButton"
                width: Math.max(86, Math.min(220, scene.width - 978 - 136 - (libraryCount.visible ? 78 : 0))); height: 48
                visible: window.section === 0
                text: window.sortLabels[window.sortModes.indexOf(window.bridge.librarySort)]
                Accessible.name: qsTranslate("NGS", "Sort: ") + text
                highlighted: window.focusZone === 0 && window.navigationIndex === 7
                contentItem: Item {
                    Text { anchors.left: parent.left; anchors.leftMargin: 14; anchors.right: arrow.left; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter; text: librarySortButton.text; color: uiTheme.themeColors.text; font.pixelSize: 15; elide: Text.ElideRight }
                    Item {
                        id: arrow; anchors.right: parent.right; anchors.rightMargin: 14; anchors.verticalCenter: parent.verticalCenter; width: 14; height: 10
                        Rectangle { x: 1; y: 4; width: 8; height: 2; radius: 1; rotation: 45; color: uiTheme.themeColors.muted }
                        Rectangle { x: 6; y: 4; width: 8; height: 2; radius: 1; rotation: -45; color: uiTheme.themeColors.muted }
                    }
                }
                onClicked: librarySortMenu.open()
            }
            HeaderButton {
                id: installedFilter; objectName: "installedFilter"
                width: 128; height: 48
                text: qsTranslate("NGS", "Filters")
                highlighted: window.focusZone === 0 && window.navigationIndex === 6
                Accessible.name: text + qsTranslate("NGS", ": active filters: ") + window.activeFilterCount
                NotificationBadge { count: window.activeFilterCount; x: parent.width - width + 2; y: -4 }
                contentItem: Item {
                    Column {
                        x: 4; anchors.verticalCenter: parent.verticalCenter; width: 18; spacing: 4
                        Repeater {
                            model: [18, 12, 6]
                            Rectangle { required property int modelData; width: modelData; height: 2; radius: 1; anchors.horizontalCenter: parent.horizontalCenter; color: window.activeFilterCount ? window.accent : uiTheme.themeColors.text }
                        }
                    }
                    Text { objectName: "gameFiltersCaption"; anchors.left: parent.left; anchors.leftMargin: 32; anchors.right: parent.right; anchors.rightMargin: 4; anchors.verticalCenter: parent.verticalCenter; text: installedFilter.text; color: uiTheme.themeColors.text; font.pixelSize: 14; elide: Text.ElideRight }
                }
                onClicked: window.openFilters()
            }
        }
            HeaderButton {
                id: addGameButton; objectName: "addLocalGameButton"
                x: 700; y: 70; text: window.hints.toggle + (window.section === 0 ? qsTranslate("NGS", " Add game") : qsTranslate("NGS", " Catalog manager")); width: 190; height: 48; focusPolicy: Qt.NoFocus
                visible: window.section !== 2
                Accessible.name: window.section === 0 ? qsTranslate("NGS", "Add a game from this device") : window.section === 3 ? qsTranslate("NGS", "Installer catalog manager") : qsTranslate("NGS", "Game catalog manager")
                enabled: !window.bridge.operationBusy
                highlighted: window.focusZone === 0 && window.navigationIndex === 3
                onClicked: window.section === 0 ? localScanner.openScanner() : window.openCatalogizer(window.section === 3)
            }
        Row {
            objectName: "utilityNavigation"
            anchors.right: parent.right; anchors.rightMargin: 64
            y: 16; spacing: 6
            Text {
                objectName: "previousControlHint"; text: window.hints.previous_control; height: 48; verticalAlignment: Text.AlignVCenter; color: uiTheme.themeColors.muted; font.pixelSize: 12
                opacity: window.hints.device === "gamepad" && (window.focusZone === -1 || window.focusZone === 0) ? 0.85 : 0
                Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
            }
            Row {
                objectName: "systemStatusBar"
                spacing: 6
                Repeater {
                    id: statusButtons
                    model: window.statusKeys
                    HeaderButton {
                        required property string modelData
                        required property int index
                        objectName: "status-" + modelData
                        text: window.systemLabel(modelData)
                        width: modelData === "clock" ? 64 : modelData === "battery" ? 76 : 48; height: 48
                        highlighted: window.focusZone === -1 && window.statusIndex === index
                        Accessible.name: text + qsTranslate("NGS", ". Open NGS panel")
                        contentItem: Item {
                            Row {
                                anchors.centerIn: parent; spacing: 5
                                StatusIcon { kind: modelData; state: window.bridge.systemState; visible: modelData !== "clock"; width: visible ? 22 : 0; height: 22; anchors.verticalCenter: parent.verticalCenter }
                                Text {
                                    visible: modelData === "clock" || modelData === "battery"
                                    text: modelData === "clock" ? window.clock : !window.bridge.systemState.has_battery ? "AC" : window.bridge.systemState.battery >= 0 ? window.bridge.systemState.battery + "%" : "?"
                                    font.pixelSize: 13; color: uiTheme.themeColors.text; anchors.verticalCenter: parent.verticalCenter
                                }
                            }
                        }
                        onClicked: { window.focusZone = -1; window.statusIndex = index; window.showSystemPanel(modelData) }
                    }
                }
            }
            HeaderButton {
                id: searchButton; objectName: "gameSearchButton"
                text: "⌕"; width: 48; height: 48; visible: window.section !== 2
                Accessible.name: qsTranslate("NGS", "Search games"); highlighted: window.focusZone === 0 && window.navigationIndex === 9
                onClicked: window.openSearch()
                contentItem: Item {
                    Rectangle { x: (parent.width - 20) / 2; y: (parent.height - 20) / 2; width: 14; height: 14; radius: 7; color: "transparent"; border.color: uiTheme.themeColors.text; border.width: 2 }
                    Rectangle { x: parent.width / 2 + 4; y: parent.height / 2 + 5; width: 9; height: 2; rotation: 45; color: uiTheme.themeColors.text }
                }
                Rectangle { anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 5; width: 7; height: 7; radius: 4; color: window.accent; visible: !!window.searchQuery }
            }
            HeaderButton {
                id: viewButton; objectName: "gameViewButton"
                text: window.gridMode ? "▤" : "▦"; width: 48; height: 48; visible: window.section !== 2
                Accessible.name: window.gridMode ? qsTranslate("NGS", "Show ribbon") : qsTranslate("NGS", "Show grid")
                highlighted: window.focusZone === 0 && window.navigationIndex === 10
                onClicked: window.bridge.setGamesView(window.gridMode ? "ribbon" : "grid")
                contentItem: Item {
                    Grid {
                        anchors.centerIn: parent; columns: window.gridMode ? 1 : 2; spacing: 3
                        Repeater { model: window.gridMode ? 3 : 4; Rectangle { width: window.gridMode ? 18 : 7; height: window.gridMode ? 3 : 7; radius: 1; color: uiTheme.themeColors.text } }
                    }
                }
            }
            HeaderButton {
                id: downloadsButton; objectName: "downloadsButton"
                text: qsTranslate("NGS", "Downloads"); width: 48; height: 48
                Accessible.name: window.downloadsLabel
                highlighted: window.focusZone === 0 && window.navigationIndex === 2
                onClicked: window.changeSection(2)
                contentItem: Item {
                    readonly property color ink: window.section === 2 ? window.accent : uiTheme.themeColors.text
                    Item {
                        anchors.centerIn: parent; width: 24; height: 24
                        Rectangle { x: 11; y: 1; width: 2; height: 14; radius: 1; color: parent.parent.ink }
                        Rectangle { x: 5; y: 11; width: 9; height: 2; rotation: 45; color: parent.parent.ink }
                        Rectangle { x: 10; y: 11; width: 9; height: 2; rotation: -45; color: parent.parent.ink }
                        Rectangle { x: 2; y: 22; width: 20; height: 2; radius: 1; color: parent.parent.ink }
                    }
                }
                NotificationBadge { objectName: "activeDownloadsBadge"; count: window.activeDownloads; x: parent.width - width + 4; y: -4 }
                NotificationBadge { objectName: "failedDownloadsBadge"; count: window.failedDownloads; error: true; x: parent.width - width + 4; y: parent.height - height + 2 }
            }
            HeaderButton {
                id: minimizeButton; objectName: "minimizeButton"
                text: qsTranslate("NGS", "Minimize"); width: 48; height: 48; focusPolicy: Qt.NoFocus
                Accessible.name: text
                contentItem: Item { Rectangle { anchors.centerIn: parent; width: 16; height: 2; radius: 1; color: uiTheme.themeColors.text } }
                highlighted: window.focusZone === 0 && window.navigationIndex === 4
                onClicked: window.bridge.minimize()
            }
            HeaderButton {
                id: menuButton; objectName: "menuButton"
                text: qsTranslate("NGS", "Settings"); width: 48; height: 48; focusPolicy: Qt.NoFocus
                Accessible.name: window.updateAvailable ? qsTranslate("NGS", "Settings · NGS update available") : qsTranslate("NGS", "Settings")
                contentItem: Item {
                    KeyboardGlyph { keyName: "@settings"; ink: uiTheme.themeColors.text }
                }
                highlighted: window.focusZone === 0 && window.navigationIndex === 5
                onClicked: settingsPane.open()
                NotificationBadge { objectName: "updateAvailableBadge"; count: window.updateAvailable ? 1 : 0; x: parent.width - width + 4; y: -4 }
            }
            Text {
                objectName: "nextControlHint"; text: window.hints.next_control; height: 48; verticalAlignment: Text.AlignVCenter; color: uiTheme.themeColors.muted; font.pixelSize: 12
                opacity: window.hints.device === "gamepad" && (window.focusZone === -1 || window.focusZone === 0) ? 0.85 : 0
                Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
            }
        }

        Item {
            id: catalogPage
            objectName: "catalogPage"
            opacity: !window.gridMode && (window.section === 1 || (window.section !== 2 && (window.hasGames || !!window.searchQuery))) ? 1 : 0
            visible: opacity > 0
            enabled: window.section !== 2 && window.hasGames
            anchors.fill: parent
            transform: Translate { x: (1 - catalogPage.opacity) * -32 }
            Behavior on opacity { NumberAnimation { duration: 280; easing.type: Easing.InOutQuad } }

            Item {
                anchors.fill: parent
                opacity: window.heroOpacity
                transform: Translate { x: (1 - window.heroReveal) * -18 }

            Text {
                id: heroTitle
                objectName: "heroTitle"
                x: 64; y: parent.height < 820 ? 120 : 128
                text: window.heroGame.name
                textFormat: Text.PlainText
                width: parent.width - 128; elide: Text.ElideRight
                color: uiTheme.themeColors.text; font.pixelSize: parent.height < 820 ? 34 : 72; font.weight: Font.Bold; font.letterSpacing: 3
            }
            Text {
                id: heroSubtitle
                objectName: "heroSubtitle"
                x: 68; y: heroTitle.y + heroTitle.height + 4; text: window.heroGame.subtitle
                width: parent.width - 136; elide: Text.ElideRight
                textFormat: Text.PlainText
                transform: Translate { y: (1 - window.heroReveal) * 14 }
                color: uiTheme.themeColors.text; font.pixelSize: 21; font.weight: Font.Medium
            }
            Text {
                objectName: "heroDescription"
                x: 68; y: heroSubtitle.y + heroSubtitle.height + 4; width: 545
                height: Math.max(0, heroActions.y - y - (parent.height < 820 ? 4 : 16))
                text: window.section === 3 && window.bridge.operationId === window.selected.id ? window.bridge.operationStatus : window.heroGame.description; wrapMode: Text.WordWrap
                textFormat: Text.PlainText
                maximumLineCount: 3; elide: Text.ElideRight
                opacity: Math.min(1, window.heroReveal * 1.8)
                transform: Translate { y: (1 - window.heroReveal) * 23 }
                color: uiTheme.themeColors.muted; font.pixelSize: 16; lineHeight: parent.height < 820 ? 1 : 1.4
            }
            }
            Row {
                id: heroActions
                objectName: "heroActions"
                x: 68; y: cards.y - height - (parent.height < 820 ? 8 : 42); spacing: 20
                ActionButton {
                    objectName: "gameActionButton"
                    visible: window.canAct
                    text: window.primaryActionText
                    enabled: window.actionEnabled
                    filled: true; highlighted: window.focusZone === 1 && window.heroAction === 0
                    onClicked: window.runSelected()
                }
                ActionButton {
                    objectName: "detailsButton"
                    visible: window.hasGames || !!window.searchQuery || window.activeFilterCount
                    text: window.hasGames ? qsTranslate("NGS", "Details     >") : window.searchQuery ? qsTranslate("NGS", "Clear search") : qsTranslate("NGS", "Show all"); filled: !window.canAct; highlighted: window.focusZone === 1 && window.heroAction === 1
                    onClicked: { window.focusZone = 1; window.openDetails() }
                }
                ActionButton {
                    objectName: "addInstallerGame"
                    visible: window.installerReady; enabled: !window.bridge.operationBusy
                    text: qsTranslate("NGS", "Add an installed game"); width: 290
                    highlighted: window.focusZone === 1 && window.heroAction === 3
                    onClicked: window.addInstalledGame()
                }
                Text { objectName: "heroGameSize"; text: (window.heroGame.total_size !== undefined ? installDestination.sizeLabel(window.heroGame.total_size) + " · " : "") + window.heroGame.genre;
                    width: Math.max(0, scene.width - heroActions.x - x - 64); elide: Text.ElideRight;
                    textFormat: Text.PlainText; color: uiTheme.themeColors.muted; font.pixelSize: 14; anchors.verticalCenter: parent.verticalCenter }
            }
            Row {
                objectName: "catalogHeading"
                visible: parent.height >= 820
                x: 68; y: heroActions.y + heroActions.height + 8; spacing: 14
                Text { text: window.hasGames ? qsTranslate("NGS", "Find your next adventure") : window.activeFilterCount ? qsTranslate("NGS", "Games hidden by filters") : qsTranslate("NGS", "Waiting for published games"); color: uiTheme.themeColors.text; font.pixelSize: 20; font.weight: Font.DemiBold }
                Text { text: window.hasGames ? (window.selectedIndex + 1) + " / " + window.displayGames.length : ""; color: uiTheme.themeColors.muted; font.pixelSize: 13; anchors.verticalCenter: parent.verticalCenter }
            }
            ListView {
                id: cards
                objectName: "gameCards"
                x: 60; y: parent.height - height - 70; width: parent.width - 120; height: 394
                orientation: ListView.Horizontal
                spacing: 12
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                reuseItems: true
                model: window.gridMode ? [] : window.displayGames
                currentIndex: window.selectedIndex
                snapMode: ListView.NoSnap
                synchronousDrag: true
                maximumFlickVelocity: 6000
                flickDeceleration: 500
                function keepSelectionVisible() {
                    if (!visible || !count || window.section === 2) return
                    cancelFlick()
                    currentIndex = window.selectedIndex
                    forceLayout()
                    if (window.pendingScroll >= 0) {
                        contentX = Math.max(originX, Math.min(window.pendingScroll, originX + Math.max(0, contentWidth - width)))
                        window.pendingScroll = -1
                    }
                    positionViewAtIndex(currentIndex, ListView.Contain)
                    const item = currentItem
                    if (item && item.index === currentIndex)
                        contentX = Math.max(item.x + item.width - width + 4, Math.min(contentX, item.x - 4))
                    window.bridge.rememberScroll(window.section, contentX)
                }
                onMovementEnded: if (window.section !== 2) window.bridge.rememberScroll(window.section, contentX)
                onCurrentIndexChanged: Qt.callLater(keepSelectionVisible)
                onCountChanged: Qt.callLater(keepSelectionVisible)
                onCurrentItemChanged: Qt.callLater(keepSelectionVisible)
                onWidthChanged: Qt.callLater(keepSelectionVisible)
                onHeightChanged: Qt.callLater(keepSelectionVisible)
                onVisibleChanged: if (visible) Qt.callLater(keepSelectionVisible)
                delegate: GameCard { height: cards.height }
            }
        }

        Item {
            id: gridPage
            objectName: "gridPage"
            anchors.fill: parent; visible: window.gridMode && window.section !== 2
            opacity: visible ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
            Text {
                x: 68; y: 119; width: parent.width - 136 - (window.section === 1 || window.section === 3 ? 400 : 0); elide: Text.ElideRight
                text: window.searchQuery ? qsTranslate("NGS", "Search: “") + window.searchQuery + qsTranslate("NGS", "” · Found: ") + window.displayGames.length
                    : window.hasGames ? window.selected.name + " · " + (window.selectedIndex + 1) + " / " + window.displayGames.length
                    : window.section === 0 ? qsTranslate("NGS", "Add games from your device or download them from the catalog") : qsTranslate("NGS", "No games in this section yet")
                textFormat: Text.PlainText; color: uiTheme.themeColors.text; font.pixelSize: 20; font.weight: Font.Medium
            }
            GridView {
                id: gameGrid; objectName: "gameGrid"
                x: 60; y: 156; width: parent.width - 120; height: parent.height - y - 76
                readonly property int columns: Math.max(1, Math.floor(width / 190))
                cellWidth: width / columns; cellHeight: Math.floor((cellWidth - 40) * 1.5) + 66
                synchronousDrag: true
                maximumFlickVelocity: 6000
                flickDeceleration: 500
                clip: true; boundsBehavior: Flickable.StopAtBounds; reuseItems: true
                model: window.gridMode && window.section !== 2 ? window.displayGames : []
                currentIndex: window.selectedIndex
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                NumberAnimation {
                    id: gridScroll; objectName: "gridScrollAnimation"
                    target: gameGrid; property: "contentY"; duration: 240; easing.type: Easing.OutCubic
                    onFinished: if (gameGrid.visible) window.bridge.rememberGridScroll(window.section, gameGrid.contentY)
                }
                function keepSelectionVisible(animate = true) {
                    if (!visible || !count) return
                    cancelFlick(); currentIndex = window.selectedIndex; forceLayout()
                    if (window.pendingGridScroll >= 0) {
                        gridScroll.stop()
                        contentY = Math.max(originY, Math.min(window.pendingGridScroll, originY + Math.max(0, contentHeight - height)))
                        window.pendingGridScroll = -1
                    }
                    if (window.revealGridSelection) {
                        const top = originY + Math.floor(currentIndex / columns) * cellHeight
                        const bottom = top + cellHeight
                        const target = Math.max(originY, Math.min(top < contentY ? top : bottom > contentY + height ? bottom - height : contentY,
                            originY + Math.max(0, contentHeight - height)))
                        if (Math.abs(target - contentY) > .5) {
                            gridScroll.stop()
                            if (animate) { gridScroll.from = contentY; gridScroll.to = target; gridScroll.start() }
                            else contentY = target
                        }
                        window.revealGridSelection = false
                    }
                    window.bridge.rememberGridScroll(window.section, gridScroll.running ? gridScroll.to : contentY)
                }
                onMovementStarted: gridScroll.stop()
                onMovementEnded: if (visible) window.bridge.rememberGridScroll(window.section, contentY)
                onCurrentIndexChanged: Qt.callLater(keepSelectionVisible)
                onCountChanged: Qt.callLater(keepSelectionVisible, false)
                onWidthChanged: { window.revealGridSelection = true; Qt.callLater(keepSelectionVisible, false) }
                onHeightChanged: { window.revealGridSelection = true; Qt.callLater(keepSelectionVisible, false) }
                onVisibleChanged: { gridScroll.stop(); if (visible) Qt.callLater(keepSelectionVisible, false) }
                delegate: GameCard { width: gameGrid.cellWidth - 8; height: gameGrid.cellHeight - 8 }
            }
            Text {
                objectName: "gridEmptyMessage"; anchors.centerIn: parent; width: parent.width - 136
                visible: !window.hasGames
                text: window.activeFilterCount ? qsTranslate("NGS", "Nothing found. Open Filters and reset or change the conditions.") : window.searchQuery ? qsTranslate("NGS", "Nothing found. Try another query or clear the search with ") + window.hints.back + "."
                    : window.section === 1 ? window.bridge.catalogStatus + "\n" + window.hints.refresh + qsTranslate("NGS", " — refresh catalog.")
                    : qsTranslate("NGS", "Choose + above to add games.")
                textFormat: Text.PlainText; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter
                color: uiTheme.themeColors.muted; font.pixelSize: 23
            }
        }

        Text {
            objectName: "catalogRefresh"
            x: parent.width - (window.gridMode ? 420 : 520)
            y: window.gridMode ? 117 : parent.height < 820 ? heroActions.y - 38 : heroActions.y + heroActions.height + 8
            height: 30; width: window.gridMode ? 352 : 452
            visible: window.section === 1 || window.section === 3
            text: window.section === 3 ? window.bridge.installerStatus : window.bridge.catalogStatus
            color: uiTheme.themeColors.muted; font.pixelSize: 13; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
        }

        Repeater {
            model: [0]
        Column {
            id: emptyPage
            required property int modelData
            opacity: !window.gridMode && !window.searchQuery && window.section === 0 && !window.hasGames && !window.activeFilterCount ? 1 : 0
            visible: opacity > 0
            enabled: !window.gridMode && !window.searchQuery && window.section === 0 && !window.hasGames && !window.activeFilterCount
            x: 68; y: 255; width: 650; spacing: 22
            transform: Translate { x: (1 - emptyPage.opacity) * 32 }
            Behavior on opacity { NumberAnimation { duration: 280; easing.type: Easing.InOutQuad } }
            Text { text: emptyPage.modelData === 0 ? qsTranslate("NGS", "Your games.\nAlways with you.") : qsTranslate("NGS", "Downloads.\nUnder control."); color: uiTheme.themeColors.text; font.pixelSize: 62; font.weight: Font.Bold; lineHeight: 1.1 }
            Text { text: window.bridge.localMode ? qsTranslate("NGS", "Add a game folder or scan installed store games. Your library and playtime stay on this device. No server or account is required.") : qsTranslate("NGS", "Download a game from the catalog or add an existing folder with X."); width: 540; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 19; lineHeight: 1.4 }
            ActionButton {
                objectName: "emptyLibraryAction-" + emptyPage.modelData
                width: Math.max(210, contentItem.implicitWidth + 40)
                text: window.bridge.localMode ? qsTranslate("NGS", "Add a game from this device") : qsTranslate("NGS", "Open catalog"); filled: true; highlighted: window.focusZone === 1
                onClicked: window.openEmptyLibrary()
            }
        }
        }

        Column {
            objectName: "emptyInstallers"
            visible: !window.gridMode && !window.searchQuery && window.section === 3 && !window.hasGames && !window.activeFilterCount
            x: 68; y: 205; width: parent.width - 136; spacing: 22
            Text { text: qsTranslate("NGS", "Installers"); color: uiTheme.themeColors.text; font.pixelSize: 54; font.weight: Font.Bold }
            Text { text: window.bridge.installerStatus; width: Math.min(parent.width, 750); wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 19 }
            Text { text: qsTranslate("NGS", "Download the complete installer package, install the game normally, then add its folder to My games."); width: Math.min(parent.width, 750); wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 19 }
            ActionButton { text: qsTranslate("NGS", "Open catalog manager"); width: 260; filled: true; highlighted: window.focusZone === 1; onClicked: window.openCatalogizer(true) }
        }

        DownloadQueue {
            id: downloadQueue
            objectName: "downloadsPage"
            bridge: window.bridge
            x: 68; y: 195; width: parent.width - 136; height: parent.height - y - 90
            opacity: window.section === 2 ? 1 : 0; visible: opacity > 0; enabled: window.section === 2
            transform: Translate { x: (1 - opacity) * 32 }
            Behavior on opacity { NumberAnimation { duration: 280; easing.type: Easing.OutCubic } }
            onFocused: window.focusZone = 1
        }

        Rectangle { x: 64; y: parent.height - 66; width: parent.width - 128; height: 1; color: uiTheme.themeColors.line }
        Row {
            id: bottomHintsRow
            x: 68; y: parent.height - 43; spacing: 28
            visible: !window.selectionHelp || window.hints.device !== "gamepad"
            opacity: visible ? 0.85 : 0
            transform: Translate { y: (1 - bottomHintsRow.opacity / 0.85) * 6 }
            Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
            Text { objectName: "acceptHint"; text: window.hints.activate + qsTranslate("NGS", "   Select"); color: uiTheme.themeColors.muted; font.pixelSize: 13 }
            HeaderButton { objectName: "detailsHint"; visible: window.hasGames && window.section !== 2; height: 30; y: -7; text: window.hints.menu + qsTranslate("NGS", "   Details"); font.pixelSize: 13; onClicked: window.openDetails() }
            Text { objectName: "backHint"; text: window.hints.back + qsTranslate("NGS", "   Back"); color: uiTheme.themeColors.muted; font.pixelSize: 13 }
            Text { objectName: "sectionsHint"; text: window.hints.sections + qsTranslate("NGS", "   Sections"); color: uiTheme.themeColors.muted; font.pixelSize: 13 }
        }
        FluidSurface {
            id: selectionHelpBadge
            x: 64; y: parent.height - 54
            width: selectionHelpText.width + 36
            height: 36; radius: 18
            color: uiTheme.themeColors.soft
            border.width: 1; border.color: uiTheme.themeColors.border
            property real reveal: 1
            visible: !!window.selectionHelp && window.hints.device === "gamepad"
            opacity: reveal
            scale: 0.96 + 0.04 * reveal
            transform: Translate { y: (1 - selectionHelpBadge.reveal) * 8 }
            NumberAnimation {
                id: helpRevealAnim
                target: selectionHelpBadge; property: "reveal"
                from: 0; to: 1; duration: 220
                easing.type: Easing.OutBack; easing.overshoot: 1.15
            }
            Row {
                anchors.verticalCenter: parent.verticalCenter
                x: 12; spacing: 8
                Rectangle { width: 6; height: 6; radius: 3; color: window.accent; anchors.verticalCenter: parent.verticalCenter }
                Text {
                    id: selectionHelpText
                    objectName: "selectionHelp"
                    width: Math.min(implicitWidth, Math.max(0, freeSpaceLabel.x - selectionHelpBadge.x - 56))
                    text: window.selectionHelp; visible: !!text
                    onTextChanged: if (selectionHelpText.text) helpRevealAnim.restart()
                    textFormat: Text.PlainText; elide: Text.ElideRight
                    color: uiTheme.themeColors.text; font.pixelSize: 14; font.weight: Font.Medium
                    anchors.verticalCenter: parent.verticalCenter
                }
            }
        }
        Text {
            id: freeSpaceLabel
            objectName: "freeSpaceLabel"
            anchors.right: parent.right; anchors.rightMargin: 68
            y: parent.height - 43; text: window.bridge.freeSpace
            color: uiTheme.themeColors.muted; font.pixelSize: 13
        }
        }
    }

    SystemPanel { id: statusPanel; bridge: window.bridge; uiScale: window.uiScale }
    Item {
        id: launchTile; objectName: "launchTile"; anchors.fill: parent; z: 90
        visible: window.launchPending
        property string identity: ""
        property var card: ({})
        property url source
        property real progress: 0
        property real startX: 0; property real startY: 0
        property real startWidth: 200
        property rect landingRect: Qt.rect(0, 0, 180, 270)
        readonly property real zoomWidth: Math.max(startWidth * 1.7, Math.min(window.width * .34, window.height * .43))
        readonly property real flight: Math.min(1, progress)
        readonly property real landing: Math.max(0, progress - 1)
        MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }
        Image {
            id: flyingPoster; objectName: "launchFlyingPoster"
            readonly property real centerX: (window.width - launchTile.zoomWidth) / 2
            readonly property real centerY: (window.height - launchTile.zoomWidth * 1.5) / 2
            x: launchTile.progress <= 1 ? launchTile.startX + (centerX - launchTile.startX) * launchTile.flight
                : centerX + (launchTile.landingRect.x - centerX) * launchTile.landing
            y: launchTile.progress <= 1 ? launchTile.startY + (centerY - launchTile.startY) * launchTile.flight
                : centerY + (launchTile.landingRect.y - centerY) * launchTile.landing
            width: launchTile.progress <= 1 ? launchTile.startWidth + (launchTile.zoomWidth - launchTile.startWidth) * launchTile.flight
                : launchTile.zoomWidth + (launchTile.landingRect.width - launchTile.zoomWidth) * launchTile.landing
            height: width * 1.5
            source: launchTile.source; fillMode: Image.PreserveAspectCrop
            sourceSize.width: 600; sourceSize.height: 900
            layer.enabled: GraphicsInfo.api !== GraphicsInfo.Software
            layer.smooth: true
            layer.effect: MultiEffect {
                maskEnabled: true; maskThresholdMin: .5; maskSpreadAtMin: 1
                maskSource: Rectangle {
                    parent: launchTile; width: flyingPoster.width; height: flyingPoster.height
                    radius: 12 * window.toolScale; color: "white"; visible: false
                    layer.enabled: true; layer.smooth: true
                }
            }
        }
        SequentialAnimation {
            id: launchMotion; objectName: "launchMotion"
            NumberAnimation { target: launchTile; property: "progress"; from: 0; to: 1; duration: Math.round(300 * window.animScale); easing.type: Easing.OutCubic }
            ScriptAction { script: launchTile.landingRect = runningDashboard.posterRectangle() }
            NumberAnimation { target: launchTile; property: "progress"; from: 1; to: 2; duration: Math.round(360 * window.animScale); easing.type: Easing.OutBack; easing.overshoot: .5 }
            onFinished: {
                window.launchPending = false
                window.bridge.launchPresentationReady(launchTile.identity)
                window.update()
            }
        }
    }
    GameFilters {
        id: gameFiltersPane
        uiScale: window.toolScale; backHint: window.hints.back
        sectionName: window.section === 0 ? qsTranslate("NGS", "My games") : window.section === 3 ? qsTranslate("NGS", "Installers") : qsTranslate("NGS", "Catalog")
        statusKeys: window.filterStatusKeys; statusLabels: window.filterStatusLabels; genres: window.filterGenres
        resultCount: window.filterGames(window.filterSource, window.searchQuery, mode, genre).length
        onSubmitted: (mode, genre) => window.applyFilters(mode, genre)
    }
    MotionPopup {
        id: librarySortMenu
        objectName: "librarySortMenu"
        width: Math.min(window.width - 32, 360 * window.uiScale)
        height: contentItem.implicitHeight + topPadding + bottomPadding
        modal: true; focus: true; padding: 20 * window.uiScale
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onAboutToShow: window.sortAction = window.sortModes.indexOf(window.bridge.librarySort)
        background: FluidSurface { elevation: 1; shadowBlur: 26; color: uiTheme.themeColors.glass; radius: 26; border.color: uiTheme.themeColors.border }
        Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim }

        contentItem: Column {
            spacing: 10 * window.uiScale
            Text { text: qsTranslate("NGS", "Sort"); color: uiTheme.themeColors.text; font.pixelSize: 22 * window.uiScale }
            Repeater {
                model: window.sortLabels
                HeaderButton {
                    required property string modelData
                    required property int index
                    objectName: "librarySort-" + window.sortModes[index]
                    width: parent.width; height: 48 * window.uiScale
                    text: (window.bridge.librarySort === window.sortModes[index] ? "• " : "") + modelData
                    highlighted: window.sortAction === index
                    onClicked: { if (window.bridge.setLibrarySort(window.sortModes[index])) librarySortMenu.close() }
                }
            }
            HeaderButton { objectName: "librarySortBack"; text: qsTranslate("NGS", "Back · ") + window.hints.back; width: parent.width; height: 50 * window.uiScale; highlighted: window.sortAction === window.sortModes.length; onClicked: librarySortMenu.close() }
        }
    }

    ToolPage {
        id: details
        objectName: "detailsPopup"
        pageScale: window.uiScale
        leftPadding: Math.max(24, width * .045); rightPadding: leftPadding
        onAboutToShow: detailsScroll.contentItem.contentY = 0
        Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim; Behavior on opacity { NumberAnimation { duration: 250 } } }
        contentItem: Item {
            clip: true
            ScrollView {
                id: detailsScroll
                objectName: "detailsScroll"
                x: 0; y: 0; width: parent.width
                height: Math.max(30, parent.height - detailsActions.implicitHeight - 24)
                contentWidth: availableWidth
                clip: true
                background: Rectangle { color: window.detailsAction === 7 ? uiTheme.themeColors.button : "transparent"; radius: 16 }
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            Column {
                width: detailsScroll.availableWidth; spacing: 20 * window.uiScale
                GamePresentation {
                    id: detailsPresentation
                    width: parent.width; title: window.selected.name || ""
                    cover: window.selected.image || ""; background: window.selected.backgroundImage || ""
                    coverObjectName: "detailsCover"; backgroundObjectName: "detailsBanner"
                    uiScale: window.toolScale; presenting: details.visible
                }
                Column {
                    objectName: "gameStatistics"; width: parent.width; spacing: 10 * window.toolScale; visible: !window.selectedInstaller
                    Text { text: qsTranslate("NGS", "Statistics on this device"); color: uiTheme.themeColors.muted; font.pixelSize: 18 * window.toolScale }
                    Row {
                        width: parent.width; spacing: 16 * window.toolScale
                        Repeater {
                            model: [{label: qsTranslate("NGS", "Total playtime"), value: window.duration(window.detailsStatistics.total_seconds)},
                                    {label: qsTranslate("NGS", "Total launches"), value: String(window.detailsStatistics.launches || 0)},
                                    {label: qsTranslate("NGS", "Longest session"), value: window.duration(window.detailsStatistics.longest_seconds)}]
                            Column {
                                required property var modelData
                                width: (parent.width - parent.spacing * 2) / 3; spacing: 6
                                Text { width: parent.width; wrapMode: Text.WordWrap; text: modelData.label; color: uiTheme.themeColors.muted; font.pixelSize: 14 * window.toolScale }
                                Text { text: modelData.value; color: uiTheme.themeColors.text; font.pixelSize: 26 * window.toolScale }
                            }
                        }
                    }
                    Text { width: parent.width; visible: !!window.detailsStatistics.status; text: window.detailsStatistics.status || ""; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted }
                }
                Text { objectName: "detailsGameSize"; visible: window.selected.total_size !== undefined;
                    text: qsTranslate("NGS", "Game size: ") + installDestination.sizeLabel(window.selected.total_size); color: uiTheme.themeColors.accent; font.pixelSize: 17 * window.uiScale }
                Text { text: window.selected.description; textFormat: Text.PlainText; width: Math.min(parent.width, 960 * window.toolScale); color: uiTheme.themeColors.text; font.pixelSize: 18 * window.toolScale; wrapMode: Text.WordWrap; lineHeight: 1.45 }
                Text { text: window.selectedInstaller ? qsTranslate("NGS", "The entire installer package is downloaded. Install the game normally, then add its folder and confirm its executable. NGS keeps only a library entry for an independently installed game and does not manage its files.") : window.selectedUnavailable ? qsTranslate("NGS", "The folder or executable is unavailable. Connect the drive and refresh My games (") + window.hints.refresh + ")." : window.selectedRemoving ? qsTranslate("NGS", "Removal did not finish. You can retry it.") : window.selectedExternal ? qsTranslate("NGS", "Added without copying. Removing the library entry preserves the game files.") : window.selectedInstalled ? qsTranslate("NGS", "Installed on this device.") : window.selected.delivery === "on_request" || window.selected.manifest ? qsTranslate("NGS", "Ready to download.") : qsTranslate("NGS", "This game is not ready to download yet."); width: parent.width; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 14 * window.uiScale }
                Text { objectName: "detailsExecutable"; visible: window.detailsMore && window.selectedInstalled;
                    text: visible ? "EXE: " + window.bridge.executableLocation(window.selected.id) : ""; textFormat: Text.PlainText; width: parent.width; wrapMode: Text.WrapAnywhere; color: uiTheme.themeColors.muted; font.pixelSize: 13 * window.uiScale }
            }
            }
            Flow {
                id: detailsActions
                x: 0; width: parent.width; anchors.bottom: parent.bottom; spacing: 10
            ActionButton {
                id: detailsPrimary; objectName: "detailsGameAction"
                visible: window.canAct; enabled: window.actionEnabled
                text: window.primaryActionText
                filled: window.detailsLaunchSelected; highlighted: window.detailsLaunchSelected
                width: (window.selectedInstaller ? 220 : 150) * window.uiScale; height: Math.max(48, 50 * window.uiScale)
                onClicked: window.runSelected()
            }
            ActionButton {
                id: detailsAdd; objectName: "detailsAddInstallerGame"
                visible: window.installerReady; enabled: !window.bridge.operationBusy
                text: qsTranslate("NGS", "Add an installed game"); highlighted: window.detailsAction === 6
                width: 285 * window.uiScale; height: Math.max(48, 50 * window.uiScale)
                onClicked: window.addInstalledGame()
            }
            ActionButton {
                id: detailsBack; objectName: "detailsBack"
                text: qsTranslate("NGS", "Back"); filled: window.detailsAction === 1; highlighted: window.detailsAction === 1
                width: 120 * window.uiScale; height: Math.max(48, 50 * window.uiScale)
                onClicked: details.close()
            }
            ActionButton {
                id: detailsReveal; objectName: "revealGameButton"
                visible: window.detailsMore && window.selectedInstalled; text: qsTranslate("NGS", "Folder / executable"); highlighted: window.detailsAction === 3
                width: 165 * window.uiScale; height: Math.max(48, 50 * window.uiScale)
                onClicked: window.bridge.revealGame(window.selected.id)
            }
            ActionButton {
                id: detailsRemove; objectName: "removeGameButton"
                text: window.selectedRemoving ? qsTranslate("NGS", "Retry removal") : qsTranslate("NGS", "Remove from library")
                visible: window.detailsMore && window.selectedRemovable; enabled: !window.bridge.operationBusy
                width: 230 * window.uiScale; height: Math.max(48, 50 * window.uiScale)
                highlighted: window.detailsAction === 2
                onClicked: window.openRemove()
            }
            ActionButton {
                id: detailsMoreButton; objectName: "detailsMore"
                text: window.detailsMore ? qsTranslate("NGS", "Fewer actions") : qsTranslate("NGS", "More actions")
                width: 210 * window.uiScale; height: Math.max(48, 50 * window.uiScale); highlighted: window.detailsAction === 8
                onClicked: window.detailsMore = !window.detailsMore
            }
            FluidButton {
                id: optionsButton; objectName: "launchOptionsButton"
                text: qsTranslate("NGS", "Launch options"); focusPolicy: Qt.NoFocus; visible: window.detailsMore && !window.selectedInstaller
                width: 225 * window.uiScale; height: Math.max(48, 52 * window.uiScale)
                highlighted: window.detailsAction === 5
                onClicked: window.openLaunchOptions()
            }
            FluidButton {
                id: metadataButton; objectName: "findMetadataButton"
                width: Math.min(parent.width, Math.max(240 * window.uiScale, implicitWidth)); height: Math.max(48, 52 * window.uiScale)
                visible: window.selectedExternal; enabled: !window.bridge.operationBusy
                highlighted: window.detailsAction === 4
                text: qsTranslate("NGS", "Change game / artwork"); focusPolicy: Qt.NoFocus
                onClicked: { details.close(); window.bridge.findMetadata(window.selected.id) }
            }
            FluidButton {
                id: detailsResetButton; objectName: "detailsResetStatistics"
                visible: !window.selectedInstaller; enabled: !window.bridge.operationBusy
                text: window.detailsResetConfirm ? qsTranslate("NGS", "Reset on this device") + "?" : qsTranslate("NGS", "Reset statistics")
                highlighted: window.detailsAction === 9
                height: Math.max(48, 52 * window.uiScale)
                onClicked: {
                    if (!window.detailsResetConfirm) { window.detailsResetId = window.selected.id; window.detailsResetConfirm = true; window.detailsAction = 10; return }
                    if (window.bridge.resetLocalGameStatistics(window.detailsResetId)) {
                        window.detailsResetConfirm = false
                        window.detailsStatistics = window.bridge.gameStatistics(window.selected.id)
                    }
                }
            }
            FluidButton {
                id: detailsResetCancel; objectName: "detailsResetCancel"
                visible: window.detailsResetConfirm; text: qsTranslate("NGS", "Cancel", "Отмена")
                highlighted: window.detailsAction === 10; height: Math.max(48, 52 * window.uiScale)
                onClicked: { window.detailsResetConfirm = false; window.detailsAction = 9 }
            }
            }
        }

    }

    MotionPopup {
        id: removeDialog
        objectName: "removeDialog"
        width: Math.min(window.width - 40, 650); height: removeContent.implicitHeight + padding * 2
        modal: true; focus: true; padding: 24
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: FluidSurface { elevation: 1; shadowBlur: 26; color: uiTheme.themeColors.glass; radius: 26; border.color: uiTheme.themeColors.error }
        Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim }

        Column {
            id: removeContent
            width: parent.width; spacing: 12
            Text { text: window.removeInProgress ? qsTranslate("NGS", "Finish removing the game?") : qsTranslate("NGS", "Remove this game from the library?"); color: uiTheme.themeColors.text; font.pixelSize: 24; font.weight: Font.DemiBold }
            Text { text: window.removeName; textFormat: Text.PlainText; width: parent.width; elide: Text.ElideRight; color: uiTheme.themeColors.warning; font.pixelSize: 20 }
            Text { text: window.removeLocation; textFormat: Text.PlainText; width: parent.width; elide: Text.ElideMiddle; color: uiTheme.themeColors.muted; font.pixelSize: 13 }
            Text { text: window.removeInProgress ? qsTranslate("NGS", "Deletes this copy's entire folder, including saves stored inside it. The original on the NAS is preserved.") : window.removeExternal ? qsTranslate("NGS", "Only the NGS library entry is removed. The game folder and all its files are preserved.") : qsTranslate("NGS", "Remove entry preserves all files and saves. You can add the game again from its local folder. Delete game and files removes this copy's entire folder."); width: parent.width; wrapMode: Text.WordWrap; color: uiTheme.themeColors.muted; font.pixelSize: 14 }
            Column {
                width: parent.width; spacing: 8
                ActionButton { text: qsTranslate("NGS", "Cancel", "Отмена"); width: parent.width; height: 44; filled: window.removeAction === 0; highlighted: window.removeAction === 0; onClicked: removeDialog.close() }
                ActionButton { objectName: "forgetGameConfirm"; text: qsTranslate("NGS", "Remove entry · keep files"); visible: !window.removeInProgress; enabled: visible; width: parent.width; height: 44; filled: window.removeAction === 1; highlighted: window.removeAction === 1; onClicked: window.confirmRemove(false) }
                ActionButton { objectName: "deleteGameConfirm"; text: qsTranslate("NGS", "Delete game and files"); visible: !window.removeExternal; enabled: visible; width: parent.width; height: 44; filled: window.removeAction === 2 || (window.removeInProgress && window.removeAction === 1); highlighted: filled; onClicked: window.confirmRemove(true) }
            }
        }
    }

    GameSearch {
        id: gameSearch; bridge: window.bridge; uiScale: window.uiScale
        sectionName: window.section === 0 ? qsTranslate("NGS", "My games") : window.section === 3 ? qsTranslate("NGS", "Installers") : qsTranslate("NGS", "Catalog")
        onSubmitted: function(query) { window.applySearch(query) }
    }
    BindingsPane {
        id: bindingsPane; bridge: window.bridge; uiScale: window.toolScale
        navigationPath: settingsPane.childPage === "bindings" ? qsTranslate("NGS", "Settings  ›  Controllers and input") : ""
        covered: padKeyboardEditor.visible || keyboardLayoutEditor.visible
        onClosed: settingsPane.finishChild("bindings")
    }
    PadKeyboardEditor {
        id: padKeyboardEditor; bridge: window.bridge; uiScale: window.toolScale
        navigationPath: bindingsPane.visible ? (bindingsPane.navigationPath ? bindingsPane.navigationPath + "  ›  " : "") + qsTranslate("NGS", "Buttons and gamepad") : ""
    }
    KeyboardLayoutEditor { id: keyboardLayoutEditor; keyboard: window.bridge.keyboard }
    Connections { target: window.bridge.keyboard; function onLayoutEditRequested(draft) { keyboardLayoutEditor.begin(draft); keyboardLayoutEditor.open() } }
    Connections { target: window.bridge.keyboard; function onEditRequested() { padKeyboardEditor.open() } }
    KeyboardWindow { keyboard: window.bridge.keyboard; hints: window.hints; uiScale: window.bridge.settingsState.ui_scale; screen: window.screen }
    TouchpadWindow { keyboard: window.bridge.keyboard; hints: window.hints; uiScale: window.bridge.settingsState.ui_scale; screen: window.screen }
    TouchpadWindow { rightSide: true; keyboard: window.bridge.keyboard; hints: window.hints; uiScale: window.bridge.settingsState.ui_scale; screen: window.screen }
    property bool autoBackupPrompted: false
    Connections {
        target: window.bridge
        function onSettingsChanged() {
            if (window.bridge.localMode && window.section !== 0) window.changeSection(0)
        }
    }
    Connections {
        target: window.bridge.playerAccount
        function onChanged() {
            if (!window.bridge.playerAccount.state.logged_in) {
                window.autoBackupPrompted = false
            } else if (window.bridge.playerAccount.state.backup_prompt && !window.autoBackupPrompted && !window.bridge.playerAccount.state.busy) {
                window.autoBackupPrompted = true
                if (!playerAccountPane.visible) playerAccountPane.open()
            }
        }
    }
    PlayerAccount {
        id: playerAccountPane; bridge: window.bridge; uiScale: window.toolScale
        navigationPath: settingsPane.childPage === "account" ? qsTranslate("NGS", "Settings  ›  Users and account") : ""
        onClosed: settingsPane.finishChild("account")
    }
    SettingsPane { id: settingsPane; bridge: window.bridge; uiScale: window.toolScale; onUpdatesRequested: updatesPane.open(); onBindingsRequested: bindingsPane.open(); onAccountRequested: playerAccountPane.open()
        onCatalogizerRequested: installers => window.openCatalogizer(installers) }
    CatalogizerPane {
        id: catalogizerPane; hints: window.hints; bridge: window.bridge.remoteCatalogizer; keyboard: window.bridge.keyboard; uiScale: window.toolScale
        navigationPath: ["catalogizer", "installerCatalogizer"].indexOf(settingsPane.childPage) >= 0 ? qsTranslate("NGS", "Settings  ›  Catalog and publishing") : ""
        onClosed: settingsPane.finishChild(bridge === window.bridge.installerCatalogizer ? "installerCatalogizer" : "catalogizer")
    }
    UpdatesPane {
        id: updatesPane; bridge: window.bridge; uiScale: window.toolScale
        navigationPath: settingsPane.childPage === "updates" ? qsTranslate("NGS", "Settings  ›  System") : ""
        onClosed: settingsPane.finishChild("updates")
    }
    InstallDestination { id: installDestination; bridge: window.bridge; uiScale: window.toolScale }
    MetadataPicker { id: metadataPicker; bridge: window.bridge; uiScale: window.toolScale }
    LaunchOptions { id: launchOptions; bridge: window.bridge }

    LocalScanner {
        id: localScanner
        bridge: window.bridge
        uiScale: window.toolScale
    }

    MotionPopup {
        id: exitDialog
        objectName: "exitPopup"
        width: Math.min(window.width - 40, 480 * window.toolScale); height: menuGrid.implicitHeight + padding * 2
        modal: true; focus: true; padding: 20 * window.toolScale
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: FluidSurface { elevation: 1; shadowBlur: 26; color: uiTheme.themeColors.glass; radius: 26; border.color: uiTheme.themeColors.border }
        Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim; Behavior on opacity { NumberAnimation { duration: 200 } } }

        contentItem: Column {
            id: menuGrid; spacing: 10 * window.toolScale
            component MenuButton: HeaderButton {
                id: menuButton
                required property string item
                objectName: "menu" + item; text: window.menuLabel(item)
                width: exitDialog.availableWidth; height: Math.max(54, 64 * window.toolScale)
                Accessible.name: text
                highlighted: window.menuItems[window.menuAction] === item
                contentItem: Item {
                    Item {
                        anchors.left: parent.left; anchors.leftMargin: 8 * window.toolScale; anchors.verticalCenter: parent.verticalCenter
                        width: 26 * window.toolScale; height: width
                        KeyboardGlyph {
                            objectName: "menuIcon-" + menuButton.item
                            width: parent.width; height: width
                            keyName: ({Settings:"@settings",Minimize:"@minimize",Quit:"@power"})[menuButton.item]
                            ink: uiTheme.themeColors.text
                        }
                    }
                    Text {
                        anchors.left: parent.left; anchors.leftMargin: 50 * window.toolScale
                        anchors.right: parent.right; anchors.rightMargin: 30 * window.toolScale; anchors.verticalCenter: parent.verticalCenter
                        text: menuButton.text; color: uiTheme.themeColors.text; font.pixelSize: 18 * window.toolScale
                        wrapMode: Text.WordWrap
                    }
                }
                onClicked: window.activateMenu(item)
                NotificationBadge { count: window.updateAvailable && item === "Settings" ? 1 : 0; x: parent.width - width - 10; anchors.verticalCenter: parent.verticalCenter }
            }
            MenuButton { item: "Settings" }
            MenuButton { item: "Minimize" }
            MenuButton { item: "Quit" }
            Text {
                width: parent.width; text: window.hints.back + qsTranslate("NGS", " — Back")
                horizontalAlignment: Text.AlignHCenter; color: uiTheme.themeColors.muted; font.pixelSize: 14 * window.toolScale
            }
        }
    }

    RunningGame {
        id: runningDashboard
        objectName: "playingBackground"
        anchors.fill: parent
        visible: window.gameInProgress || window.bridge.launchingGame
        opacity: window.launchPending ? Math.min(1, launchTile.progress * 1.5) : 1
        launching: window.bridge.launchingGame
        launchCard: launchTile.card
        contentReveal: window.launchPending ? Math.max(0, Math.min(1, launchTile.progress - 1)) : 1
        bridge: window.bridge
        uiScale: window.uiScale
    }

    Popup {
        id: frozenScreen
        objectName: "frozenScreen"
        visible: (window.bridge.gameFrozen || !!window.bridge.powerError) && !window.frozenLibraryVisible
        anchors.centerIn: parent
        width: window.width; height: window.height
        modal: true; focus: true; padding: 0
        closePolicy: Popup.NoAutoClose
        background: null
        Overlay.modal: Rectangle { color: uiTheme.themeColors.scrim }
        contentItem: RunningGame {
            id: frozenDashboard
            objectName: "frozenDashboard"
            bridge: window.bridge
            launchCard: launchTile.card
            uiScale: window.uiScale
        }
    }
}
