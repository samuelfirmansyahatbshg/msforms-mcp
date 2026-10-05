"""Browser-only Forms API library. Callers serialize complete operations with session.lock."""

import asyncio
import json
import re
from importlib.resources import files
from urllib.parse import urljoin, urlsplit

from .errors import FormsError
from .ids import ORIGIN, api_root


def script(name: str) -> str:
    return files("forms_mcp").joinpath("js", name).read_text(encoding="utf-8")


def redact(value):
    if isinstance(value, dict):
        return {
            k: redact(v)
            for k, v in value.items()
            if not any(s in k.lower() for s in ("token", "cookie", "authorization", "secret"))
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class FormsAPI:
    def __init__(self, session):
        self.session = session

    async def request(self, method: str, url: str, body=None, *, allow_missing=False):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc != "forms.cloud.microsoft"
            or not parts.path.startswith("/formapi/")
        ):
            raise FormsError("unsafe_endpoint", "Refused request outside the Forms origin.")
        for attempt in range(4):
            if method != "GET":
                await self.session.pace()
            try:
                result = await self.session.page.evaluate(
                    script("request.js"), {"url": url, "method": method, "body": body}
                )
            except Exception:
                raise FormsError(
                    "uncertain_result" if method != "GET" else "network_error",
                    "Browser request interrupted; reread state before retrying a write.",
                ) from None
            status = result["status"]
            if status in (200, 201, 204) or (allow_missing and status == 404):
                return result
            if method == "GET" and status == 401 and attempt == 0:
                await self.session.page.reload(wait_until="domcontentloaded")
                await self.session.page.wait_for_function(
                    "!!window.OfficeFormServerInfo?.antiForgeryToken"
                )
                continue
            if method == "GET" and status in (429, 503) and attempt < 3:
                try:
                    delay = float(result.get("retryAfter") or 1.5 * 2**attempt)
                except ValueError:
                    delay = 1.5 * 2**attempt
                await asyncio.sleep(min(max(delay, 0.12), 60))
                continue
            raise FormsError(
                f"http_{status}",
                f"Forms {method} returned HTTP {status}. No response body or credentials logged.",
            )

    async def prepare(self, form_id=None):
        if form_id:
            api_root(form_id)  # Validate before navigation.
        await self.session.ready(form_id)

    async def read(self, form_id: str) -> dict:
        result = await self.request(
            "GET",
            api_root(form_id) + f"/light/forms('{form_id}')?$expand=questions,descriptiveQuestions",
        )
        if not isinstance(result["data"], dict):
            raise FormsError("invalid_response", "Forms did not return a JSON form.")
        return result["data"]

    async def user_root(self):
        identity = await self.session.identity()
        if not all(identity.get(k) for k in ("tenant", "owner")):
            raise FormsError("login_required", "No signed-in Forms identity.")
        return f"{ORIGIN}/formapi/api/{identity['tenant']}/users/{identity['owner']}"

    async def pages(self, url):
        rows, seen = [], set()
        while url:
            if url in seen:
                raise FormsError(
                    "pagination_loop", "Forms repeated a pagination URL; discovery is incomplete."
                )
            seen.add(url)
            result = await self.request("GET", url)
            data = result["data"]
            if not isinstance(data, dict) or not isinstance(data.get("value"), list):
                raise FormsError(
                    "invalid_collection", "Forms collection shape changed; discovery is incomplete."
                )
            rows.extend(data["value"])
            next_url = data.get("@odata.nextLink") or data.get("odata.nextLink")
            url = urljoin(url, next_url) if next_url else None
        return rows

    async def list_forms(self, title_filter: str = ""):
        root = await self.user_root()
        identity = await self.session.identity()
        found, errors = {}, []

        async def collect(url, ownership):
            try:
                for form in await self.pages(url):
                    if form.get("softDeleted"):
                        continue
                    if not isinstance(form.get("id"), str):
                        raise FormsError(
                            "discovery_shape", "Discovery returned an item without a form ID."
                        )
                    found[form["id"]] = {**form, "ownership": ownership}
            except FormsError as exc:
                errors.append({"source": ownership, "code": exc.code, "message": str(exc)})

        # No $top: measured home request returns all owned forms with this projection.
        select = "?$select=id,title,createdDate,modifiedDate,rowCount,softDeleted"
        await collect(root + "/light/forms" + select, "owned")
        await collect(ORIGIN + "/formapi/api/sharedWithMeForms", "shared")
        try:
            groups = await self.pages(ORIGIN + "/formapi/api/groups")
            for group in groups:
                group_id = str(group.get("id", ""))
                import uuid

                uuid.UUID(group_id)
                await collect(
                    f"{ORIGIN}/formapi/api/{identity['tenant']}/groups/{group_id}/light/forms"
                    + select,
                    "group",
                )
        except (FormsError, ValueError) as exc:
            errors.append({"source": "groups", "message": str(exc)})
        result = []
        for fid, item in found.items():
            if title_filter.casefold() not in str(item.get("title", "")).casefold():
                continue
            try:
                form = await self.read(fid)
                result.append(
                    {
                        "id": fid,
                        "title": form.get("title"),
                        "ownership": item["ownership"],
                        "question_count": len(form.get("questions") or []),
                        "response_count": form.get("rowCount"),
                        "created": form.get("createdDate"),
                        "modified": form.get("modifiedDate"),
                    }
                )
            except FormsError as exc:
                errors.append({"form_id": fid, "code": exc.code, "message": str(exc)})
        return {
            "forms": result,
            "complete": not errors,
            "errors": errors,
            "coverage": ["owned", "group", "shared"],
        }

    async def create(self, title):
        if not isinstance(title, str) or not title.strip() or title.strip() == "Untitled form":
            raise FormsError("invalid_title", "Choose a nonempty title other than Untitled form.")
        identity = await self.session.identity()
        settings = {
            "RequiresUniqueResponse": False,
            "IsAnonymous": False,
            "NotRecordIdentity": True,
            "IsQuizMode": False,
            "PermissionForResponder": 1,
        }
        result = await self.request(
            "POST",
            await self.user_root() + "/forms",
            {
                "title": title.strip(),
                "ownerId": identity["owner"],
                "ownerTenantId": identity["tenant"],
                "progressBarEnabled": "false",
                "settings": json.dumps(settings, separators=(",", ":")),
            },
        )
        fid = (result.get("data") or {}).get("id")
        if not fid:
            raise FormsError(
                "uncertain_result",
                "Create returned no form ID; inspect the owned list before retrying.",
            )
        live = await self.read(fid)
        return {
            "id": fid,
            "title": live.get("title"),
            "verified": live.get("title") == title.strip(),
        }

    async def rename(self, form_id, title):
        if not isinstance(title, str) or not title.strip():
            raise FormsError("invalid_title", "A nonempty title is required.")
        await self.request("PATCH", api_root(form_id) + f"/forms('{form_id}')", {"title": title})
        live = await self.read(form_id)
        return {
            "id": form_id,
            "title": live.get("title"),
            "verified": live.get("title") == title,
            "warnings": ["An existing workbook is not renamed."],
        }

    async def inspect(self, form_id):
        form = await self.read(form_id)
        await self.session.page.evaluate(script("forms_dump.js"))
        cards = await self.session.page.evaluate("f=>FORMS.normalize(f)", form)
        return {
            "id": form["id"],
            "title": form["title"],
            "response_count": form.get("rowCount"),
            "cards": cards,
            "fractional_orders": [c["id"] for c in cards if c["fractional_order"]],
            "untitled_orphans": [c["id"] for c in cards if c["orphan"]],
            "raw": redact(form),
        }

    async def inject(self):
        await self.session.page.evaluate(
            "() => { window.__formsRequest = (" + script("request.js") + "); }"
        )
        await self.session.page.evaluate(script("forms_dump.js"))

    async def build(self, form_id, spec, dry=False):
        from .spec import validate_spec

        spec = validate_spec(spec)
        await self.inject()
        return await self.session.page.evaluate(
            script("forms_build.js"),
            {"root": api_root(form_id), "formId": form_id, "spec": spec, "dry": dry},
        )

    async def verify(self, form_id, spec):
        from .spec import validate_spec

        spec = validate_spec(spec)
        await self.inject()
        form = await self.read(form_id)
        mismatches = await self.session.page.evaluate(
            "({form,spec})=>FORMS.verify(form,spec)", {"form": form, "spec": spec}
        )
        return {"matches": not mismatches, "mismatches": mismatches}

    @staticmethod
    def card_path(form_id, card_id, section=False):
        if not re.fullmatch(r"r[0-9a-f]{32}", card_id):
            raise FormsError("invalid_card_id", "Expected an r-prefixed 32-hex card ID.")
        return (
            api_root(form_id)
            + f"/forms('{form_id}')/"
            + ("descriptiveQuestions" if section else "questions")
            + f"('{card_id}')"
        )
