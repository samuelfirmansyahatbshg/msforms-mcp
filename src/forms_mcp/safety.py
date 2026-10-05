"""Every destructive entry point must pass this gate before an API DELETE."""

import hashlib
import json

from .errors import FormsError
from .ids import api_root
from .operations import cards, find_card


def fingerprint(form):
    state = {
        "id": form["id"],
        "title": form.get("title"),
        "rowCount": form.get("rowCount"),
        "cards": [
            {k: c.get(k) for k in ("id", "type", "title", "order", "required", "questionInfo")}
            for c in cards(form)
        ],
    }
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def response_count(form):
    count = form.get("rowCount")
    if type(count) is not int or count < 0:
        raise FormsError(
            "unknown_response_count",
            "Cannot confirm deletion without a current response row count.",
        )
    return count


async def delete(
    api, workbooks, confirm, form_id, card_id=None, *, section=False, skip_backup=False
):
    before = await api.read(form_id)
    count = response_count(before)
    if card_id:
        card = find_card(before, card_id)
        if (card["type"] == "Question.ColumnGroup") != section:
            raise FormsError("wrong_card_type", "Use the matching question or section delete tool.")
        description = f"Delete {'section' if section else 'question'} {card['title']!r} ({card_id})"
    else:
        description = "Delete the entire form"
    consequence = (
        f"The form has {count} response rows. Measured section deletion preserves its questions; no question answers are intentionally deleted."
        if section
        else f"This permanently removes answer data from {count} historical response rows."
    )
    # A form with no responses has no answer data to export, so a backup is neither
    # possible nor needed; that is stated rather than silently skipped.
    backup_needed = not skip_backup and count > 0
    message = f"{description} in {before['title']!r} ({form_id})? {consequence} " + (
        "A validated response workbook backup is required before deletion; export does not automatically restore Forms answers."
        if backup_needed
        else "BACKUP EXPLICITLY DISABLED."
        if skip_backup
        else "There are no responses, so no backup is taken."
    )
    if await confirm(message) is not True:
        return {"status": "cancelled", "deleted": False}
    fresh = await api.read(form_id)
    if fingerprint(fresh) != fingerprint(before):
        raise FormsError(
            "stale_confirmation",
            "Form or response count changed after confirmation; obtain a new confirmation.",
        )
    backup = await workbooks.export(form_id, fresh) if backup_needed else None
    fresh = await api.read(form_id)
    if fingerprint(fresh) != fingerprint(before):
        raise FormsError("stale_confirmation", "Form changed during backup; deletion refused.")
    path = (
        api.card_path(form_id, card_id, section)
        if card_id
        else api_root(form_id) + f"/forms('{form_id}')"
    )
    await api.request("DELETE", path)
    if card_id:
        after = await api.read(form_id)
        remaining = {c["id"] for c in cards(after)}
        expected = {c["id"] for c in cards(before)} - {card_id}
        verified = remaining == expected
    else:
        check = await api.request(
            "GET", api_root(form_id) + f"/light/forms('{form_id}')", allow_missing=True
        )
        verified = check["status"] == 404 or bool((check.get("data") or {}).get("softDeleted"))
    return {
        "status": "completed" if verified else "verification_failed",
        "deleted": verified,
        "backup": backup,
        "backup_skipped": skip_backup,
        "response_rows": count,
    }
