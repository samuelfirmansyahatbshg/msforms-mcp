"""Immutable plans, explicit confirmation, stale-state checks, durable apply journals."""

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .errors import FormsError
from .operations import cards, desired_matches, patch_payload
from .safety import fingerprint, response_count


def card_sections(form):
    section = None
    result = {}
    for c in cards(form):
        if c["type"] == "Question.ColumnGroup":
            section = c["title"]
        result[c["id"]] = section
    return result


def plan_form(form, edits):
    state = deepcopy(form)
    operations = []
    for edit in edits:
        sections = card_sections(state)
        matches = [
            c
            for c in cards(state)
            if c["title"] == edit["title"]
            and ("section" not in edit or sections[c["id"]] == edit["section"])
        ]
        if len(matches) > 1:
            return {
                "form_id": form["id"],
                "title": form["title"],
                "status": "ambiguous",
                "matching_ids": [c["id"] for c in matches],
            }
        if not matches:
            operations.append({"status": "unmatched", "title": edit["title"]})
            continue
        card = matches[0]
        section = card["type"] == "Question.ColumnGroup"
        payload = patch_payload(card, edit["changes"]) if edit["action"] == "patch" else None
        operation = {
            "status": "planned",
            "action": edit["action"],
            "card_id": card["id"],
            "section": section,
            "before": deepcopy(card),
            "payload": payload,
        }
        if payload is not None:
            if desired_matches(card, payload):
                operation["status"] = "already_applied"
            card.update(payload)
            operation["after"] = deepcopy(card)
        else:
            collection = "descriptiveQuestions" if section else "questions"
            state[collection] = [c for c in state[collection] if c["id"] != card["id"]]
            operation["after"] = None
        operations.append(operation)
    deletions = [op for op in operations if op.get("action") == "delete"]
    count = response_count(form) if deletions else form.get("rowCount")
    return {
        "form_id": form["id"],
        "title": form["title"],
        "status": "planned",
        "before_fingerprint": fingerprint(form),
        "after_fingerprint": fingerprint(state),
        "response_rows": count,
        "deletions": len(deletions),
        "operations": operations,
    }


