"""Exact mixed-endian GUID decode used by the measured Forms JavaScript."""

import base64
import binascii
import re
import uuid

from .errors import FormsError

ORIGIN = "https://forms.cloud.microsoft"


def decode_form_id(form_id: str) -> tuple[str, str, str]:
    if not isinstance(form_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", form_id):
        raise FormsError("invalid_form_id", "Expected a base64url form ID, not a URL.")
    try:
        raw = base64.b64decode(form_id + "=" * (-len(form_id) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise FormsError("invalid_form_id", "Malformed base64url form ID.") from exc
    if len(raw) <= 32:
        raise FormsError(
            "invalid_form_id", "Form ID must contain organization, owner and table IDs."
        )
    tail = raw[32:].decode("latin1").rstrip(".")
    extra = {}
    for part in tail.split("$%@#")[1:]:
        pair = part.split("=")
        if len(pair) > 1:
            extra[pair[0]] = pair[1]
    return (
        str(uuid.UUID(bytes_le=raw[:16])),
        str(uuid.UUID(bytes_le=raw[16:32])),
        ("groups" if extra.get("t") == "g" else "users"),
    )


def api_root(form_id: str) -> str:
    tenant, owner, collection = decode_form_id(form_id)
    return f"{ORIGIN}/formapi/api/{tenant}/{collection}/{owner}"
