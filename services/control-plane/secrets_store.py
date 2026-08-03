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


def decrypt_value(blob: str) -> str:
    try:
        return _get_fernet().decrypt(blob.encode()).decode()
    except InvalidToken:
        logger.error("Failed to decrypt a workspace secret (SECRETS_MASTER_KEY changed?)")
        return ""
