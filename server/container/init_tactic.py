"""Initialize only a fresh TACTIC 5.0/PostgreSQL installation."""
import os
import shutil
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import psycopg2
from psycopg2 import sql


ROOT = Path('/opt/tactic/tactic')
DATA = Path('/opt/tactic/tactic_data')
SECRETS = Path(os.environ.get('NGS_SECRETS_DIR', '/run/secrets'))
PASSWORD = (SECRETS / 'postgres_password').read_text().strip()
ADMIN_PASSWORD = (SECRETS / 'tactic_admin_password').read_text().strip()


def configure():
    config = DATA / 'config/tactic-conf.xml'
    created = not config.exists()
    if created:
        template = ROOT / 'src/install/template/config/tactic_linux-conf_python3.xml'
        tree = ET.parse(template)
        for section, key, value in (
            ('install', 'tmp_dir', str(DATA / 'temp')),
            ('install', 'log_type', 'stream'),
            ('database', 'server', 'db'),
            ('database', 'port', '5432'),
            ('database', 'user', 'postgres'),
            ('database', 'password', PASSWORD),
            ('checkin', 'asset_base_dir', '/opt/tactic/assets'),
        ):
            tree.find(f'./{section}/{key}').text = value
    else:
        tree = ET.parse(config)
    changed = False
    for section, key, value in (
        ('install', 'hostname', '127.0.0.1'),
        ('services', 'process_count', '2'),
        ('services', 'start_port', '8082'),
    ):
        element = tree.find(f'./{section}/{key}')
        if element is None:
            element = ET.SubElement(tree.find(f'./{section}'), key)
        if element.text != value:
            element.text = value
            changed = True
    protocol = tree.find('./security/protocol')
    if protocol is None:
        protocol = ET.SubElement(tree.find('./security'), 'protocol')
    expected_protocol = 'https' if os.environ.get('NGS_PUBLIC_DOMAIN') else 'http'
    if protocol.text != expected_protocol:
        protocol.text = expected_protocol
        changed = True
    if changed and not created:
        with tempfile.NamedTemporaryFile(dir=config.parent, delete=False) as output:
            tree.write(output, encoding='utf-8', xml_declaration=True)
        Path(output.name).replace(config)
    if created:
        fd = os.open(str(config), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            tree.write(output, encoding='utf-8', xml_declaration=True)
    license_path = DATA / 'config/tactic-license.xml'
    if not license_path.exists():
        shutil.copyfile(ROOT / 'src/install/template/config/tactic-license.xml', license_path)
    templates = DATA / 'templates'
    if not templates.exists():
        shutil.copytree(ROOT / 'src/install/start/templates', templates)
    (DATA / 'dist').mkdir(exist_ok=True)


def connect(database):
    return psycopg2.connect(host='db', port=5432, user='postgres',
                            password=PASSWORD, dbname=database, connect_timeout=5)


def can_resume_incomplete_install():
    import tacticenv  # noqa: F401; sets the TACTIC import paths
    from pyasm.security.drupal_password_hasher import DrupalPasswordHasher

    with connect('sthpw') as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.login'), to_regclass('public.project'), "
                           "to_regclass('public.login_group'), to_regclass('public.login_in_group')")
            if not all(cursor.fetchone()):
                return False
            cursor.execute('SELECT password FROM "login" WHERE "login" = %s', ('admin',))
            admin = cursor.fetchone()
            cursor.execute('SELECT count(*) FROM "login"')
            if cursor.fetchone()[0] != 1:
                return False
            cursor.execute('SELECT code FROM project ORDER BY code')
            if [row[0] for row in cursor.fetchall()] != ['admin', 'sthpw']:
                return False
            cursor.execute('SELECT 1 FROM login_group WHERE login_group = %s', ('admin',))
            if not cursor.fetchone():
                return False
            cursor.execute('SELECT 1 FROM login_in_group WHERE "login" = %s AND login_group = %s',
                           ('admin', 'admin'))
            if not cursor.fetchone():
                return False
    return bool(admin and DrupalPasswordHasher().verify(ADMIN_PASSWORD, admin[0]))


def main():
    configure()
    for attempt in range(30):
        try:
            server = connect('postgres')
            break
        except psycopg2.OperationalError:
            if attempt == 29:
                raise
            time.sleep(2)
    server.autocommit = True
    with server.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = 'sthpw'")
        exists = bool(cursor.fetchone())
    marker = DATA / '.initialized'
    if marker.exists():
        if not exists:
            raise SystemExit('TACTIC data volume is initialized, but sthpw database is missing.')
        with connect('sthpw') as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.login')")
                if cursor.fetchone()[0] is None:
                    raise SystemExit('sthpw database has no login table.')
        print('Existing TACTIC database found.')
        return
    if exists and not can_resume_incomplete_install():
        raise SystemExit('sthpw database already exists without this installation marker; refusing to modify it.')
    if not exists:
        with server.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier('sthpw')))
    else:
        print('Resuming incomplete TACTIC database initialization.')
    server.close()

    import tacticenv  # noqa: F401; sets the TACTIC import paths
    from pyasm.security import Login, Batch
    from pyasm.search.upgrade import Upgrade

    if not exists:
        schema_dir = ROOT / 'src/pyasm/search/upgrade/postgresql'
        db = connect('sthpw')
        try:
            with db.cursor() as cursor:
                for name in ('bootstrap', 'config', 'sthpw'):
                    cursor.execute((schema_dir / f'{name}_schema.sql').read_text(encoding='utf-8'))
                cursor.execute("INSERT INTO login_group (login_group, description) VALUES (%s, %s)",
                               ('admin', 'Site Administration'))
                cursor.execute('INSERT INTO "login" ("login", "password", "upn", first_name, last_name) '
                               'VALUES (%s, %s, %s, %s, %s)',
                               ('admin', Login.encrypt_password(ADMIN_PASSWORD), 'admin', 'Admin', ''))
                cursor.execute('INSERT INTO login_in_group ("login", login_group) VALUES (%s, %s)',
                               ('admin', 'admin'))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    Batch()
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    Upgrade(version, is_forced=True, project_code=None, quiet=True, is_confirmed=True).execute()
    marker.write_text(version + '\n', encoding='utf-8')
    print('TACTIC database initialized.')


if __name__ == '__main__':
    main()
