"""Validate fleet plan/apply against freshly created disposable forms only.

Reproduces the driving use case: across every selected form, delete "Accepted" and
rename "Additional Comments" to "Comments". Each form is created here, carries one
synthetic response, and is deleted at the end.
"""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from forms_mcp.api import FormsAPI
from forms_mcp.fill import Responses
from forms_mcp.fleet import Fleet
from forms_mcp.safety import delete
from forms_mcp.session import BrowserSession
from forms_mcp.workbook import Workbooks

sys.stdout.reconfigure(encoding="utf-8")
STATE = Path(__file__).resolve().parents[1] / ".state"
REGISTRY = STATE / "fleet-probe.json"
PREFIX = "msforms-mcp FLEET PROBE "
SPEC = [
    {"s": "Evaluation"},
    {"t": "Text", "q": "Additional Comments"},
    {"t": "Choice", "q": "Accepted", "req": True, "opts": ["Yes", "No"]},
]
EDITS = [
    {"action": "patch", "title": "Additional Comments", "changes": {"title": "Comments"}},
    {"action": "delete", "title": "Accepted"},
]


async def always(message):
    return True  # Only ever applied to the disposable forms registered below.


async def main():
    session = BrowserSession(STATE / "browser-profile")
    api = FormsAPI(session)
    books = Workbooks(api, STATE)
    responder = Responses(api, STATE)
    fleet = Fleet(api, books, STATE)
    try:
        await api.prepare()
        if REGISTRY.exists():
            ids = json.loads(REGISTRY.read_text())
        else:
            ids = []
            for _ in range(2):
                ids.append((await api.create(PREFIX + uuid4().hex[:8]))["id"])
                REGISTRY.write_text(json.dumps(ids), encoding="utf-8")
        for fid in ids:
            form = await api.read(fid)
            assert form["title"].startswith(PREFIX), "Refusing to touch a non-probe form"
            await api.prepare(fid)
            build = await api.build(fid, SPEC)
            assert build["status"] in ("completed", "already_applied"), build
            form = await api.read(fid)
            if not form["rowCount"]:
                draft = await responder.fill(fid, responder.synthetic_answers(form))
                assert (await responder.submit(draft["draft_id"], always))["status"] == "submitted"
        plan = await fleet.plan({"ids": ids}, EDITS)
        print(
            json.dumps(
                {
                    "plan_id": plan["plan_id"][:12],
                    "forms": [
                        {
                            "status": f["status"],
                            "rows": f["response_rows"],
                            "deletions": f["deletions"],
                            "ops": [
                                {
                                    "action": o.get("action"),
                                    "status": o["status"],
                                    "before": (o.get("before") or {}).get("title"),
                                    "after": (o.get("after") or {}).get("title"),
                                }
                                for o in f["operations"]
                            ],
                        }
                        for f in plan["forms"]
                    ],
                }
            ),
            flush=True,
        )
        applied = await fleet.apply(plan["plan_id"], always)
        print(
            json.dumps(
                {
                    "status": applied["status"],
                    "results": [
                        {
                            "status": r["status"],
                            "backup_rows": (r.get("backup") or {}).get("row_count"),
                        }
                        for r in applied["results"]
                    ],
                }
            ),
            flush=True,
        )
        for fid in ids:
            cards = [c["title"] for c in (await api.inspect(fid))["cards"]]
            print(json.dumps({"final_cards": cards}), flush=True)
            assert cards == ["Evaluation", "Comments"], cards
        again = await fleet.apply(plan["plan_id"], always)
        print(
            json.dumps({"reapply": [r["status"] for r in again["results"]]}),
            flush=True,
        )
        fresh = await fleet.plan({"ids": ids}, EDITS)
        print(
            json.dumps(
                {
                    "replan_statuses": [
                        [o["status"] for o in f["operations"]] for f in fresh["forms"]
                    ]
                }
            ),
            flush=True,
        )
        for fid in ids:
            await api.prepare(fid)
            removed = await delete(api, books, always, fid)
            print(json.dumps({"cleanup": removed["status"]}), flush=True)
        REGISTRY.unlink()
    finally:
        await session.close()


asyncio.run(main())
