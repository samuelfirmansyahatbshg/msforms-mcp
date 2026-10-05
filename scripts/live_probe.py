"""Development-only experiments restricted to the exact recorded disposable form."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from forms_mcp.ids import ORIGIN, api_root
from forms_mcp.session import BrowserSession

sys.stdout.reconfigure(encoding="utf-8")
STATE = Path(__file__).resolve().parents[1] / ".state"


async def main(action):
    registry = STATE / (
        "probe-ui-create.json" if (STATE / "probe-ui-create.json").exists() else "probe-create.json"
    )
    created = json.loads(registry.read_text())
    fid = created["form_id"]
    assert created["verified"] and created["title"].startswith("msforms-mcp PROBE ")
    session = BrowserSession(STATE / "browser-profile")
    request = files("forms_mcp").joinpath("js/request.js").read_text()
    events = []
    tasks = []

    async def capture(response):
        parts = urlsplit(response.url)
        if any(
            x in parts.path.lower() for x in ("formapi", "workbook", "excel", "export", "download")
        ):
            event = {
                "method": response.request.method,
                "host": parts.netloc,
                "path": parts.path,
                "query_keys": [p.split("=")[0] for p in parts.query.split("&")],
                "status": response.status,
            }
            if "formapi" in parts.path and response.request.method == "GET":
                try:
                    data = await response.json()
                    if isinstance(data, dict):
                        event["keys"] = list(data)
                        if isinstance(data.get("value"), list) and data["value"]:
                            event["item_keys"] = list(data["value"][0])
                except Exception:
                    pass
            events.append(event)
            if parts.path.endswith("DownloadExcelFile.ashx"):
                event["query"] = parse_qs(parts.query)
                event["header_names"] = list((await response.request.all_headers()))
                print(json.dumps({"download_request": event}), flush=True)
            if (
                action == "create-ui"
                and response.request.method == "POST"
                and parts.path.endswith("/forms")
            ):
                body = await response.json()
                (STATE / "probe-ui-payload.json").write_text(
                    json.dumps(response.request.post_data_json, indent=2), encoding="utf-8"
                )
                (STATE / "probe-ui-created.json").write_text(
                    json.dumps({"id": body.get("id"), "status": response.status}), encoding="utf-8"
                )

    async def call(method, path, body=None):
        if method != "GET":
            await session.pace()
        result = await session.page.evaluate(
            request, {"url": api_root(fid) + path, "method": method, "body": body}
        )
        print(json.dumps({"method": method, "path": path, "status": result["status"]}), flush=True)
        return result

    fpath = f"/forms('{fid}')"
    rpath = f"/light/forms('{fid}')?$expand=questions,descriptiveQuestions"
    try:
        await session.start()
        session.context.on("response", lambda r: tasks.append(asyncio.create_task(capture(r))))
        await session.ready(fid)
        live = (await call("GET", rpath))["data"]
        assert live["title"] == created["title"], "Disposable form title changed; refuse mutation"
        if action == "create-ui":
            await session.page.goto(ORIGIN)
            await session.page.get_by_role("button", name="Create a new form", exact=True).click()
            await session.page.wait_for_timeout(6000)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            new = json.loads((STATE / "probe-ui-created.json").read_text())
            fid = new["id"]
            title = "msforms-mcp PROBE UI " + uuid4().hex[:8]
            # Register before initialization so interrupted runs remain recoverable.
            record = {"title": title, "form_id": fid, "verified": False}
            (STATE / "probe-ui-create.json").write_text(json.dumps(record), encoding="utf-8")
            await session.ready(fid)
            fpath = f"/forms('{fid}')"
            rpath = f"/light/forms('{fid}')?$expand=questions,descriptiveQuestions"
            await call("PATCH", fpath, {"title": title})
            check = (await call("GET", rpath))["data"]
            record["verified"] = check["title"] == title
            (STATE / "probe-ui-create.json").write_text(json.dumps(record), encoding="utf-8")
        elif action == "build":
            assert not live.get("questions") and not live.get("descriptiveQuestions"), (
                "Already built"
            )
            cards = [
                ("Section One", "Question.ColumnGroup", None),
                (
                    "Probe Text",
                    "Question.TextField",
                    {"Multiline": True, "ShuffleOptions": False, "ShowRatingLabel": False},
                ),
                ("Section Two", "Question.ColumnGroup", None),
                (
                    "Probe Choice",
                    "Question.Choice",
                    {
                        "Choices": [
                            {"Description": "Yes", "FormsProDisplayRTText": "Yes"},
                            {"Description": "No", "FormsProDisplayRTText": "No"},
                        ],
                        "ChoiceType": 1,
                        "AllowOtherAnswer": False,
                        "OptionDisplayStyle": "ListAll",
                        "ChoiceRestrictionType": "None",
                        "ShuffleOptions": False,
                        "ShowRatingLabel": False,
                    },
                ),
            ]
            for n, (title, typ, info) in enumerate(cards, 1):
                collection = "descriptiveQuestions" if info is None else "questions"
                qid = "r" + uuid4().hex
                body = {
                    "id": qid,
                    "type": typ,
                    "title": "Section" if info is None else "Question",
                    "order": n * 1000000,
                    "isQuiz": False,
                    "required": info is not None,
                }
                if info is not None:
                    body["questionInfo"] = json.dumps(info, separators=(",", ":"))
                result = await call("POST", fpath + "/" + collection, body)
                assert result["status"] == 201, result["status"]
                result = await call(
                    "PATCH",
                    fpath + f"/{collection}('{qid}')",
                    {"title": title, "formsProRTQuestionTitle": title},
                )
                assert result["status"] == 204, result["status"]
        elif action in ("fill", "submit"):
            await session.page.goto(ORIGIN + "/Pages/ResponsePage.aspx?id=" + fid)
            await session.page.wait_for_timeout(4000)
            if await session.page.get_by_role("button", name="Technical details").count():
                await session.page.get_by_role("button", name="Technical details").click()
            if action == "submit":
                assert live["rowCount"] == 0, "Never submit twice"
                await session.page.get_by_role("textbox").fill(
                    "SYNTHETIC PROBE historical answer 12345"
                )
                await session.page.get_by_role("button", name="Next", exact=True).click()
                await session.page.get_by_role("radio", name="Yes", exact=True).check()
                await session.page.get_by_role("button", name="Submit", exact=True).click()
                await session.page.wait_for_timeout(3000)
        elif action == "collect":
            await session.page.get_by_role("button", name="Collect responses", exact=True).click()
            await session.page.wait_for_timeout(2000)
        elif action == "home":
            await session.page.goto(ORIGIN)
            await session.page.wait_for_timeout(6000)
            await session.page.get_by_role("tab", name="Shared with me", exact=True).click()
            await session.page.wait_for_timeout(2000)
        elif action == "move":
            card = next(q for q in live["questions"] if q["title"] == "Probe Text")
            result = await call("PATCH", fpath + f"/questions('{card['id']}')", {"order": 3500000})
            assert result["status"] == 204
        elif action == "graph":
            import base64

            share = "u!" + base64.urlsafe_b64encode(live["sdxWebUrl"].encode()).decode().rstrip("=")
            for path in (
                "shares/" + share + "/driveItem",
                live["sdxWorkbookId"],
                live["sdxWorkbookId"] + "/workbook/worksheets",
            ):
                r = await session.page.evaluate(
                    request,
                    {
                        "url": ORIGIN + "/formapi/msgraph/v1.0/" + path,
                        "method": "GET",
                        "body": None,
                    },
                )
                print(
                    json.dumps({"proxy_path": path, "status": r["status"], "data": r["data"]}),
                    flush=True,
                )
                if path.endswith("/worksheets") and r["status"] == 200:
                    for sheet in r["data"]["value"]:
                        p = (
                            ORIGIN
                            + "/formapi/msgraph/v1.0/"
                            + live["sdxWorkbookId"]
                            + "/workbook/worksheets/"
                            + sheet["id"]
                            + "/usedRange(valuesOnly=true)"
                        )
                        cells = await session.page.evaluate(
                            request, {"url": p, "method": "GET", "body": None}
                        )
                        (STATE / "probe-graph-cells.json").write_text(
                            json.dumps(cells), encoding="utf-8"
                        )
                        print(json.dumps({"cells": cells}), flush=True)
        elif action == "responses":
            for path in (fpath + "/responses", fpath + "/xl/exportFormExcel?enableOpenXML=true"):
                r = await call("GET", path)
                print(json.dumps({"response_data": r}), flush=True)
        elif action == "open-excel":
            excel = await session.context.new_page()
            await excel.goto(live["sdxWebUrl"], wait_until="domcontentloaded")
            await excel.wait_for_timeout(15000)
            snapshot = await excel.locator("body").aria_snapshot()
            (STATE / "probe-excel-web-ui.txt").write_text(snapshot, encoding="utf-8")
            print(snapshot[:16000], flush=True)
        elif action in ("delete-question", "delete-section"):
            collection = "questions" if action == "delete-question" else "descriptiveQuestions"
            title = "Probe Text" if action == "delete-question" else "Section Two"
            card = next(q for q in live[collection] if q["title"] == title)
            result = await call("DELETE", fpath + f"/{collection}('{card['id']}')")
            print(json.dumps({"delete_result": result}), flush=True)
        elif action == "cleanup":
            result = await call("DELETE", fpath)
            print(json.dumps({"delete_result": result}), flush=True)
        elif action in ("workbook", "excel", "export-menu", "download"):
            for button in await session.page.get_by_role(
                "button", name="Got it", exact=False
            ).all():
                await button.evaluate("e=>e.click()")
            await session.page.get_by_role("button", name="View responses", exact=False).click()
            await session.page.wait_for_timeout(4000)
            for button in await session.page.get_by_role(
                "button", name="Got it", exact=False
            ).all():
                await button.evaluate("e=>e.click()")
            if action == "excel":
                await session.page.get_by_role(
                    "button", name="Open results in Excel", exact=True
                ).first.click()
                await session.page.wait_for_timeout(3000)
                if await session.page.get_by_role("button", name="Continue", exact=True).count():
                    await session.page.get_by_role("button", name="Continue", exact=True).click()
                    await session.page.wait_for_timeout(5000)
            elif action in ("export-menu", "download"):
                menu = session.page.get_by_role("button", name="More options", exact=True)
                if not await menu.count():
                    menu = session.page.get_by_role(
                        "button", name="Open results in Excel", exact=True
                    ).last
                await menu.click()
                await session.page.wait_for_timeout(1000)
                if action == "download":
                    async with session.page.expect_download(timeout=60000) as pending:
                        await session.page.get_by_role(
                            "menuitem", name="Download a copy", exact=True
                        ).click()
                    download = await pending.value
                    path = STATE / (
                        "probe-export-" + datetime.now(timezone.utc).strftime("%H%M%S") + ".xlsx"
                    )
                    await download.save_as(path)
                    from openpyxl import load_workbook

                    wb = load_workbook(path, read_only=True, data_only=True)
                    print(
                        json.dumps(
                            {
                                "export": str(path),
                                "sheets": {ws.title: list(ws.values) for ws in wb},
                            },
                            default=str,
                        ),
                        flush=True,
                    )
                    wb.close()
        if action not in ("home", "fill", "submit", "cleanup"):
            live = (await call("GET", rpath))["data"]
            (STATE / f"probe-{action}-form.json").write_text(
                json.dumps(live, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        "form_keys": list(live),
                        "cards": [
                            {
                                k: c.get(k)
                                for k in ("id", "title", "order", "type", "parentId", "sectionId")
                            }
                            for c in live.get("questions", [])
                            + live.get("descriptiveQuestions", [])
                        ],
                    }
                ),
                flush=True,
            )
        snapshot = await session.page.locator("body").aria_snapshot()
        (STATE / f"probe-{action}-ui.txt").write_text(snapshot, encoding="utf-8")
        print(snapshot[:14000], flush=True)
    finally:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        (STATE / f"probe-{action}-network.json").write_text(
            json.dumps(events, indent=2), encoding="utf-8"
        )
        print(json.dumps({"network_events": len(events), "saved_under": str(STATE)}), flush=True)
        await session.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
