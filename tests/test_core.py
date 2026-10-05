import base64
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forms_mcp.api import FormsAPI, redact
from forms_mcp.errors import FormsError
from forms_mcp.fleet import Fleet, plan_form
from forms_mcp.ids import api_root, decode_form_id
from forms_mcp.operations import move_order, patch_payload
from forms_mcp.safety import delete, fingerprint
from forms_mcp.server import confirmed
from forms_mcp.spec import validate_spec

FID = (
    base64.urlsafe_b64encode(bytes.fromhex("33221100554477668899aabbccddeeff" * 2) + b"TABLE")
    .decode()
    .rstrip("=")
)
QID = "r" + "a" * 32


def form():
    return {
        "id": FID,
        "title": "Fixture",
        "rowCount": 3,
        "version": "v1",
        "descriptiveQuestions": [],
        "questions": [
            {
                "id": QID,
                "title": "Comments",
                "order": 1000000,
                "required": False,
                "type": "Question.TextField",
                "questionInfo": '{"Multiline":true,"FutureProperty":42}',
            }
        ],
    }


def test_guid_golden_mixed_endian():
    tenant, owner, typ = decode_form_id(FID)
    assert tenant == owner == "00112233-4455-6677-8899-aabbccddeeff"
    assert typ == "users"
    raw = bytes.fromhex("33221100554477668899aabbccddeeff" * 2) + b"TABLE$%@#t=g..."
    group = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert "/groups/00112233-4455-6677-8899-aabbccddeeff" in api_root(group)


@pytest.mark.parametrize(
    "value", ["", "abc", "https://forms.cloud.microsoft/id", "../oops", "'", "=" * 20]
)
def test_invalid_form_ids(value):
    with pytest.raises(FormsError):
        decode_form_id(value)


@pytest.mark.parametrize(
    "card",
    [
        {"t": "Choice", "q": "Pick", "opts": ["Yes", "Yes"]},
        {"s": "Section"},
        {"t": "Text", "q": "Q", "req": "false"},
        {"t": "Upload", "q": "Q", "mb": True},
        {"t": "Text", "q": "Q", "opts_from": "TABLE"},
    ],
)
def test_invalid_specs(card):
    with pytest.raises(FormsError):
        validate_spec([card])


def test_repeated_titles_allowed_but_duplicate_keys_refused():
    spec = [{"t": "Text", "q": "Notes", "k": "a"}, {"t": "Text", "q": "Notes", "k": "b"}]
    assert len(validate_spec({"title": "Fixture", "spec": spec})) == 2
    spec[1]["k"] = "a"
    with pytest.raises(FormsError):
        validate_spec(spec)


def test_patch_preserves_unknown_info_and_refuses_type_change():
    c = form()["questions"][0]
    assert json.loads(patch_payload(c, {"Multiline": False})["questionInfo"]) == {
        "Multiline": False,
        "FutureProperty": 42,
    }
    with pytest.raises(FormsError, match="cannot change"):
        patch_payload(c, {"type": "Choice"})


def test_move_arithmetic_and_unknown_neighbor():
    f = form()
    second = deepcopy(f["questions"][0])
    second.update(id="r" + "b" * 32, order=2000000)
    f["questions"].append(second)
    assert move_order(f, QID, after_id=second["id"]) == 2500000
    with pytest.raises(FormsError):
        move_order(f, QID, before_id="missing")


def test_ambiguous_fleet_match_refuses_entire_form():
    f = form()
    f["questions"].append({**f["questions"][0], "id": "r" + "b" * 32})
    target = plan_form(f, [{"action": "delete", "title": "Comments"}])
    assert target["status"] == "ambiguous"
    assert "operations" not in target


def test_fleet_plan_reports_before_after_and_rows_without_mutation():
    f = form()
    original = deepcopy(f)
    p = plan_form(
        f,
        [
            {"action": "patch", "title": "Comments", "changes": {"title": "Notes"}},
            {"action": "delete", "title": "Notes"},
        ],
    )
    assert p["response_rows"] == 3
    assert p["deletions"] == 1
    assert p["operations"][0]["before"]["title"] == "Comments"
    assert p["operations"][0]["after"]["title"] == "Notes"
    assert f == original


@pytest.mark.parametrize("answer", [False, None, {}, "yes", 1])
async def test_declined_gate_never_reaches_delete(answer):
    api = SimpleNamespace(read=AsyncMock(return_value=form()), request=AsyncMock())
    books = SimpleNamespace(export=AsyncMock())
    result = await delete(api, books, AsyncMock(return_value=answer), FID, QID)
    assert result["status"] == "cancelled"
    api.request.assert_not_called()
    books.export.assert_not_called()


async def test_failed_backup_never_reaches_delete():
    api = SimpleNamespace(read=AsyncMock(return_value=form()), request=AsyncMock())
    books = SimpleNamespace(export=AsyncMock(side_effect=FormsError("failed", "Backup failed")))
    with pytest.raises(FormsError):
        await delete(api, books, AsyncMock(return_value=True), FID, QID)
    api.request.assert_not_called()


