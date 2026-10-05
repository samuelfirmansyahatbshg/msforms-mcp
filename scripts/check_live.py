"""Run package-level checks only on a registered disposable form."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

from forms_mcp.api import FormsAPI
from forms_mcp.fill import Responses
from forms_mcp.session import BrowserSession
from forms_mcp.workbook import Workbooks

sys.stdout.reconfigure(encoding="utf-8")
STATE = Path(__file__).resolve().parents[1] / ".state"
SPEC = [
    {"s": "Probe Section"},
    {"k": "text", "t": "Text", "q": "Probe Text", "req": True},
    {"k": "choice", "t": "Choice", "q": "Probe Choice", "req": True, "opts": ["Yes", "No"]},
    {"k": "upload", "t": "Upload", "q": "Probe Upload"},
]


async def main(action):
    session = BrowserSession(STATE / "browser-profile")
    api = FormsAPI(session)
    try:
        await api.prepare()
        record = STATE / "package-probe.json"
        if not record.exists():
            created = await api.create("msforms-mcp PROBE PACKAGE " + uuid4().hex[:8])
            record.write_text(json.dumps(created), encoding="utf-8")
        form_id = json.loads(record.read_text())["id"]
        await api.prepare(form_id)
        form = await api.read(form_id)
        assert form["title"].startswith("msforms-mcp PROBE PACKAGE ")
        if action == "build":
            print(json.dumps(await api.build(form_id, SPEC)), flush=True)
            print(json.dumps(await api.verify(form_id, SPEC)), flush=True)
            print(json.dumps(await api.build(form_id, SPEC)), flush=True)
        elif action == "list":
            result = await api.list_forms()
            print(
                json.dumps(
                    {
                        "complete": result["complete"],
                        "count": len(result["forms"]),
                        "errors": result["errors"],
                        "groups": sum(f["ownership"] == "group" for f in result["forms"]),
                    }
                ),
                flush=True,
            )
        elif action == "response-dom":
            await session.page.goto(
                "https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=" + form_id
            )
            await session.page.wait_for_timeout(5000)
            print(await session.page.locator("body").aria_snapshot())
            print(
                json.dumps(
                    await session.page.locator('[data-automation-id="questionItem"]').evaluate_all(
                        "es=>es.map(e=>({html:e.outerHTML.slice(0,3000)}))"
                    )
                ),
                flush=True,
            )
        elif action == "export":
            book = Workbooks(api, STATE)
            print(json.dumps(await book.export(form_id)), flush=True)
            print(json.dumps(await book.status(form_id)), flush=True)
        elif action == "fill-submit":
            assert form["rowCount"] == 0, "Do not duplicate a probe submission"
            responder = Responses(api, STATE)
            answers = responder.materialize(responder.synthetic_answers(form))
            prepared = await responder.fill(form_id, answers)
            print(
                json.dumps({"draft": prepared["draft_id"], "status": prepared["status"]}),
                flush=True,
            )

            async def confirm_probe(message):
                return True  # User authorized synthetic submissions to disposable probes.

            print(
                json.dumps(await responder.submit(prepared["draft_id"], confirm_probe)), flush=True
            )
            print(json.dumps(await Workbooks(api, STATE).provision(form_id)), flush=True)
        elif action == "upload-net":
            # Observe the real attachment network sequence before trusting any wait condition.
            responder = Responses(api, STATE)
            answers = responder.materialize(responder.synthetic_answers(form))
            upload_id = next(
                c["id"] for c in form["questions"] if c["type"] == "Question.FileUpload"
            )
            page = await session.context.new_page()
            events = []
            page.on(
                "request",
                lambda r: events.append(
                    {"phase": "request", "method": r.method, "url": r.url[:160]}
                ),
            )
            page.on(
                "response",
                lambda r: events.append(
                    {
                        "phase": "response",
                        "method": r.request.method,
                        "status": r.status,
                        "url": r.url[:160],
                    }
                ),
            )
            await page.goto(
                "https://forms.cloud.microsoft/Pages/ResponsePage.aspx?id=" + form_id,
                wait_until="domcontentloaded",
            )
            await page.evaluate(
                """fid => {for(const key of Object.keys(localStorage))
                if(key.startsWith('officeforms.answermap.'+fid+'.')) localStorage.removeItem(key); }""",
                form_id,
            )
            await page.reload(wait_until="domcontentloaded")
            await page.locator('[data-automation-id="questionItem"]').first.wait_for(timeout=60000)
            block = page.locator('[data-automation-id="questionItem"]').filter(
                has=page.locator(f'[id="QuestionId_{upload_id}"]')
            )
            print(json.dumps({"upload_id": upload_id, "blocks": await block.count()}), flush=True)
            async with page.expect_file_chooser() as pending:
                await block.locator('[data-automation-id="fileUploadButton"]').click()
            chooser = await pending.value
            await chooser.set_files(answers[upload_id])
            await page.wait_for_timeout(25000)
            interesting = [
                e
                for e in events
                if any(
                    k in e["url"].lower() for k in ("upload", "session", "formapi", "sharepoint")
                )
            ]
            (STATE / "probe-upload-net.json").write_text(
                json.dumps(interesting, indent=2), encoding="utf-8"
            )
            print(json.dumps(interesting, indent=2), flush=True)
            print(await block.aria_snapshot(), flush=True)
            print(
                json.dumps(
                    {
                        "progressbars": await block.locator('[role="progressbar"]').count(),
                        "submit_enabled": await page.evaluate(
                            """() => [...document.querySelectorAll('button')]
                           .some(b => b.textContent.trim() === 'Submit' && !b.disabled)"""
                        ),
                    }
                ),
                flush=True,
            )
            await page.close()
        elif action == "cleanup-probes":
            # Delete only the probe forms this harness registered, each backed up first.
            from forms_mcp.safety import delete

            async def always(message):
                return True

            for name in ("probe-ui-create.json", "package-probe.json"):
                registry = STATE / name
                if not registry.exists():
                    continue
                probe = json.loads(registry.read_text())["form_id" if "ui" in name else "id"]
                live = await api.read(probe)
                assert live["title"].startswith("msforms-mcp PROBE "), live["title"]
                await api.prepare(probe)
                result = await delete(api, Workbooks(api, STATE), always, probe)
                print(
                    json.dumps({name: result["status"], "rows": result["response_rows"]}),
                    flush=True,
                )
                registry.unlink()
        elif action == "cleanup-first":
            from forms_mcp.ids import api_root

            first = json.loads((STATE / "probe-create.json").read_text())
            old = await api.read(first["form_id"])
            assert old["title"] == first["title"] and old["rowCount"] == 0
            result = await api.request(
                "DELETE", api_root(first["form_id"]) + f"/forms('{first['form_id']}')"
            )
            check = await api.request(
                "GET",
                api_root(first["form_id"]) + f"/light/forms('{first['form_id']}')",
                allow_missing=True,
            )
            print(
                json.dumps(
                    {
                        "delete_status": result["status"],
                        "read_status": check["status"],
                        "soft_deleted": (check.get("data") or {}).get("softDeleted"),
                    }
                )
            )
        elif action == "existing-export":
            existing = json.loads((STATE / "probe-ui-create.json").read_text())["form_id"]
            book = Workbooks(api, STATE)
            print(json.dumps(await book.export(existing)), flush=True)
            print(json.dumps(await book.status(existing)), flush=True)
        elif action == "direct-export":
            import base64

            from forms_mcp.workbook import EXPORT_JS

            existing = json.loads((STATE / "probe-ui-create.json").read_text())["form_id"]
            await api.prepare(existing)
            r = await session.page.evaluate(EXPORT_JS, {"formId": existing, "minId": 1, "maxId": 1})
            data = base64.b64decode(r.get("content", ""))
            print(
                json.dumps(
                    {"status": r["status"], "bytes": len(data), "signature": data[:24].hex()}
                )
            )
            if data.startswith(b"{"):
                j = json.loads(data)
                print(json.dumps({"error": j.get("error"), "keys": list(j)}))
    finally:
        await session.close()


asyncio.run(main(sys.argv[1]))
