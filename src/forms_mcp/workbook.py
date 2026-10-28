"""Fresh Forms exports and associated-workbook diagnostics, with browser-only auth."""

import base64
import hashlib
import io
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from openpyxl import load_workbook

from .errors import FormsError
from .ids import ORIGIN, api_root

EXPORT_JS = """async ({formId, minId, maxId}) => {
 const url = new URL('/formapi/DownloadExcelFile.ashx', location.origin);
 // Measured: this is a 1-based row range despite the parameter names; 0 returns 403.
 url.search = new URLSearchParams({formid:formId, timezoneOffset:'0', __TimezoneId:'UTC',
   minResponseId:String(minId), maxResponseId:String(maxId)});
 const r = await fetch(url, {credentials:'same-origin', headers:{
   __requestverificationtoken:window.OfficeFormServerInfo?.antiForgeryToken||''}});
 if(!r.ok) return {status:r.status};
 const bytes=new Uint8Array(await r.arrayBuffer());
 if(bytes.length>100*1024*1024) return {status:413};
 let binary=''; for(let i=0;i<bytes.length;i+=32768) binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
 return {status:r.status, content:btoa(binary)};
}"""


def clean_url(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise FormsError("invalid_workbook_url", "Workbook URL must be HTTPS.")
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(
                [
                    (k, v)
                    for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if k.lower() != "wdmsformscorrelationid"
                ]
            ),
            "",
        )
    )


def read_export(content):
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            if len(wb.worksheets) != 1:
                raise FormsError(
                    "export_shape", "Expected one response worksheet; export layout changed."
                )
            sheet = wb.worksheets[0]
            # Measured Forms exports falsely report A1:A1 even with many rows/columns.
            sheet.reset_dimensions()
            rows = list(sheet.values)
        finally:
            wb.close()
    except FormsError:
        raise
    except Exception:
        raise FormsError(
            "invalid_export", "The download is not a readable response workbook."
        ) from None
    if not rows:
        raise FormsError("empty_export", "Response export has no header.")
    headers = list(rows[0])
    if [str(h).strip().lower() for h in headers[:5]] != [
        "id",
        "start time",
        "completion time",
        "email",
        "name",
    ]:
        raise FormsError(
            "unknown_export_layout",
            "Unrecognized response metadata headers; cannot validate backup.",
        )
    meta = 6 if len(headers) > 5 and headers[5] == "Last modified time" else 5
    records = [list(r) for r in rows[1:] if any(v is not None for v in r)]
    return {
        "headers": headers,
        "rows": records,
        "metadata_columns": meta,
        "question_columns": len(headers) - meta,
        "row_count": len(records),
    }


