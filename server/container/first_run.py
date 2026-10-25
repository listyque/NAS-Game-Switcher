"""Provision the NAS owner once, before accepting any client registration."""
import json
import os
import secrets
import uuid
from pathlib import Path


def create_owner(connection, password):
    from ngs_launcher_plugin.backend_players import password_hash
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(1313297225)')
        cursor.execute('SELECT COUNT(*) FROM player_profile')
        if cursor.fetchone()[0]: return False
        salt = secrets.token_hex(16)
        profile = dict(id=uuid.uuid4().hex, username='admin', salt=salt,
                       password=password_hash(password, salt), role='owner', enabled=True)
        cursor.execute('INSERT INTO player_profile (code,data) VALUES (%s,%s)',
                       ('admin', json.dumps(profile, separators=(',', ':'))))
    return True


def main():
    import psycopg2
    secret_dir = Path(os.environ.get('NGS_SECRETS_DIR', '/run/ngs-secrets'))
    admin_password = (secret_dir / 'tactic_admin_password').read_text().strip()
    with psycopg2.connect(host='db', user='postgres', dbname='ngs_launcher',
                          password=(secret_dir / 'postgres_password').read_text().strip(), connect_timeout=10) as connection:
        created = create_owner(connection, admin_password)
    print('NGS owner profile initialized.' if created else 'NGS profiles preserved.', flush=True)
    authority = Path('/opt/tactic/assets/ngs_launcher/server-public-key.bin')
    if authority.is_file():
        import hashlib
        fingerprint = hashlib.sha256(authority.read_bytes()).hexdigest()
        print('NGS local pairing code: ' + '-'.join(fingerprint[i:i+4] for i in range(0,64,4)), flush=True)


if __name__ == '__main__': main()
