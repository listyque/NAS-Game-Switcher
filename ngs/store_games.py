"""Read installed store manifests and registry entries, without changing store files."""
from ngs.i18n import tr
import json
import os
import re
import threading
from pathlib import Path

from ngs.install import check_cancel, safe_path, LocalLibrary
from ngs.portable import path_key
from ngs.scanner import local_folder, scan_executables, normalized_name
from ngs.bulk_scan import metadata_requests, title_key
from ngs.catalog import CatalogCache, relative_game_path


MAX_MANIFEST = 2 * 1024 * 1024


def registry_rows(key):
    if os.name != 'nt': return []
    import winreg
    rows = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as root:
                    keys = ['']
                    for index in range(min(4096, winreg.QueryInfoKey(root)[0])):
                        keys.append(winreg.EnumKey(root, index))
                    for name in keys:
                        try:
                            with winreg.OpenKey(root, name) as entry:
                                values = {'key': name}
                                for index in range(min(128, winreg.QueryInfoKey(entry)[1])):
                                    field, value, kind = winreg.EnumValue(entry, index)
                                    if isinstance(value, str): values[field.casefold()] = value
                                rows.append(values)
                        except OSError: continue
            except OSError: continue
    return rows


def manifest(path, *, vdf=False):
    path = Path(path)
    if not path.parent.exists():
        raise FileNotFoundError(path.parent)
    parent = local_folder(path.parent)
    path = safe_path(parent, path.name)
    with path.open('rb') as stream: raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST: raise ValueError(tr("The store manifest is too large."))
    text = raw.decode('utf-8-sig')
    if not vdf:
        value = json.loads(text)
        if not isinstance(value, dict): raise ValueError(tr("Invalid store manifest."))
        return value
    tokens = re.finditer(r'"((?:\\.|[^"\\])*)"|([{}])|//[^\n]*', text)
    root, stack, pending = {}, [], None
    current = root
    for token in tokens:
        word, bracket = token.groups()
        if word is None and bracket is None: continue
        if bracket == '{':
            if pending is None or len(stack) >= 16: raise ValueError(tr("Invalid Steam VDF."))
            child = {}; current[pending] = child; stack.append(current); current = child; pending = None
        elif bracket == '}':
            if pending is not None or not stack: raise ValueError(tr("Invalid Steam VDF."))
            current = stack.pop()
        else:
            word = re.sub(r'\\(["\\])', r'\1', word)
            if pending is None: pending = word
            else: current[pending] = word; pending = None
    if stack or pending is not None: raise ValueError(tr("Incomplete Steam VDF."))
    return root


def manifest_files(folder, pattern, cancel):
    if not Path(folder).exists():
        raise FileNotFoundError(folder)
    folder = local_folder(folder)
    result = []
    with os.scandir(folder) as entries:
        for index, entry in enumerate(entries):
            check_cancel(cancel)
            if index >= 20000: raise ValueError(tr("Too many store manifests."))
            if re.fullmatch(pattern, entry.name, re.I): result.append(Path(entry.path))
    return sorted(result)


def gog_launch_task(folder, task):
    """Keep the primary task as a validated launch specification, not just its EXE."""
    from ngs.process import split_launch_arguments
    executable = task.get('path')
    working = task.get('workingDir', '')
    if not isinstance(executable, str) or not isinstance(working, str):
        raise ValueError(tr("Invalid GOG main task paths."))
    executable = relative_game_path(executable.replace('\\', '/'))
    working = working.replace('\\', '/')
    working = relative_game_path('' if working == '.' else working, allow_empty=True)
    root = local_folder(folder)
    if not safe_path(root, executable).is_file() or not executable.lower().endswith('.exe'):
        raise ValueError(tr("The GOG main executable is unavailable."))
    if not safe_path(root, working).is_dir():
        raise ValueError(tr("The GOG working directory is unavailable."))
    arguments = split_launch_arguments(task.get('arguments', ''))
    return dict(executable=executable, arguments=arguments, working_directory=working)