class Workbooks:
    def __init__(self, api, state_dir: Path):
        self.api = api
        self.state_dir = state_dir

    async def responses(self, form_id):
        return await self.api.pages(api_root(form_id) + f"/forms('{form_id}')/responses")

    async def download(self, form_id, response_ids):
        """Fetch a fresh Forms response export in page context.

        The UI's own "Download a copy" issues this same same-origin GET, but routes it
        through a popup whose lifetime Playwright ties the artifact to — a popup that
        closes itself mid-stream, cancelling the download. Fetching the request directly
        removes that race.

        Measured 2026-10-05: despite the names, min/maxResponseId are a 1-BASED POSITIONAL
        row range, not response ids. On a form whose four responses carry ids 8-11, `8..11`
        exported zero rows while `1..4` and `1..11` both returned all four. 0 returns 403.
        So the range starts at 1, and the upper bound covers the row count and the highest
        id, which is correct under either reading. The caller validates the result against
        the live form regardless.
        """
        if not response_ids:
            raise FormsError("no_responses", "There are no responses to export.")
        result = await self.api.session.page.evaluate(
            EXPORT_JS,
            {
                "formId": form_id,
                "minId": 1,
                "maxId": max(len(response_ids), max(response_ids)),
            },
        )
        if result["status"] != 200:
            raise FormsError(
                "export_failed",
                f"Forms response export returned HTTP {result['status']}; no backup was written.",
            )
        return base64.b64decode(result["content"], validate=True)

    async def export(self, form_id, expected_form=None, save=True):
        before = expected_form or await self.api.read(form_id)
        count = before.get("rowCount")
        if type(count) is not int or count < 0:
            raise FormsError(
                "unknown_response_count", "A verified response count is required for export."
            )
        responses = await self.responses(form_id)
        if len(responses) != count or len({r.get("id") for r in responses}) != count:
            raise FormsError(
                "incomplete_responses", "Response pagination/count is inconsistent; backup refused."
            )
        content = await self.download(form_id, [r["id"] for r in responses])
        parsed = read_export(content)
        if parsed["row_count"] != count or parsed["question_columns"] != len(
            before.get("questions", [])
        ):
            raise FormsError(
                "incomplete_backup",
                "Export row/column counts differ from the live form; deletion refused.",
            )
        expected_ids = {r["id"] for r in responses}
        if {r[0] for r in parsed["rows"]} != expected_ids:
            raise FormsError("incomplete_backup", "Export response IDs differ from live responses.")
        after = await self.api.read(form_id)
        after_responses = await self.responses(form_id)
        if (
            after.get("rowCount") != count
            or after.get("version") != before.get("version")
            or responses != after_responses
        ):
            raise FormsError(
                "backup_changed", "Responses or form changed during export; retry before deletion."
            )
        result = {
            "row_count": count,
            "question_columns": parsed["question_columns"],
            "metadata_columns": parsed["metadata_columns"],
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        if save:
            directory = self.state_dir / "backups"
            directory.mkdir(parents=True, exist_ok=True)
            basename = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex
            path = directory / (basename + ".xlsx")
            with path.open("xb") as stream:
                stream.write(content)
            # Raw response snapshot supplies exact question IDs and preserves attachment references.
            from .api import redact

            snapshot = directory / (basename + ".json")
            with snapshot.open("x", encoding="utf-8") as stream:
                json.dump(
                    redact({"form": before, "responses": responses}), stream, ensure_ascii=False
                )
            result.update(path=str(path.resolve()), snapshot_path=str(snapshot.resolve()))
        return result

    async def status(self, form_id):
        form = await self.api.read(form_id)
        raw_url = form.get("sdxWebUrl")
        result = {
            "associated": bool(raw_url),
            "url": clean_url(raw_url) if raw_url else None,
            "live_questions": len(form.get("questions", [])),
            "response_count": form.get("rowCount"),
            "resolves": None,
            "rows": None,
            "columns": None,
            "question_columns": None,
            "surplus_columns": None,
            "missing_columns": None,
            "freshness": "unknown",
            "warnings": [],
        }
        if not raw_url:
            result["warnings"].append(
                "No associated workbook URL is exposed. Provision explicitly; inspection submits nothing."
            )
            return result
        proxy = ORIGIN + "/formapi/msgraph/v1.0/"
        share = "u!" + base64.urlsafe_b64encode(raw_url.encode()).decode().rstrip("=")
        try:
            item = (await self.api.request("GET", proxy + "shares/" + share + "/driveItem"))["data"]
            result["resolves"] = True
            root = (
                proxy
                + "drives/"
                + quote(item["parentReference"]["driveId"], safe="")
                + "/items/"
                + quote(item["id"], safe="")
            )
            sheets = await self.api.pages(root + "/workbook/worksheets")
            candidates = []
            for sheet in sheets:
                data = (
                    await self.api.request(
                        "GET",
                        root
                        + "/workbook/worksheets/"
                        + quote(sheet["id"], safe="")
                        + "/usedRange(valuesOnly=true)",
                    )
                )["data"]
                values = data.get("values") or []
                if values and values[0][:5] == [
                    "ID",
                    "Start time",
                    "Completion time",
                    "Email",
                    "Name",
                ]:
                    candidates.append(values)
            if len(candidates) != 1:
                raise FormsError(
                    "unknown_workbook_layout", "Cannot identify a unique response sheet."
                )
            values = candidates[0]
            meta = 6 if len(values[0]) > 5 and values[0][5] == "Last modified time" else 5
            columns = len(values[0])
            question_columns = columns - meta
            rows = sum(1 for r in values[1:] if r and r[0] not in (None, ""))
            result.update(
                rows=rows,
                columns=columns,
                question_columns=question_columns,
                surplus_columns=max(0, question_columns - result["live_questions"]),
                missing_columns=max(0, result["live_questions"] - question_columns),
                freshness="live_usedRange",
            )
            if rows != form.get("rowCount"):
                result["warnings"].append(
                    "Workbook rows differ from Forms responses; sync is incomplete."
                )
        except FormsError as exc:
            result["warnings"].append(f"{exc.code}: {exc}")
            try:
                await self.stored_shape(form_id, raw_url, result, form)
            except FormsError as fallback:
                result["warnings"].append(f"{fallback.code}: {fallback}")
                result["warnings"].append(
                    "Stored workbook columns remain unknown. A fresh Forms export is not the stored workbook."
                )
        return result

    async def stored_shape(self, form_id, raw_url, result, form):
        """Read the stored workbook itself when the Graph workbook API is denied.

        Measured 2026-10-27: `/workbook/worksheets` and `/workbook/createSession` return
        403 `OpenWorkbookAccessDenied` in this tenant, and the proxy's pre-authenticated
        `@microsoft.graph.downloadUrl` returns 401 from a session that has never visited
        SharePoint. The file's own site serves it:
        `GET {site}/_layouts/15/download.aspx?UniqueId={guid}` returned the 11 KB XLSX once
        the dedicated browser had loaded that site once (silent SSO, no extra sign-in).
        The stored sheet's first header is `Id`, not the fresh export's `ID`.
        """
        parts = urlsplit(clean_url(raw_url))
        if (
            not parts.hostname
            or not parts.hostname.endswith(".sharepoint.com")
            or parts.username is not None
            or parts.password is not None
            or parts.netloc.lower() != parts.hostname.lower()
        ):
            raise FormsError(
                "unsafe_workbook_url", "Stored downloads require an HTTPS SharePoint site."
            )
        unique = (parse_qs(parts.query).get("sourcedoc") or [""])[0].strip("{}")
        if "/_layouts/" not in parts.path or not re.fullmatch(
            r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}", unique
        ):
            raise FormsError("unknown_workbook_url", "Workbook URL carries no document id.")
        site_path = parts.path.split("/_layouts/")[0]
        if site_path and not re.fullmatch(r"/(?:personal|sites|teams)/[A-Za-z0-9_%-]+", site_path):
            raise FormsError("unsafe_workbook_url", "Unrecognized SharePoint site path.")
        site = f"{parts.scheme}://{parts.netloc}{site_path}"
        target = f"{site}/_layouts/15/download.aspx?UniqueId={unique}"
        content = await self.fetch_file(target)
        if content[:2] != b"PK":
            # Sign-in HTML instead of the file: establish this site's session once.
            try:
                await self.api.session.page.goto(site + "/", wait_until="domcontentloaded")
            except Exception:
                raise FormsError(
                    "workbook_signin_failed", "Could not establish the workbook site's session."
                ) from None
            finally:
                await self.api.session.ready(form_id)
            content = await self.fetch_file(target)
        if content[:2] != b"PK":
            raise FormsError(
                "stored_workbook_unreadable", "The workbook site did not return the file."
            )
        parsed = read_export(content)
        headers = parsed["headers"][parsed["metadata_columns"] :]
        live = Counter(c.get("title") for c in form.get("questions", []))
        stored = Counter(headers)
        result.update(
            rows=parsed["row_count"],
            columns=len(parsed["headers"]),
            question_columns=parsed["question_columns"],
            surplus_columns=sum((stored - live).values()),
            missing_columns=sum((live - stored).values()),
            freshness="stored_file",
        )
        if parsed["row_count"] != form.get("rowCount"):
            result["warnings"].append(
                "Workbook rows differ from Forms responses; sync is incomplete."
            )

    async def fetch_file(self, url):
        response = None
        try:
            # Do not follow an unvalidated cross-origin redirect carrying session credentials.
            response = await self.api.session.context.request.get(url, max_redirects=0)
            if response.status != 200:
                return b""
            length = response.headers.get("content-length")
            if length and int(length) > 100 * 1024 * 1024:
                raise FormsError("workbook_too_large", "Stored workbook exceeds the read limit.")
            body = await response.body()
            if len(body) > 100 * 1024 * 1024:
                raise FormsError("workbook_too_large", "Stored workbook exceeds the read limit.")
            return body
        except FormsError:
            raise
        except Exception:
            raise FormsError(
                "stored_workbook_unreadable", "Could not download the stored workbook."
            ) from None
        finally:
            if response is not None:
                await response.dispose()

    async def provision(self, form_id):
        form = await self.api.read(form_id)
        if form.get("sdxWebUrl"):
            return {
                "status": "already_associated",
                "url": clean_url(form["sdxWebUrl"]),
                "warnings": ["Association does not prove response synchronization."],
            }
        if not form.get("rowCount"):
            raise FormsError(
                "seed_required", "Prepare and confirm a synthetic response before provisioning."
            )
        # Measured GET has an external side effect; this is a WRITE tool despite the verb.
        result = await self.api.request(
            "GET", api_root(form_id) + f"/forms('{form_id}')/xl/exportFormExcel?enableOpenXML=true"
        )
        url = (result.get("data") or {}).get("webUrl")
        if not url:
            raise FormsError(
                "provision_unverified",
                "Provisioning returned no workbook URL; inspect before retrying.",
            )
        reread = await self.api.read(form_id)
        return {
            "status": "completed" if reread.get("sdxWebUrl") else "verification_failed",
            "url": clean_url(url),
            "warnings": ["Workbook association verified separately from cell synchronization."],
        }
