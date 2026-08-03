"""Encryption at rest for workspace secrets (Fernet).

The master key comes from ``SECRETS_MASTER_KEY`` (a Fernet key, i.e. 32
url-safe base64 bytes). If it is unset, an ephemeral key is generated so
local dev works — but stored secrets become unreadable after a restart, so
production and Compose must set it.
"""

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("control-plane.secrets")
# At-rest encryption for non-workspace values (canvas keys) MUST only engage
# when the master key is stable: the ephemeral per-process fallback would
# leave the value undecryptable after a restart. Workspace secrets keep
# their existing always-encrypted behavior (ephemeral key in dev).
SECRETS_STABLE = bool(os.environ.get("SECRETS_MASTER_KEY"))

_fernet: Fernet | None = None
_warned = False


def _get_fernet() -> Fernet:
    global _fernet, _warned
    if _fernet is None:
        raw = os.environ.get("SECRETS_MASTER_KEY", "")
        if raw:
            _fernet = Fernet(raw.encode())
        else:
            if not _warned:
                logger.warning(
                    "SECRETS_MASTER_KEY not set — using an ephemeral key; "
                    "stored secrets will be unreadable after restart"
                )
                _warned = True
            _fernet = Fernet(Fernet.generate_key())
    return _fernet


def encrypt_value(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


class SecretDecryptionError(Exception):
    """A stored secret cannot be decrypted with the current SECRETS_MASTER_KEY
    (rotated or lost). Callers surface this as a loud failure (HTTP 500)."""


def decrypt_value(blob: str) -> str:
    try:
        return _get_fernet().decrypt(blob.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt a workspace secret (SECRETS_MASTER_KEY changed?)")
        raise SecretDecryptionError(
            "Stored secret cannot be decrypted with the current "
            "SECRETS_MASTER_KEY (key rotated or lost)"
        ) from None

# Every Fernet token begins with this fixed prefix (version byte 0x80 +
# epoch timestamp). It doubles as an at-rest marker: stored values without
# it are legacy plaintext (written before canvas-key encryption existed).
FERNET_PREFIX = "gAAAAA"


def encrypt_if_stable(value: str) -> str:
    """Encrypt ``value`` for storage when SECRETS_MASTER_KEY is stable;
    return plaintext under the ephemeral dev fallback (which cannot be
    decrypted after a restart)."""
    if SECRETS_STABLE:
        return encrypt_value(value)
    return value


def decrypt_value_if_encrypted(value: str) -> str:
    """Return the plaintext for a stored value.

    Fernet tokens (``gAAAAA`` prefix) are decrypted — raising
    SecretDecryptionError when they cannot be decrypted with the current
    key (callers must fail loudly rather than use a ciphertext). Any other
    value is treated as legacy plaintext (pre-encryption rows) and returned
    unchanged.
    """
    if not value.startswith(FERNET_PREFIX):
        return value
    return decrypt_value(value)