def installed_store_games(cancel=None):
    cancel = cancel or threading.Event()
    rows, warnings, seen = [], [], set()
    def add(store, name, location, identity='', executable='', launch=None):
        if not isinstance(location, str) or not location: return
        folder = local_folder(os.path.expandvars(location))
        if folder == Path(folder.anchor) or path_key(folder) in seen: return
        preferred = ''
        if isinstance(executable, str) and executable:
            try:
                exe = Path(executable.strip('"'))
                relative = exe.relative_to(folder).as_posix() if exe.is_absolute() else executable.replace('\\', '/')
                file = safe_path(folder, relative)
                if file.is_file() and file.suffix.casefold() == '.exe': preferred = relative
            except (ValueError, OSError): pass
        title = name.strip()[:160] if isinstance(name, str) else ''
        if not title or '\0' in title: title = folder.name[:160]
        seen.add(path_key(folder))
        rows.append(dict(store=store, name=title, path=str(folder), store_id=str(identity)[:160],
                         preferred_exe=preferred, state='found', status=store, candidates=[]))
        if launch is not None:
            rows[-1]['store_launch'] = launch
    def attempt(callback):
        check_cancel(cancel)
        try: callback()
        except FileNotFoundError: pass
        except (ValueError, OSError, KeyError, TypeError, UnicodeError) as error:
            warnings.append(str(error)[:240])

    steam = registry_rows(r'Software\Valve\Steam')
    roots = [row.get('steampath') or row.get('installpath') for row in steam]
    for key in ('ProgramFiles(x86)', 'ProgramFiles'):
        if os.environ.get(key):
            try: roots.append(str(local_folder(Path(os.environ[key])/'Steam')))
            except (ValueError, OSError): pass
    libraries = {}
    for location in roots:
        if not location: continue
        def steam_root(location=location):
            root = local_folder(location); libraries[path_key(root)] = root
            document = manifest(root/'steamapps/libraryfolders.vdf', vdf=True)
            data = next((value for key,value in document.items() if key.casefold() == 'libraryfolders'), {})
            if not isinstance(data, dict): raise ValueError(tr("Invalid Steam library list."))
            for key, value in data.items():
                if not key.isdigit(): continue
                path = value.get('path') if isinstance(value, dict) else value
                if isinstance(path, str):
                    attempt(lambda path=path: libraries.setdefault(path_key(path), local_folder(path)))
        attempt(steam_root)
    for library in libraries.values():
        def steam_library(library=library):
            for path in manifest_files(library/'steamapps', r'appmanifest_[0-9]+\.acf', cancel):
                def steam_game(path=path):
                    app = manifest(path, vdf=True)['AppState']
                    if not isinstance(app, dict): raise ValueError(tr("Invalid Steam game entry."))
                    if str(app['appid']) != path.stem.split('_')[1] or not int(app.get('StateFlags', '4')) & 4: return
                    folder = safe_path(library/'steamapps/common', app['installdir'])
                    add('Steam', app.get('name'), str(folder), app['appid'])
                attempt(steam_game)
        attempt(steam_library)

    for row in registry_rows(r'Software\GOG.com\Games'):
        if not row.get('key'): continue
        def gog_game(row=row):
            folder = row.get('path') or row.get('installlocation')
            exe, name = row.get('exe', ''), row.get('gamename', '')
            primary_task = None
            if folder:
                info = local_folder(folder)/('goggame-'+row['key']+'.info')
                if info.is_file():
                    try:
                        data = manifest(info); name = data.get('name') or name
                        tasks = data.get('playTasks', [])
                        primary = [task for task in tasks if isinstance(task, dict) and task.get('isPrimary') and task.get('type') == 'FileTask'] if isinstance(tasks, list) else []
                        if len(primary) == 1: primary_task = primary[0]
                    except (ValueError, OSError, TypeError): pass
            # An invalid task must not silently turn into a different command.
            launch = gog_launch_task(folder, primary_task) if primary_task is not None else None
            if launch is not None: exe = launch['executable']
            add('GOG', name, folder, row['key'], exe, launch)
        attempt(gog_game)

    program_data = os.environ.get('ProgramData')
    if program_data:
        epic = Path(program_data)/'Epic'
        def epic_manifests():
            for path in manifest_files(epic/'EpicGamesLauncher/Data/Manifests', r'.*\.item', cancel):
                def epic_game(path=path):
                    app = manifest(path)
                    if app.get('bIsIncompleteInstall') or str(app.get('AppName', '')).startswith('UE_'): return
                    add('Epic Games Store', app.get('DisplayName'), app.get('InstallLocation'), app.get('AppName', ''), app.get('LaunchExecutable', ''))
                attempt(epic_game)
        attempt(epic_manifests)
        def epic_list():
            for app in manifest(epic/'UnrealEngineLauncher/LauncherInstalled.dat')['InstallationList']:
                if isinstance(app, dict) and not str(app.get('AppName', '')).startswith('UE_'):
                    attempt(lambda app=app: add('Epic Games Store', app.get('DisplayName'), app.get('InstallLocation'), app.get('AppName', '')))
        attempt(epic_list)

    for row in registry_rows(r'Software\Microsoft\Windows\CurrentVersion\Uninstall'):
        name, command = row.get('displayname', ''), row.get('uninstallstring', '')
        if name.casefold() in ('battle.net', 'battle.net launcher', 'blizzard battle.net'): continue
        classic = any(word in name.casefold() for word in ('diablo', 'warcraft', 'starcraft', 'hearthstone', 'overwatch', 'heroes of the storm'))
        if ('battle.net' in command.casefold() and '--uid=' in command.casefold()
                or 'blizzard' in row.get('publisher', '').casefold() and classic):
            icon = re.sub(r',\s*-?[0-9]+$', '', row.get('displayicon', '')).strip('"')
            attempt(lambda row=row, icon=icon: add('Battle.net', row.get('displayname'), row.get('installlocation'), row['key'], icon))
    return dict(rows=sorted(rows, key=lambda row: (row['store'], row['name'].casefold())), warnings=warnings)


