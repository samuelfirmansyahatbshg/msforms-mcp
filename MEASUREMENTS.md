# Measurements

What was confirmed against the live service, when, and with which response codes. These
endpoints are private and undocumented; treat anything not listed here as unverified.

All live work below ran on 2026-10-05 against `forms.cloud.microsoft` in one Microsoft 365
business tenant, through a dedicated signed-in browser profile, and mutated only
uniquely-named disposable forms created for the purpose and deleted afterwards.

## Inherited evidence (not re-derived here)

From `../oven-report-generator/scripts/` (`forms_build.js`, `forms_dump.js`,
`forms_creation_order.js`, `forms_drive.py`, `FORMS_PLAYBOOK.md`): the request headers, the
doubly-encoded `questionInfo` strings, the three-call create sequence, the `light` read
projection versus the writable path, the ~120 ms write pacing, and the form-id → API-root
decode. This package re-implements those and re-verified each one in the runs below.

## Authentication and transport

| Fact | Status |
|---|---|
| `formapi` requires the signed-in page's cookie plus `OfficeFormServerInfo.antiForgeryToken` | confirmed (no server-side route) |
| Token read live per call, never cached across a session | implemented; `401` on a read triggers one reload-and-retry |
| `forms.cloud.microsoft` needs this network's PAC proxy from a fresh browser profile | confirmed — the session passes `--proxy-pac-url` from the registry |
| Azure app-registration mode (`Forms.ReadWrite.All`) | **not implemented, not measured** |

## Reads

| Request | Result |
|---|---|
| `GET {root}/light/forms('{f}')?$expand=questions,descriptiveQuestions` | 200 |
| `GET {root}/light/forms?$select=id,title,createdDate,modifiedDate,rowCount,softDeleted` | 200, owned forms |
| `GET /formapi/api/groups` | 200, `value[].id` group IDs |
| `GET /formapi/api/{tenant}/groups/{groupId}/light/forms` | 200 |
| `GET /formapi/api/sharedWithMeForms` | 200, **empty on this account** — the non-empty shape is unverified |
| `GET {root}/forms('{f}')/responses` | 200, one record per response with answers as a JSON string |

Combined discovery returned 55 unique forms with no per-source errors, 3 of them
group-owned. `forms_list` reports `complete` and `errors`, and `forms_fleet_plan` refuses
an account-wide selector when discovery is incomplete.

`parentId` and `sectionId` were `null` on every card: section membership is implied by
`order` relative to section cards, with no explicit parent field.

## Writes

| Request | Result |
|---|---|
| `POST {root}/forms` with `{title}` only | 201 and the form is editable, **but its response page fails** ("Sorry, something went wrong") |
| `POST {root}/forms` with the UI's own payload | 201 and the response page works; a response was submitted successfully |
| `PATCH {root}/forms('{f}')` `{title}` | 204, verified by reread |
| `POST {root}/forms('{f}')/questions` / `/descriptiveQuestions` | 201 |
| `PATCH …/questions('{q}')` / `…/descriptiveQuestions('{q}')` | 204 |
| `PATCH …/questions('{q}')` `{order: 3500000}` | 204 — fractional orders are accepted and move a card across a section boundary |

The UI's create payload, which `forms_create` reproduces:

```json
{"title": "...", "ownerId": "<signed-in user>", "ownerTenantId": "<tenant>",
 "progressBarEnabled": "false",
 "settings": "{\"RequiresUniqueResponse\":false,\"IsAnonymous\":false,\"NotRecordIdentity\":true,\"IsQuizMode\":false,\"PermissionForResponder\":1}"}
```

`settings` is a JSON **string**; `progressBarEnabled` is the string `"false"`. The two
ownership fields are read from the live session, never from a fixture.

## Deletes, and the answer loss that matters

| Request | Result |
|---|---|
| `DELETE {root}/forms('{f}')/questions('{q}')` | 204 |
| `DELETE {root}/forms('{f}')/descriptiveQuestions('{q}')` | 204 |
| `DELETE {root}/forms('{f}')` | 204; a following `light` read returns 404 |

**Deleting a question destroys its historical answers — confirmed, not assumed.** A form
with one submitted response was exported, the text question deleted, and the form exported
again. The first export carried `Probe Text` and its answer; the second had neither, while
the other question's answer remained. This is why every delete path requires elicitation
and a validated export first.

**Deleting a section does not delete the questions inside it.** After deleting the second
of two sections, the question that had been under it remained in the form, with its own
`order` unchanged, and its answer stayed in the export. It is not re-parented to a new
card; it simply sits after the preceding section by order.

## Response workbook

| Fact | Status |
|---|---|
| The workbook only exists after the first response | confirmed; `forms_provision_workbook` refuses to invent one and asks before submitting a synthetic response |
| `GET {root}/forms('{f}')/xl/exportFormExcel?enableOpenXML=true` | 200 `{webUrl, serverDocId, resourceId, webDavUrl}` — **a GET with a side effect**: it associates the workbook, verified by the form then exposing `sdxWebUrl` |
| Workbook identity fields appear on the form only once it exists | `sdxWebUrl`, `sdxWorkbookId`, `xlServerDocId`, `sdxWorkbookFileName`, `sdxWorkbookParentFolder`, `sdxWorkbookOwner` |
| `GET /formapi/msgraph/v1.0/shares/{u!base64url(whole URL)}/driveItem` | 200 — resolves `driveId`/`id` without a separate Graph sign-in |
| `GET …/drives/{driveId}/items/{id}/workbook/worksheets` | **403 `OpenWorkbookAccessDenied`** through the browser proxy |

