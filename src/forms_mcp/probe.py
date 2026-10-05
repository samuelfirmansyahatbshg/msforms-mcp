"""Probe bootstrap. No production form mutation is permitted by this harness."""

import asyncio
import json
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from .errors import FormsError
from .ids import ORIGIN, api_root


async def probe_create(session, output: Path) -> dict:
    """Create one disposable form. Record its exact ID before any subsequent work."""
    await session.ready()
    identity = await session.identity()
    root = f"{ORIGIN}/formapi/api/{identity['tenant']}/users/{identity['owner']}"
    title = f"msforms-mcp PROBE {datetime.now(timezone.utc):%Y%m%dT%H%M%SZ} {uuid4().hex[:8]}"
    request = files("forms_mcp").joinpath("js/request.js").read_text(encoding="utf-8")
    response = await session.page.evaluate(
        request,
        {
            "url": root + "/forms",
            "method": "POST",
            "body": {"title": title},
        },
    )
    data = response.get("data") or {}
    fid = data.get("id")
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": "create",
        "status": response["status"],
        "title": title,
        "form_id": fid,
        "verified": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
    if fid:
        record["root_decodes"] = api_root(fid) == root
        reread = await session.page.evaluate(
            request,
            {
                "url": api_root(fid)
                + f"/light/forms('{fid}')?$expand=questions,descriptiveQuestions",
                "method": "GET",
                "body": None,
            },
        )
        record["read_status"] = reread["status"]
        record["verified"] = (
            reread["status"] == 200 and (reread["data"] or {}).get("title") == title
        )
        output.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


async def login(session, timeout: int = 600):
    await session.start()
    try:
        await session.page.goto(ORIGIN + "/Pages/DesignPageV2.aspx", wait_until="domcontentloaded")
    except Exception:
        pass  # Keep the visible browser available for network/login recovery.
    print("Dedicated browser opened. Sign into Microsoft Forms there.", flush=True)
    for _ in range(timeout):
        try:
            identity = await session.identity()
            if identity.get("owner") and await session.page.evaluate(
                "!!window.OfficeFormServerInfo?.antiForgeryToken"
            ):
                print("Forms sign-in verified; profile saved.", flush=True)
                return
        except Exception:
            pass
        await asyncio.sleep(1)
    raise FormsError("login_required", "Sign-in was not completed before the timeout.")
