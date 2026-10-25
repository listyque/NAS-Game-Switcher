"""Bounded JWE messages for the portable player account endpoint (RFC 7516)."""
import base64
import hashlib
import json
import re
import secrets
import time
import uuid

from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

PATH = '/tactic/ngs_launcher/ngs-players/v1/accounts'
PUBLIC_PATH = '/assets/ngs_launcher/players-public.json'
DOMAIN = b'NGS-Players-key-v1\n'
MAX_WIRE = 32768


class SessionExpired(ValueError):
    pass


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def decode(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9_-]*', value):
        raise ValueError('Invalid encoding')
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf8')


def key_id(public):
    return hashlib.sha256(RSA.import_key(public).public_key().export_key(format='DER')).hexdigest()


def seal(payload, key, wrapped='', kid=''):
    header = {'alg': 'RSA-OAEP-256' if wrapped else 'dir', 'enc': 'A256GCM', 'typ': 'NGS-Players', 'kid': kid}
    aad = encode(compact(header))
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(aad.encode('ascii'))
    body = compact(payload)
    if len(body) > 20000: raise ValueError('Message too large')
    data, tag = cipher.encrypt_and_digest(body)
    return '.'.join((aad, wrapped, encode(nonce), encode(data), encode(tag))).encode('ascii')


def open_message(wire, key, kid, private=False):
    if not isinstance(wire, bytes) or not 0 < len(wire) <= MAX_WIRE: raise ValueError('Invalid message size')
    parts = wire.decode('ascii').split('.')
    if len(parts) != 5: raise ValueError('Invalid JWE')
    aad, wrapped, iv, body, tag = parts
    expected = {'alg': 'RSA-OAEP-256' if private else 'dir', 'enc': 'A256GCM', 'typ': 'NGS-Players', 'kid': kid}
    if json.loads(decode(aad)) != expected or bool(wrapped) != private: raise ValueError('Invalid JWE header')
    if private:
        encrypted = decode(wrapped)
        rsa = RSA.import_key(key)
        if len(encrypted) != rsa.size_in_bytes(): raise ValueError('Invalid wrapped key')
        key = PKCS1_OAEP.new(rsa, hashAlgo=SHA256).decrypt(encrypted)
    nonce, signature = decode(iv), decode(tag)
    if len(key) != 32 or len(nonce) != 12 or len(signature) != 16: raise ValueError('Invalid JWE parameters')
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce, mac_len=16)
    cipher.update(aad.encode('ascii'))
    value = json.loads(cipher.decrypt_and_verify(decode(body), signature))
    if not isinstance(value, dict): raise ValueError('Invalid payload')
    return value, key


def request(public, payload):
    key, identity = secrets.token_bytes(32), uuid.uuid4().hex
    kid = key_id(public)
    wrapped = encode(PKCS1_OAEP.new(RSA.import_key(public), hashAlgo=SHA256).encrypt(key))
    return seal(dict(payload, request_id=identity, timestamp=int(time.time())), key, wrapped, kid), key, identity


def username(value):
    if not isinstance(value, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{2,31}', value):
        raise ValueError("Username: 3–32 Latin letters, digits, periods, hyphens or underscores.")
    return value.lower()


def password_hash(password, salt):
    if not isinstance(password, str) or not 8 <= len(password) <= 128 or len(password.encode('utf8')) > 512:
        raise ValueError("The password must contain 8 to 128 characters.")
    return hashlib.scrypt(password.encode('utf8'), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=64*1024*1024).hex()


def session_row(row, now):
    fields = {'id', 'game', 'name', 'started_at', 'updated_at', 'ended_at', 'active_ms', 'paused_ms'}
    if not isinstance(row, dict) or set(row) != fields: raise ValueError('Invalid session fields')
    if not re.fullmatch('[0-9a-f]{32}', row['id']) or not re.fullmatch('[a-z0-9][a-z0-9_-]{0,95}', row['game']):
        raise ValueError('Invalid session identity')
    if not isinstance(row['name'], str) or not 1 <= len(row['name']) <= 256 or any(ord(c) < 32 for c in row['name']):
        raise ValueError('Invalid game name')
    for field in ('started_at', 'updated_at', 'active_ms', 'paused_ms'):
        if type(row[field]) is not int or row[field] < 0: raise ValueError('Invalid session counter')
    if not 946684800 <= row['started_at'] <= row['updated_at'] <= now + 300: raise ValueError('Invalid session dates')
    if row['ended_at'] is not None and (type(row['ended_at']) is not int or not row['started_at'] <= row['ended_at'] <= row['updated_at']):
        raise ValueError('Invalid session end')
    if row['active_ms'] + row['paused_ms'] > (row['updated_at'] - row['started_at'] + 300) * 1000:
        raise ValueError('Invalid session duration')
    return row
