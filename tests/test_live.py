"""Opt in explicitly: FORMS_MCP_LIVE=1, FORMS_MCP_PROFILE=<dedicated signed-in profile>."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from forms_mcp.api import FormsAPI
from forms_mcp.fill import Responses
from forms_mcp.operations import patch_question
from forms_mcp.safety import delete
from forms_mcp.session import BrowserSession
from forms_mcp.workbook import Workbooks

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FORMS_MCP_LIVE") != "1", reason="Live mutations require explicit opt-in"
    ),
]


async def test_disposable_full_chain(tmp_path):
    profile = os.environ.get("FORMS_MCP_PROFILE")
    assert profile, "Set a dedicated signed-in FORMS_MCP_PROFILE"
    session = BrowserSession(Path(profile))
    api = FormsAPI(session)
    books = Workbooks(api, tmp_path)
    responder = Responses(api, tmp_path)

    async def confirm_disposable(message):
        return True  # Explicit environment opt-in authorizes only this newly created form.

    try:
        await api.prepare()
        created = await api.create("msforms-mcp INTEGRATION " + uuid4().hex)
        fid = created["id"]
        (tmp_path / "created-form.json").write_text(json.dumps(created), encoding="utf-8")
        await api.prepare(fid)
        spec = [
            {"s": "Test"},
            {"t": "Text", "q": "Synthetic text", "req": True},
            {"t": "Choice", "q": "Synthetic choice", "opts": ["Yes", "No"], "req": True},
        ]
        assert (await api.build(fid, spec))["status"] == "completed"
        assert (await api.verify(fid, spec))["matches"]
        form = await api.read(fid)
        qid = next(c["id"] for c in form["questions"] if c["title"] == "Synthetic text")
        assert (await patch_question(api, fid, qid, {"Multiline": False}))["status"] == "completed"
        spec[1]["long"] = False
        assert (await api.build(fid, spec))["status"] == "already_applied"
        form = await api.read(fid)
        draft = await responder.fill(fid, responder.synthetic_answers(form))
        assert (await responder.submit(draft["draft_id"], confirm_disposable))[
            "status"
        ] == "submitted"
        assert (await responder.submit(draft["draft_id"], confirm_disposable))[
            "status"
        ] == "submitted"
        assert (await api.read(fid))["rowCount"] == 1
        assert (await books.provision(fid))["status"] == "completed"
        status = await books.status(fid)
        assert status["associated"] and status["resolves"]
        if status["freshness"] == "unknown":
            assert status["columns"] is None and status["warnings"]
        backup = await books.export(fid)
        assert backup["row_count"] == 1 and Path(backup["path"]).is_file()
        removed = await delete(api, books, confirm_disposable, fid, qid)
        assert removed["deleted"] and removed["backup"]["row_count"] == 1
        remaining = await books.export(fid)
        assert remaining["question_columns"] == 1
        assert (await delete(api, books, confirm_disposable, fid))["deleted"]
    finally:
        # On failure preserve the registered disposable form and backups for diagnosis.
        await session.close()
