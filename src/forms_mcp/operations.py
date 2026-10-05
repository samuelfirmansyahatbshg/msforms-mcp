import json
import math
from copy import deepcopy

from .api import script
from .errors import FormsError
from .ids import api_root
from .spec import validate_spec

MOVE_WARNING = "Display order may diverge from workbook creation order. Whole order values do not prove column alignment."


def cards(form):
    return sorted(
        form.get("questions", []) + form.get("descriptiveQuestions", []),
        key=lambda c: c.get("order", 0),
    )


def find_card(form, card_id):
    matches = [c for c in cards(form) if c["id"] == card_id]
    if len(matches) != 1:
        raise FormsError(
            "card_not_found", "The specified card no longer exists uniquely in this form."
        )
    return matches[0]


def patch_payload(card, changes):
    if any(k in changes for k in ("type", "t")):
        raise FormsError(
            "type_change_refused",
            "Forms cannot change a question's type. Delete/recreate destroys historical answers and is never implicit.",
        )
    allowed = {"title", "required", "options", "Multiline", "max_file_size_mb"}
    if not changes or set(changes) - allowed:
        raise FormsError(
            "invalid_patch",
            "Allowed changes: title, required, options, Multiline, max_file_size_mb.",
        )
    result = {}
    if "title" in changes:
        title = changes["title"]
        if not isinstance(title, str) or not title.strip() or title in ("Question", "Section"):
            raise FormsError("invalid_patch", "Choose a nonempty, non-placeholder title.")
        result.update(title=title, formsProRTQuestionTitle=title)
    if "required" in changes:
        if type(changes["required"]) is not bool or card["type"] == "Question.ColumnGroup":
            raise FormsError("invalid_patch", "required must be a boolean on a question.")
        result["required"] = changes["required"]
    if set(changes) & {"options", "Multiline", "max_file_size_mb"}:
        try:
            info = json.loads(card.get("questionInfo") or "{}")
        except (TypeError, ValueError):
            raise FormsError("malformed_info", "Cannot preserve malformed questionInfo.") from None
        if not isinstance(info, dict):
            raise FormsError("malformed_info", "questionInfo must contain a JSON object.")
        if "options" in changes:
            values = changes["options"]
            validate_spec([{"t": "Choice", "q": "Validation", "opts": values}])
            if card["type"] != "Question.Choice":
                raise FormsError("invalid_patch", "options applies only to Choice.")
            old = {c["Description"]: c for c in info.get("Choices", [])}
            info["Choices"] = [
                deepcopy(old.get(v, {"Description": v, "FormsProDisplayRTText": v})) for v in values
            ]
        if "Multiline" in changes:
            if (
                type(changes["Multiline"]) is not bool
                or card["type"] != "Question.TextField"
                or (info.get("IsNumber") and changes["Multiline"])
            ):
                raise FormsError(
                    "invalid_patch", "Multiline must be boolean; numeric text cannot be multiline."
                )
            info["Multiline"] = changes["Multiline"]
        if "max_file_size_mb" in changes:
            size = changes["max_file_size_mb"]
            if card["type"] != "Question.FileUpload" or type(size) is not int or size <= 0:
                raise FormsError(
                    "invalid_patch", "max_file_size_mb must be a positive integer on Upload."
                )
            info["MaxFileSize"] = size
        result["questionInfo"] = json.dumps(info, separators=(",", ":"))
    return result


def desired_matches(card, payload):
    return all(
        json.loads(card.get(k) or "{}") == json.loads(v)
        if k == "questionInfo"
        else card.get(k) == v
        for k, v in payload.items()
    )


async def patch_question(api, form_id, card_id, changes):
    form = await api.read(form_id)
    card = find_card(form, card_id)
    payload = patch_payload(card, changes)
    if desired_matches(card, payload):
        return {"status": "already_applied", "id": card_id}
    await api.request(
        "PATCH", api.card_path(form_id, card_id, card["type"] == "Question.ColumnGroup"), payload
    )
    after = find_card(await api.read(form_id), card_id)
    return {
        "status": "completed" if desired_matches(after, payload) else "verification_failed",
        "id": card_id,
    }


async def add_card(api, form_id, spec):
    spec = validate_spec([spec])
    await api.inject()
    return await api.session.page.evaluate(
        script("forms_build.js"),
        {"root": api_root(form_id), "formId": form_id, "spec": spec, "append": True},
    )


def move_order(form, card_id, before_id=None, after_id=None):
    if (before_id is None) == (after_id is None):
        raise FormsError("invalid_move", "Specify exactly one of before_id or after_id.")
    find_card(form, card_id)
    target = before_id or after_id
    if card_id == target:
        raise FormsError("invalid_move", "A card cannot be its own neighbor.")
    others = [c for c in cards(form) if c["id"] != card_id]
    matches = [i for i, c in enumerate(others) if c["id"] == target]
    if len(matches) != 1:
        raise FormsError("invalid_move", "Neighbor card does not exist uniquely.")
    index = matches[0] + (1 if after_id else 0)
    low = others[index - 1]["order"] if index else others[0]["order"] - 1000000
    high = others[index]["order"] if index < len(others) else low + 1000000
    value = low + (high - low) / 2
    if not math.isfinite(value) or not low < value < high:
        raise FormsError(
            "order_exhausted",
            "No representable order between neighbors; automatic renumbering is refused.",
        )
    return value


async def move(api, form_id, card_id, before_id=None, after_id=None):
    form = await api.read(form_id)
    card = find_card(form, card_id)
    order = move_order(form, card_id, before_id, after_id)
    await api.request(
        "PATCH",
        api.card_path(form_id, card_id, card["type"] == "Question.ColumnGroup"),
        {"order": order},
    )
    updated = await api.read(form_id)
    return {
        "status": "completed"
        if find_card(updated, card_id)["order"] == order
        else "verification_failed",
        "order": order,
        "orders_whole": all(c["order"] % 1000000 == 0 for c in cards(updated)),
        "warnings": [MOVE_WARNING],
    }