So **stored-workbook cell and column counts are not readable browser-only in this
tenant.** `forms_workbook_status` reports `resolves: true`, leaves `rows`/`columns`/
`surplus_columns` as `null` with `freshness: "unknown"`, and says so in its warnings rather
than substituting a fresh export's shape for the stored file's.

### Fresh response export (what backups use)

`GET /formapi/DownloadExcelFile.ashx?formid=&timezoneOffset=&__TimezoneId=&minResponseId=&maxResponseId=`
→ 200 with an XLSX body.

- `minResponseId=0` → **403**. It must be a real response id; the backup passes the
  observed minimum and maximum.
- A same-origin `fetch` from the Forms page works. The UI's "Download a copy" issues the
  same request through a popup that closes itself mid-stream, which cancelled the download
  under Playwright (`Download.save_as: Target page … has been closed`) — one run in three.
  Fetching directly removes that race.
- **The exported worksheet declares a false dimension of `A1:A1`.** A read-only reader
  that trusts it sees one cell and silently loses every answer. `read_export` calls
  `reset_dimensions()` before reading.
- Metadata headers are `ID, Start time, Completion time, Email, Name, Last modified time`
  (6 columns), then one column per live question. The metadata width is detected, not
  hardcoded at 5.

## Responder

| Fact | Status |
|---|---|
| The response page restores a persisted draft from `localStorage` key `officeforms.answermap.{formId}.*` | confirmed — including an already-completed upload, so `forms_fill` clears it first |
| Attachment upload: `PUT /formapi/spo/{tenant}/users/{owner}/forms('{f}')/…` | **202**, a same-origin Forms proxy |
| `/CreateUploadSession` and a direct SharePoint `PUT` | **not used in this tenant** — the `msforms-api` reference's signals never fire here |
| Upload completion marker | the upload control is replaced by a per-file control whose `aria-label` contains the filename, with no `[role=progressbar]`; matched on the filename, not an English verb |
| `Multiline` text renders as `<textarea>` | confirmed |

A submit whose confirmation is lost is reported as `uncertain` and **never replayed**.

## End-to-end runs

- `tests/test_live.py` (opt-in): create → build → verify → patch → re-run build is a no-op
  → fill with answers → submit → repeat submit is a no-op → provision workbook → status →
  export backup → delete question behind the gate → export shows the column gone → delete
  form. Passed.
- Fleet, the driving use case, on two disposable forms each holding one response: plan
  reported both forms, before/after titles, response rows and one deletion each while
  writing nothing; apply completed with a validated 1-row backup per form before the
  delete; the result was `Evaluation` + `Comments`; re-applying the same plan reported
  `already_applied` with no second mutation; a **fresh** plan with the same edits reported
  `unmatched` rather than claiming the edit had been applied.

## Rate limiting — hit, not theorised (2026-10-05)

Renaming one card across 27 forms in a real account tripped Forms' throttle. What was
observed:

- The sequence that caused it: a 52-form scan (two full reads each), then two
  account-wide `forms_fleet_plan` runs, each of which read all 52 forms again — roughly
  400 requests in half an hour.
- The response is **HTTP 429**, first on the per-form `$expand` reads and, at its peak, on
  the collection endpoints (`light/forms`, `groups`, `sharedWithMeForms`) as well. No
  `Retry-After` header was returned.
- It is **not** momentary: short retries (1.5s × 4) never outlasted it, and the Forms *web
  UI* showed "We're having trouble accessing your forms" for the same account while it
  lasted. It cleared on its own in well under an hour with no action taken.
- Nothing was corrupted. Throttled writes simply did not happen, which is why retrying
  them is safe.

Consequences now in the code: every request is paced (`FORMS_MCP_MIN_INTERVAL`, default
350 ms, reads included); 429, 503 and HTML-bodied 403 are retried with 4s doubling to 64s
over 6 attempts; `forms_fleet_plan` reuses discovery's reads instead of reading every form
a second time. Even so, **target ids and work in chunks** for anything fleet-wide — an
account-wide sweep is the shape that earns a throttle.

A separate failure worth recording: the visible automation browser was closed by hand
mid-run twice. Writes in flight surfaced as `uncertain_result`, were **not** marked done,
and the fleet journal reconciled them against each operation's predicted fingerprint on
resume rather than writing twice.

## Still unmeasured — do not build on these without probing

- Writing to a **group-owned or shared** form (discovery works; the group write path is untested).
- The non-empty shape of `sharedWithMeForms`.
- Question types beyond Text, Choice and file Upload (Rating, Date, Ranking, Likert, NPS, quizzes).
- Branching forms, and any form whose answers reveal later pages conditionally.
- Whether a stored, already-synced workbook keeps a deleted question's column — the
  measurement above is of fresh exports, and the stored file's cells are 403 here.
- Whether the limit in "Rate limiting" below is per minute, per hour or per request type.
- Any tenant whose Forms UI is not in English: several waits key on `Submit`/`Next` names.
