"""Prepare a response in a dedicated tab; submit only after separate confirmation."""

import base64
import json
from pathlib import Path
from uuid import uuid4

from .errors import FormsError
from .ids import ORIGIN
from .safety import fingerprint


class Responses:
    def __init__(self, api, state_dir):
        self.api, self.state_dir = api, state_dir
        self.drafts = {}

    def synthetic_answers(self, form):
        answers = {}
        for card in form.get("questions", []):
            info = json.loads(card.get("questionInfo") or "{}")
            typ = card["type"]
            if typ == "Question.TextField":
                answers[card["id"]] = (
                    "1"
                    if info.get("IsNumber")
                    else "SYNTHETIC TEST RESPONSE — workbook provisioning"
                )
            elif typ == "Question.Choice" and info.get("Choices"):
                answers[card["id"]] = info["Choices"][0]["Description"]
            elif typ == "Question.FileUpload":
                # Materialize only after confirmation; preview the generated filename now.
                types = info.get("FileTypes") or {}
                if not info.get("HasSpecificFileType") or types.get("Image"):
                    answers[card["id"]] = {"generated": "synthetic-probe.png"}
                elif types.get("Excel"):
                    answers[card["id"]] = {"generated": "synthetic-probe.xlsx"}
                else:
                    raise FormsError(
                        "seed_attachment_unsupported",
                        "Provide an allowed attachment using forms_fill for this form.",
                    )
            elif card.get("required"):
                raise FormsError(
                    "seed_type_unsupported", f"Cannot safely generate an answer for {typ}."
                )
        return answers

    def materialize(self, preview):
        values = dict(preview)
        folder = self.state_dir / "synthetic" / uuid4().hex
        for qid, value in values.items():
            if isinstance(value, dict) and "generated" in value:
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / value["generated"]
                if path.suffix == ".png":
                    path.write_bytes(
                        base64.b64decode(
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jA6kAAAAASUVORK5CYII="
                        )
                    )
                else:
                    from openpyxl import Workbook

                    workbook = Workbook()
                    workbook.active.append(["SYNTHETIC TEST ATTACHMENT"])
                    workbook.save(path)
                values[qid] = [str(path.resolve())]
        return values

    async def fill(self, form_id, answers):
        form = await self.api.read(form_id)
        questions = {c["id"]: c for c in form.get("questions", [])}
        if not isinstance(answers, dict) or set(answers) - questions.keys():
            raise FormsError("invalid_answers", "Answers must be keyed by existing question IDs.")
        for qid, c in questions.items():
            if c.get("required") and (qid not in answers or answers[qid] in (None, "", [])):
                raise FormsError("missing_answer", f"Required question has no answer: {qid}")
            if qid not in answers:
                continue
            value = answers[qid]
            info = json.loads(c.get("questionInfo") or "{}")
            if c["type"] == "Question.FileUpload":
                if (
                    not isinstance(value, list)
                    or not value
                    or len(value) > info.get("MaxFileCount", 1)
                ):
                    raise FormsError(
                        "invalid_upload",
                        "Upload answers must be a list of local file paths within the file-count limit.",
                    )
                for file in value:
                    if not isinstance(file, str):
                        raise FormsError("invalid_upload", "Each upload path must be a string.")
                    path = Path(file).expanduser().resolve()
                    if (
                        not path.is_file()
                        or path.stat().st_size > info.get("MaxFileSize", 10) * 1024 * 1024
                    ):
                        raise FormsError(
                            "invalid_upload", "An upload is missing or exceeds the size limit."
                        )
            elif c["type"] == "Question.TextField":
                if not isinstance(value, str):
                    raise FormsError(
                        "invalid_answer", "Text answers, including numeric text, must be strings."
                    )
            elif c["type"] == "Question.Choice":
                allowed = [v["Description"] for v in info.get("Choices", [])]
                selected = value if isinstance(value, list) else [value]
                if any(v not in allowed for v in selected):
                    raise FormsError(
                        "invalid_answer", "Choice answers must exactly match option text."
                    )
            else:
                raise FormsError(
                    "unsupported_answer_type", f"Responder support is not measured for {c['type']}."
                )
        # Limit one outstanding response to avoid persisted answer-map collisions.
        if any(d["status"] == "prepared" for d in self.drafts.values()):
            raise FormsError(
                "draft_pending",
                "Submit the existing prepared draft before preparing another response.",
            )
        page = await self.api.session.context.new_page()
        # Measured 2026-10-05: attachments ride a same-origin Forms SPO proxy
        # (PUT /formapi/spo/... -> 202), not a SharePoint CreateUploadSession.
        failed_uploads = []

        def watch(response):
            url = response.url.lower()
            if (
                response.request.method in ("PUT", "POST")
                and ("/formapi/spo/" in url or "uploadsession" in url)
                and response.status >= 400
            ):
                failed_uploads.append(response.status)

        page.on("response", watch)
        await page.goto(
            ORIGIN + "/Pages/ResponsePage.aspx?id=" + form_id, wait_until="domcontentloaded"
        )
        await page.evaluate(
            """fid => {for(const key of Object.keys(localStorage))
            if(key.startsWith('officeforms.answermap.'+fid+'.')) localStorage.removeItem(key); }""",
            form_id,
        )
        await page.reload(wait_until="domcontentloaded")
        visited, filled = set(), set()
        try:
            for _ in range(len(form.get("descriptiveQuestions", [])) + len(questions) + 3):
                await page.locator('[data-automation-id="questionItem"]').first.wait_for(
                    timeout=30000
                )
                visible = []
                for qid, value in answers.items():
                    block = page.locator('[data-automation-id="questionItem"]').filter(
                        has=page.locator(f'[id="QuestionId_{qid}"]')
                    )
                    if not await block.count() or not await block.is_visible():
                        continue
                    visible.append(qid)
                    if qid in filled:
                        continue
                    c = questions[qid]
                    if c["type"] == "Question.TextField":
                        # Playwright supports both INPUT and TEXTAREA and dispatches input events.
                        await block.locator('input:not([type="hidden"]),textarea').first.fill(value)
                    elif c["type"] == "Question.Choice":
                        selected = value if isinstance(value, list) else [value]
                        if await block.locator("select").count():
                            await block.locator("select").select_option(label=selected)
                        else:
                            for option in selected:
                                radio = block.get_by_role("radio", name=option, exact=True)
                                check = block.get_by_role("checkbox", name=option, exact=True)
                                if await radio.count():
                                    await radio.check()
                                elif await check.count():
                                    await check.check()
                                else:
                                    await block.get_by_role("combobox").click()
                                    await page.get_by_role(
                                        "option", name=option, exact=True
                                    ).click()
                    else:
                        paths = [str(Path(v).expanduser().resolve()) for v in value]
                        accept = (
                            await block.locator('input[type="file"]').get_attribute("accept") or ""
                        )
                        extensions = {x.strip().lower() for x in accept.split(",") if x.strip()}
                        if extensions and any(
                            Path(p).suffix.lower() not in extensions for p in paths
                        ):
                            raise FormsError(
                                "invalid_upload",
                                "Attachment extension is not accepted by this question.",
                            )
                        async with page.expect_file_chooser() as pending:
                            await block.locator('[data-automation-id="fileUploadButton"]').click()
                        chooser = await pending.value
                        await chooser.set_files(paths)
                        # Forms swaps the upload control for a per-file delete control carrying
                        # the filename only once the attachment is stored, so that marker plus
                        # the absence of progress is the completion signal. Matched on the
                        # filename inside aria-label rather than on an English verb.
                        await page.wait_for_function(
                            """({qid, names}) => {
                          const title=document.getElementById('QuestionId_'+qid);
                          const block=title?.closest('[data-automation-id="questionItem"]');
                          if(!block || block.querySelector('[role="progressbar"]')) return false;
                          const labels=[...block.querySelectorAll('[aria-label]')]
                            .map(e=>e.getAttribute('aria-label')||'');
                          return names.every(n=>labels.some(l=>l.includes(n)));
                        }""",
                            arg={"qid": qid, "names": [Path(p).name for p in paths]},
                            timeout=180000,
                        )
                        if failed_uploads:
                            raise FormsError(
                                "upload_failed",
                                "Forms rejected an attachment upload; no response was submitted.",
                            )
                    filled.add(qid)
                signature = tuple(sorted(visible))
                submit = page.get_by_role("button", name="Submit", exact=True)
                if await submit.count() and await submit.is_visible():
                    if set(answers) - filled:
                        raise FormsError(
                            "unreachable_answers",
                            "Some answers were not reached; branching is not inferred.",
                        )
                    await page.wait_for_function(
                        """() => [...document.querySelectorAll('button')]
                        .some(b=>b.textContent.trim()==='Submit' && !b.disabled)""",
                        timeout=30000,
                    )
                    draft_id = uuid4().hex
                    self.drafts[draft_id] = {
                        "form_id": form_id,
                        "page": page,
                        "answers": answers,
                        "fingerprint": fingerprint(form),
                        "status": "prepared",
                    }
                    return {
                        "draft_id": draft_id,
                        "status": "prepared",
                        "answers": answers,
                        "warnings": [
                            "Attachments are uploaded during preparation; no response has been submitted."
                        ],
                    }
                if signature in visited:
                    raise FormsError(
                        "navigation_stalled", "Response page did not advance; check validation."
                    )
                visited.add(signature)
                await page.get_by_role("button", name="Next", exact=True).click()
                await page.wait_for_timeout(300)
            raise FormsError("page_limit", "Response form exceeded its expected page count.")
        except Exception:
            await page.close()
            raise

    async def submit(self, draft_id, confirm):
        draft = self.drafts.get(draft_id)
        if not draft:
            raise FormsError(
                "unknown_draft",
                "Draft expired or is unknown. Do not retry an uncertain submission by preparing a new draft.",
            )
        if draft["status"] != "prepared":
            return {"draft_id": draft_id, "status": draft["status"], "retried": False}
        if (
            await confirm(
                "Submit this response? "
                + json.dumps(
                    {"form_id": draft["form_id"], "answers": draft["answers"]}, ensure_ascii=False
                )
            )
            is not True
        ):
            return {"status": "cancelled", "draft_id": draft_id}
        form = await self.api.read(draft["form_id"])
        if fingerprint(form) != draft["fingerprint"]:
            raise FormsError(
                "stale_draft",
                "Form or response count changed after preparation; submission refused.",
            )
        journal_dir = self.state_dir / "submissions"
        journal_dir.mkdir(parents=True, exist_ok=True)
        path = journal_dir / (draft_id + ".json")
        draft["status"] = "uncertain"
        path.write_text(
            json.dumps({"form_id": draft["form_id"], "status": "uncertain"}), encoding="utf-8"
        )
        page = draft["page"]
        try:
            await page.get_by_role("button", name="Submit", exact=True).click()
            await page.wait_for_function(
                "/response (was|has been) submitted|submitted successfully/i.test(document.body.innerText)",
                timeout=30000,
            )
            draft["status"] = "submitted"
            path.write_text(
                json.dumps({"form_id": draft["form_id"], "status": "submitted"}), encoding="utf-8"
            )
            await page.close()
        except Exception:
            # Never replay a submit when the server may already have accepted it.
            return {
                "status": "uncertain",
                "draft_id": draft_id,
                "retried": False,
                "warnings": ["Check Forms responses before attempting any further submission."],
            }
        return {"status": "submitted", "draft_id": draft_id}
