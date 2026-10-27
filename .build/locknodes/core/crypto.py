"""Password hashing and authenticated blob encryption.

Pure stdlib, no bpy — testable outside Blender. Blender does not bundle
the 'cryptography' package, so the cipher is an HMAC-SHA256 CTR keystream
(a sound PRF-based stream cipher) with encrypt-then-MAC authentication.
The blob header is versioned so the format can migrate to AES-GCM via
bundled wheels later without breaking existing locks.
"""

import base64
import hashlib
import hmac
import json
import secrets
import zlib

MAGIC = b"LKN1"
DEFAULT_ITERS = 600_000
SALT_LEN = 16
NONCE_LEN = 16
TAG_LEN = 32
_BLOCK = hashlib.sha256().digest_size  # 32


class TamperError(Exception):
    """Blob failed authentication — corrupted or tampered with."""


class BlobFormatError(Exception):
    """Blob is not a parseable LockNodes blob."""


def hash_password(password: str, salt: bytes, iters: int = DEFAULT_ITERS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=32)


def make_verify_pair(password: str, iters: int = DEFAULT_ITERS) -> tuple[str, str]:
    """Fresh (salt_b64, hash_b64) for storing alongside locked data."""
    salt = secrets.token_bytes(SALT_LEN)
    digest = hash_password(password, salt, iters)
    return _b64e(salt), _b64e(digest)


def verify_password(password: str, salt_b64: str, hash_b64: str, iters: int = DEFAULT_ITERS) -> bool:
    try:
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(hash_password(password, salt, iters), expected)


def _derive_keys(password: str, salt: bytes, iters: int) -> tuple[bytes, bytes]:
    # One derivation, split into independent encryption and MAC keys.
    okm = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, dklen=64)
    return okm[:32], okm[32:]


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    # HMAC(enc_key, nonce || counter) blocks — a fresh block per counter,
    # never a single block repeated.
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(enc_key, nonce + counter.to_bytes(8, "big"), "sha256").digest()
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def encrypt_blob(plaintext: str, password: str, iters: int = DEFAULT_ITERS) -> str:
    data = zlib.compress(plaintext.encode("utf-8"), 9)
    enc_salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    enc_key, mac_key = _derive_keys(password, enc_salt, iters)

    header = json.dumps(
        {
            "v": 1,
            "kdf": "pbkdf2_sha256",
            "iters": iters,
            "salt": _b64e(enc_salt),
            "nonce": _b64e(nonce),
        },
        sort_keys=True,
    ).encode("utf-8")

    ct = _xor(data, _keystream(enc_key, nonce, len(data)))
    # Header is authenticated too — no KDF-parameter downgrade tampering.
    tag = hmac.new(mac_key, header + nonce + ct, "sha256").digest()

    raw = MAGIC + len(header).to_bytes(4, "big") + header + ct + tag
    return _b64e(raw)


def decrypt_blob(blob_b64: str, password: str) -> str:
    try:
        raw = _b64d(blob_b64)
    except (ValueError, TypeError) as exc:
        raise BlobFormatError("blob is not valid base64") from exc

    if len(raw) < len(MAGIC) + 4 + TAG_LEN or raw[: len(MAGIC)] != MAGIC:
        raise BlobFormatError("missing LKN1 magic")

    offset = len(MAGIC)
    header_len = int.from_bytes(raw[offset : offset + 4], "big")
    offset += 4
    if offset + header_len + TAG_LEN > len(raw):
        raise BlobFormatError("truncated blob")

    header = raw[offset : offset + header_len]
    ct = raw[offset + header_len : -TAG_LEN]
    tag = raw[-TAG_LEN:]

    try:
        meta = json.loads(header.decode("utf-8"))
        iters = int(meta["iters"])
        enc_salt = _b64d(meta["salt"])
        nonce = _b64d(meta["nonce"])
        if meta.get("v") != 1 or meta.get("kdf") != "pbkdf2_sha256":
            raise BlobFormatError("unsupported blob version or KDF")
    except BlobFormatError:
        raise
    except (KeyError, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise BlobFormatError("unparseable blob header") from exc

    enc_key, mac_key = _derive_keys(password, enc_salt, iters)
    expected = hmac.new(mac_key, header + nonce + ct, "sha256").digest()
    if not hmac.compare_digest(expected, tag):
        raise TamperError("authentication failed")

    data = _xor(ct, _keystream(enc_key, nonce, len(ct)))
    try:
        return zlib.decompress(data).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as exc:
        # Tag verified, so this should be unreachable — defensive only.
        raise TamperError("decryption produced invalid data") from exc


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)