async def test_confirmation_drift_never_reaches_delete():
    changed = form()
    changed["rowCount"] += 1
    api = SimpleNamespace(read=AsyncMock(side_effect=[form(), changed]), request=AsyncMock())
    with pytest.raises(FormsError, match="changed"):
        await delete(
            api, SimpleNamespace(), AsyncMock(return_value=True), FID, QID, skip_backup=True
        )
    api.request.assert_not_called()


async def test_accepted_delete_backs_up_before_request():
    before, after = form(), form()
    after["questions"] = []
    order = []

    async def export(*args):
        order.append("backup")
        return {"path": "fixture.xlsx"}

    async def request(*args):
        order.append("delete")

    api = SimpleNamespace(
        read=AsyncMock(side_effect=[before, before, before, after]),
        request=request,
        card_path=FormsAPI.card_path,
    )
    confirm = AsyncMock(return_value=True)
    result = await delete(api, SimpleNamespace(export=export), confirm, FID, QID)
    assert result["deleted"] is True
    assert order == ["backup", "delete"]
    assert "3 historical response rows" in confirm.call_args.args[0]


async def test_empty_form_states_no_backup_and_still_confirms():
    empty = form()
    empty["rowCount"] = 0
    api = SimpleNamespace(
        read=AsyncMock(return_value=empty),
        request=AsyncMock(return_value={"status": 404, "data": None}),
        card_path=FormsAPI.card_path,
    )
    books = SimpleNamespace(export=AsyncMock())
    confirm = AsyncMock(return_value=True)
    result = await delete(api, books, confirm, FID)
    assert result["deleted"] is True
    assert "no responses" in confirm.call_args.args[0]
    books.export.assert_not_called()
    assert result["backup"] is None


@pytest.mark.parametrize(
    "action,value", [("decline", True), ("cancel", True), ("accept", False), ("accept", None)]
)
async def test_mcp_confirmation_rejects_nonacceptance(action, value):
    ctx = SimpleNamespace(
        elicit=AsyncMock(
            return_value=SimpleNamespace(action=action, data=SimpleNamespace(confirm=value))
        )
    )
    assert await confirmed(ctx, "Confirm") is False


async def test_unsupported_elicitation_fails_closed():
    assert (
        await confirmed(SimpleNamespace(elicit=AsyncMock(side_effect=RuntimeError())), "Confirm")
        is False
    )


async def test_fleet_apply_is_idempotent_and_journaled(tmp_path):
    f = form()
    writes = []

    async def request(method, path, payload):
        writes.append(method)
        f["questions"][0].update(payload)

    api = SimpleNamespace(
        read=AsyncMock(side_effect=lambda fid: deepcopy(f)),
        request=request,
        card_path=FormsAPI.card_path,
        session=SimpleNamespace(
            identity=AsyncMock(return_value={"owner": "test", "tenant": "test"})
        ),
    )
    fleet = Fleet(api, SimpleNamespace(), tmp_path)
    plan = await fleet.plan(
        {"ids": [FID]}, [{"action": "patch", "title": "Comments", "changes": {"title": "Notes"}}]
    )
    assert not list(tmp_path.iterdir())
    first = await fleet.apply(plan["plan_id"], AsyncMock(return_value=True))
    assert first["results"][0]["status"] == "completed"
    # Reopen from a persisted apply journal to exercise restart behavior.
    second = await Fleet(api, SimpleNamespace(), tmp_path).apply(
        plan["plan_id"], AsyncMock(return_value=True)
    )
    assert second["results"][0]["status"] == "already_applied"
    assert writes == ["PATCH"]


async def test_fleet_stale_count_and_decline_produce_zero_writes(tmp_path):
    f = form()
    api = SimpleNamespace(
        read=AsyncMock(side_effect=lambda fid: deepcopy(f)),
        request=AsyncMock(),
        session=SimpleNamespace(identity=AsyncMock(return_value={"owner": "test"})),
    )
    fleet = Fleet(api, SimpleNamespace(), tmp_path)
    p = await fleet.plan(
        {"ids": [FID]}, [{"action": "delete", "title": "Comments"}], skip_backup=True
    )
    await fleet.apply(p["plan_id"], AsyncMock(return_value=False))
    assert not list(tmp_path.iterdir())
    f["rowCount"] = 4
    result = await fleet.apply(p["plan_id"], AsyncMock(return_value=True))
    assert result["results"][0]["code"] == "stale_plan"
    api.request.assert_not_called()


def test_redaction_and_fingerprint():
    assert redact(
        {"permissionTokens": ["secret"], "questions": [{"title": "X", "cookie": "secret"}]}
    ) == {"questions": [{"title": "X"}]}
    changed = form()
    changed["questions"][0]["required"] = True
    assert fingerprint(form()) != fingerprint(changed)