def has_store_artwork(card, cache=None):
    return bool(card.get('cover') and card.get('background') and card.get('description')
                and (cache is None or all(cache.image_valid(card[key]) for key in ('cover', 'background'))))


def store_game_card(row, cards, metadata):
    reference = str(row.get('store_id', ''))
    if row['store'] == 'GOG' and re.fullmatch(r'[1-9][0-9]{0,19}', reference): reference = 'gog:'+reference
    elif row['store'] != 'Steam' or not re.fullmatch(r'[1-9][0-9]{0,9}', reference): reference = ''
    if reference:
        identity = 'steam-'+reference if row['store'] == 'Steam' else reference.replace(':','-',1)
        matches = [card for card in cards if card['id'] == identity or row['store'] == 'Steam' and
                   card.get('metadata_url', '').rstrip('/') == 'https://store.steampowered.com/app/'+reference]
    else:
        matches = [card for card in cards if title_key(card['name']) == title_key(row['name'])]
    card = row.get('metadata') or (matches[0] if len(matches) == 1 else None)
    if card and has_store_artwork(card): return card, ''
    if metadata is None: return None, tr("No cached artwork; connect to the server.")
    try:
        if reference:
            try:
                return metadata('resolve', reference, row['name'])['card'], ''
            except ConnectionError: pass
            except OSError as error: return None, str(error)
            except RuntimeError as error:
                if getattr(error, 'status', None) not in (400, 404, 502, 503): raise
        found = metadata('search', row['name'], row['name'])
        exact = [match for match in found if title_key(match['name']) == title_key(row['name'])]
        if len(exact) == 1:
            return metadata('resolve', exact[0]['app_id'], row['name'])['card'], ''
        return None, tr("Choose artwork manually: ") + (tr("multiple matches.") if exact else tr("no exact match found."))
    except OSError as error:
        return None, str(error)
    except RuntimeError as error:
        if getattr(error, 'status', None) not in (400, 404, 502, 503): raise
        return None, str(error)


def import_store_games(storage, rows, cards, cancel, emit, *, url='', offline=False):
    library = LocalLibrary(storage)
    locations = {path_key(library.install_path(item)):item for item in library.reconcile()}
    metadata = metadata_requests(storage, url, cancel, emit, offline=offline) if url else None
    cache = CatalogCache(storage, url) if url else None
    result = []
    for index, original in enumerate(rows):
        check_cancel(cancel)
        row = dict(original)
        try:
            existing = locations.get(path_key(row['path']))
            if existing and (existing.get('kind') != 'external' or has_store_artwork(existing['card'], cache)):
                row.update(state='already', status=tr("Already in My games"), game_id=existing['card']['id'])
            else:
                emit(dict(status=tr("Checking: ")+row['name'], done=index, total=len(rows)))
                matches = [card for card in cards if normalized_name(card['name']) == normalized_name(row['name'])]
                card = matches[0] if len(matches) == 1 else None
                preferred = existing['executable'] if existing else row.get('preferred_exe') or (card.get('executable', '') if card else '')
                if preferred and safe_path(local_folder(row['path']), preferred).is_file():
                    found = dict(candidates=[dict(path=preferred, utility=False)], limited=False, skipped=0)
                else: found = scan_executables(row['path'], cancel)
                choices = [item for item in found['candidates'] if not item['utility']]
                row['candidates'] = found['candidates']
                if found['limited'] or found['skipped'] or len(choices) != 1:
                    row.update(state='manual', status=tr("Choose an executable manually"))
                else:
                    emit(dict(status=tr("Loading cover and description: ")+row['name']))
                    card, message = store_game_card(row, cards, metadata)
                    if card and cache and not offline and not has_store_artwork(card, cache):
                        try: cache.cache_artwork(card, cancel)
                        except (RuntimeError, OSError) as error: message = str(error)
                    check_cancel(cancel)
                    if existing:
                        receipt = library.update_external_metadata(existing['card']['id'], card) if card else existing
                    else:
                        launch = row.get('store_launch', {})
                        options = {}
                        # A manually selected different EXE must not inherit another task's flags.
                        if launch and launch.get('executable', '').casefold() == choices[0]['path'].casefold():
                            options = dict(arguments=launch['arguments'], working_directory=launch['working_directory'])
                        receipt = library.register_external(row['path'], choices[0]['path'], row['name'], card, cancel, **options)
                    locations[path_key(row['path'])] = receipt
                    ready = card and has_store_artwork(card, cache)
                    row.update(state='added' if ready else 'artwork', status=tr("Cover and description added") if ready else tr("Artwork needed · ")+(message or tr("Connect to the server and try again.")), game_id=receipt['card']['id'])
        except (ValueError, OSError, RuntimeError) as error: row.update(state='error', status=str(error))
        result.append(row)
        emit(dict(row=row, done=index+1, total=len(rows), status=tr("Checked %d of %d · added %d") %
                  (index+1, len(rows), sum(item['state'] == 'added' for item in result))))
    return dict(rows=result)
