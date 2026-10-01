"""Preserve dotenv bytes while migrating legacy keys and exact runtime paths.

This helper never reads credentials from the process environment, prints values,
or changes trading/provider settings. The caller supplies the official runtime
rename table and owns backups plus atomic replacement of the resulting bytes.
"""
from __future__ import annotations

import re

_ASSIGNMENT = re.compile(
    rb"^([ \t]*(?:export[ \t]+)?)([A-Za-z_][A-Za-z0-9_]*)([ \t]*=[ \t]*)([^\r\n]*)(\r?\n)?$"
)
_PATH_KEYS = {
    b"ASTRA_ADMIN_DB", b"ASTRA_QUANT_DB", b"ASTRA_GATEWAY_DB",
    b"ASTRA_AUDIT_FILE", b"ASTRA_RISK_RESERVATION_DB",
    b"ASTRA_SELF_IMPROVEMENT_LOG",
}


def _remap_path(raw: bytes, runtime_names: tuple[tuple[str, str], ...]) -> bytes:
    # Only an exact path value is eligible. Keep quotes, whitespace and comments.
    match = re.fullmatch(rb"([ \t]*)(['\"]?)([^'\" \t]+)\2([ \t]*(?:#.*)?)", raw)
    if not match:
        return raw
    lead, quote, value, tail = match.groups()
    for old, new in runtime_names:
        for prefix in ("", "/app/", "./"):
            if value == (prefix + old).encode():
                return lead + quote + (prefix + new).encode() + quote + tail
    return raw


def migrate_env_bytes(original: bytes, runtime_names: tuple[tuple[str, str], ...]) -> bytes:
    """Rename only assignment keys; refuse conflicting destination definitions."""
    result = []
    definitions: dict[bytes, bytes] = {}
    for line in original.splitlines(keepends=True):
        match = _ASSIGNMENT.fullmatch(line)
        if not match:
            result.append(line)
            continue
        lead, key, separator, value, newline = match.groups()
        target = b"ASTRA_" + key[4:] if key.startswith(b"R20_") else key
        migrated_value = _remap_path(value, runtime_names) if target in _PATH_KEYS else value
        if target in definitions and definitions[target] != migrated_value:
            raise ValueError(f"Conflicting dotenv definitions for {target.decode('ascii')}")
        definitions[target] = migrated_value
        result.append(lead + target + separator + migrated_value + (newline or b""))
    return b"".join(result)
