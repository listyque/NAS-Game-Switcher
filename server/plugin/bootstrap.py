"""Configure a new native TACTIC plugin in its existing command transaction."""

def initialize(groups):
    import base64,hashlib,json,secrets,struct
    from pathlib import Path
    from Crypto.Hash import SHA256
    from Crypto.PublicKey import RSA
    from Crypto.Signature import pkcs1_15
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import Search,SearchType,DbContainer
    from ngs_launcher_plugin.defaults import DEFAULTS
    from pyasm.security import Login
    if not Environment.get_security().is_admin() or Project.get().get_code()!='ngs_launcher':
        raise PermissionError('NGS project administrator required')
    if any(row.get('project_code')!='ngs_launcher' for row in groups):raise ValueError('Foreign plugin group')
    database=DbContainer.get(Project.get().get_project_db_resource())
    for definition in DEFAULTS:
        table,column=definition['table_name'],definition['column_name']
        if not table.replace('_','').isalnum() or not column.replace('_','').isalnum():raise ValueError('Invalid schema default')
        database.do_update('ALTER TABLE "'+table+'" ALTER COLUMN "'+column+'" SET DEFAULT '+definition['column_default'])
    workers={'metadata':'ngs_metadata_worker','catalog':'ngs_catalog_worker','players':'ngs_players_worker'}
    for table in ['game','api_device','player_profile','player_play','app_release','package_job','catalog_config','api_service']:
        q=Search('ngs_launcher/'+table);q.set_limit(1)
        if q.get_sobject():raise ValueError('NGS plugin is not empty; refusing reset')
    for login in workers.values():
        q=Search('sthpw/login');q.add_filter('login',login)
        if q.get_sobject():raise ValueError('Reserved NGS worker already exists')
    asset_root=Path(Environment.get_asset_dir()).resolve()/'ngs_launcher'
    if asset_root.is_symlink() or (asset_root.exists() and any(asset_root.iterdir())):
        raise ValueError('NGS assets already exist; refusing overwrite')
    def save(kind,values):
        obj=SearchType.create(kind)
        for key,value in values.items():
            if value is not None:obj.set_value(key,value)
        obj.commit(triggers='none',log_transaction=False)
        return obj
    root, signer, players = RSA.generate(2048), RSA.generate(2048), RSA.generate(3072)
    def blob(key):
        exponent = key.e.to_bytes(3, 'big')
        return struct.pack('<6I', 0x31415352, 2048, 3, 256, 0, 0) + exponent + key.n.to_bytes(256, 'big')
    public = blob(root)
    leaf = blob(signer)
    certificate = dict(public=base64.b64encode(leaf).decode(), signature=base64.b64encode(
        pkcs1_15.new(root).sign(SHA256.new(b'NGS-Game-Packages-Key-v1\n' + leaf))).decode())
    service = dict(private=signer.export_key().decode(), certificate=certificate, devices=[],
                   source_roots=dict(games=r'\\NGS\Games', installers=r'\\NGS\Installers'))
    save('ngs_launcher/catalog_config',dict(code='service',description='NGS remote catalogizer v1',data=json.dumps(service)))
    save('ngs_launcher/catalog_config',dict(code='known_games',description='NGS remote catalogizer v1',
        data=json.dumps(dict(schema=1,synced_at=0,games=[]))))
    save('ngs_launcher/catalog_config',dict(code='authority',description='NGS independent server authority v1',
        data=json.dumps(dict(private=root.export_key().decode(),public=base64.b64encode(public).decode()))))
    crypto = dict(private=players.export_key().decode(), public=players.public_key().export_key().decode(),
          kid=hashlib.sha256(players.public_key().export_key(format='DER')).hexdigest())
    save('ngs_launcher/player_config',dict(code='crypto',data=json.dumps(crypto)))
    save('ngs_launcher/player_config',dict(code='registration',data=json.dumps(dict(enabled=True))))
    credentials = {name: secrets.token_hex(32) for name in workers}
    for name,password in credentials.items():
        save('ngs_launcher/api_service',dict(code=name,login=workers[name],password=password))

    for row in groups:save('sthpw/login_group',{k:v for k,v in row.items() if k!='id'})
    for name,login in workers.items():
        Login.create(login,credentials[name],groups=['ngs_launcher_'+name+'_worker'],display_name='NGS '+name+' worker')
    asset_root.mkdir(parents=True,exist_ok=True)
    empty=json.dumps(dict(schema=1,project='ngs_launcher',games=[])).encode()
    for name in ['catalog.json','installers.json']:(asset_root/name).write_bytes(empty)
    try:
        import zlib
        from ngs_launcher_plugin.known_games_data import KNOWN_GAMES_ZLIB_B64
        (asset_root/'known-games.json').write_bytes(zlib.decompress(base64.b64decode(KNOWN_GAMES_ZLIB_B64)))
    except Exception:
        pass
    value=json.dumps(dict(schema=1,public=crypto['public'],kid=crypto['kid']),sort_keys=True,separators=(',',':')).encode()
    encode=lambda data:base64.urlsafe_b64encode(data).rstrip(b'=').decode()
    descriptor=dict(payload=encode(value),signature=encode(pkcs1_15.new(root).sign(SHA256.new(b'NGS-Players-key-v1\n'+value))))
    (asset_root/'players-public.json').write_text(json.dumps(descriptor),encoding='utf-8')
    (asset_root/'server-public-key.bin').write_bytes(public)
    return dict(project='ngs_launcher',installed=True,clean=True)