class Fleet:
    def __init__(self, api, workbooks, state_dir: Path):
        self.api, self.workbooks, self.state_dir = api, workbooks, state_dir
        self.plans = {}

    async def plan(self, selector, edits, skip_backup=False):
        if (
            not isinstance(selector, dict)
            or len(selector) != 1
            or next(iter(selector)) not in {"ids", "title_contains", "all_editable"}
        ):
            raise FormsError(
                "invalid_selector",
                "Use exactly one selector: ids, title_contains, or all_editable:true.",
            )
        if not isinstance(edits, list) or not edits:
            raise FormsError("invalid_edits", "Supply one or more exact-title edits.")
        for edit in edits:
            if (
                not isinstance(edit, dict)
                or set(edit) - {"action", "title", "section", "changes"}
                or edit.get("action") not in {"patch", "delete"}
                or not isinstance(edit.get("title"), str)
                or not edit["title"]
            ):
                raise FormsError(
                    "invalid_edit",
                    "Each edit requires action patch/delete and an exact current title.",
                )
            if edit["action"] == "patch" and not isinstance(edit.get("changes"), dict):
                raise FormsError("invalid_edit", "Patch edits require a changes object.")
            if edit["action"] == "delete" and "changes" in edit:
                raise FormsError("invalid_edit", "Delete edits cannot include changes.")
        if "ids" in selector:
            if (
                not isinstance(selector["ids"], list)
                or not selector["ids"]
                or any(not isinstance(x, str) for x in selector["ids"])
            ):
                raise FormsError("invalid_selector", "ids must be a nonempty list of form IDs.")
            ids = list(dict.fromkeys(selector["ids"]))
        else:
            if "all_editable" in selector and selector["all_editable"] is not True:
                raise FormsError("invalid_selector", "all_editable must explicitly be true.")
            if "title_contains" in selector and (
                not isinstance(selector["title_contains"], str) or not selector["title_contains"]
            ):
                raise FormsError(
                    "invalid_selector", "Use all_editable:true instead of an empty title filter."
                )
            discovery = await self.api.list_forms(selector.get("title_contains", ""))
            if not discovery["complete"]:
                raise FormsError(
                    "incomplete_discovery",
                    "Account discovery is incomplete; use explicit IDs or resolve discovery errors.",
                )
            ids = [f["id"] for f in discovery["forms"]]
        forms = []
        for fid in ids:
            try:
                forms.append(plan_form(await self.api.read(fid), edits))
            except FormsError as exc:
                forms.append({"form_id": fid, "status": "blocked", "reason": str(exc)})
        # Planning writes no files and mutates no Forms state.
        plan = {
            "identity": await self.api.session.identity(),
            "selector": deepcopy(selector),
            "edits": deepcopy(edits),
            "skip_backup": skip_backup,
            "forms": forms,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        plan["plan_id"] = digest
        self.plans[digest] = deepcopy(plan)
        return plan

    def _path(self, plan_id):
        if not re.fullmatch(r"[0-9a-f]{64}", plan_id):
            raise FormsError("invalid_plan", "Expected the plan_id returned by forms_fleet_plan.")
        return self.state_dir / "fleet" / (plan_id + ".json")

    def _save(self, journal):
        path = self._path(journal["plan"]["plan_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix("." + uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(journal, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    async def apply(self, plan_id, confirm):
        path = self._path(plan_id)
        journal = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else {"plan": self.plans.get(plan_id), "forms": {}}
        )
        plan = journal["plan"]
        if not plan:
            raise FormsError(
                "unknown_plan",
                "Plan is unavailable. Generate a new plan; unstarted plans are not persisted.",
            )
        check = deepcopy(plan)
        check.pop("plan_id", None)
        if hashlib.sha256(json.dumps(check, sort_keys=True).encode()).hexdigest() != plan_id:
            raise FormsError("invalid_plan", "Plan contents changed.")
        if plan["identity"] != await self.api.session.identity():
            raise FormsError(
                "wrong_account", "This plan belongs to a different signed-in identity."
            )
        if (
            await confirm(
                "Apply this exact fleet plan? "
                + json.dumps(plan, ensure_ascii=False)
                + " Deleted question answers cannot be restored by this server. Response row counts and backup policy are shown per plan."
            )
            is not True
        ):
            return {"status": "cancelled", "results": []}
        self._save(journal)
        results = []
        for target in plan["forms"]:
            fid = target["form_id"]
            if target["status"] != "planned":
                results.append({"form_id": fid, "status": "skipped", "reason": target["status"]})
                continue
            progress = journal["forms"].setdefault(fid, {"completed": [], "backup": None})
            try:
                if not any(op["status"] == "planned" for op in target["operations"]):
                    results.append(
                        {
                            "form_id": fid,
                            "status": "no_changes",
                            "unmatched": [
                                op["title"]
                                for op in target["operations"]
                                if op["status"] == "unmatched"
                            ],
                        }
                    )
                    continue
                current = await self.api.read(fid)
                observed = fingerprint(current)
                if observed == target["after_fingerprint"]:
                    results.append({"form_id": fid, "status": "already_applied"})
                    continue
                expected = progress.get("fingerprint", target["before_fingerprint"])
                # An uncertain write can be reconciled against its predicted resulting fingerprint.
                pending = progress.get("pending")
                if pending and observed == pending["after_fingerprint"]:
                    progress["completed"].append(pending["index"])
                    progress["fingerprint"] = observed
                    progress.pop("pending")
                    expected = observed
                    self._save(journal)
                if observed != expected:
                    raise FormsError(
                        "stale_plan", "Form or response count drifted; replan this form."
                    )
                # Nothing to export when a form holds no responses (see safety.delete).
                if target["deletions"] and not plan["skip_backup"] and current.get("rowCount"):
                    progress["backup"] = await self.workbooks.export(fid, current)
                    self._save(journal)
                for index, op in enumerate(target["operations"]):
                    if op["status"] != "planned" or index in progress["completed"]:
                        continue
                    current = await self.api.read(fid)
                    if fingerprint(current) != expected:
                        raise FormsError(
                            "stale_plan", "Form changed during execution; remaining edits skipped."
                        )
                    predicted = deepcopy(current)
                    collection = "descriptiveQuestions" if op["section"] else "questions"
                    if op["action"] == "delete":
                        predicted[collection] = [
                            c for c in predicted[collection] if c["id"] != op["card_id"]
                        ]
                    else:
                        next(c for c in predicted[collection] if c["id"] == op["card_id"]).update(
                            op["payload"]
                        )
                    progress["pending"] = {
                        "index": index,
                        "after_fingerprint": fingerprint(predicted),
                    }
                    self._save(journal)
                    await self.api.request(
                        "DELETE" if op["action"] == "delete" else "PATCH",
                        self.api.card_path(fid, op["card_id"], op["section"]),
                        op["payload"],
                    )
                    after = await self.api.read(fid)
                    if fingerprint(after) != fingerprint(predicted):
                        raise FormsError(
                            "verification_failed",
                            "Write returned but reread differs from the planned result.",
                        )
                    expected = fingerprint(after)
                    progress.update(fingerprint=expected)
                    progress["completed"].append(index)
                    progress.pop("pending")
                    self._save(journal)
                results.append(
                    {
                        "form_id": fid,
                        "status": "completed",
                        "backup": progress["backup"],
                        "unmatched": [
                            op["title"]
                            for op in target["operations"]
                            if op["status"] == "unmatched"
                        ],
                    }
                )
            except FormsError as exc:
                results.append(
                    {
                        "form_id": fid,
                        "status": "failed",
                        "reason": str(exc),
                        "code": exc.code,
                        "completed_operations": len(progress["completed"]),
                        "backup": progress["backup"],
                    }
                )
        journal["results"] = results
        self._save(journal)
        return {
            "plan_id": plan_id,
            "status": "partial"
            if any(r["status"] in ("failed", "skipped") for r in results)
            else "completed",
            "results": results,
        }
