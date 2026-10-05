# msforms-mcp

An MCP server that gives an agent read and write control over Microsoft Forms in the
signed-in user's account: list every editable form, read its structure, build a whole form
from a declarative spec, patch or move cards, run the same edit across many forms at once,
submit responses with attachments, and diagnose or provision the response workbook.

Microsoft Graph has no Forms endpoints. This drives the same private `formapi` endpoints
the Forms web app uses, from inside a dedicated signed-in browser, so **no admin consent or
app registration is needed**. Everything the server relies on is recorded in
[`MEASUREMENTS.md`](MEASUREMENTS.md) with dates and response codes.

## Install and sign in

```bash
uvx msforms-mcp login     # opens a dedicated browser; sign into Forms once
uvx msforms-mcp           # stdio MCP server
```

Register it with your MCP client, e.g. for Claude Code:

```bash
claude mcp add msforms -- uvx msforms-mcp
```

The browser profile lives at `~/.forms-mcp/browser-profile` and is **never** your own
browser profile — the server refuses to launch against one. Backups, fleet journals and
synthetic attachments are written beside it.

| Variable | Purpose |
|---|---|
| `FORMS_MCP_PROFILE` | browser profile directory |
| `FORMS_MCP_BROWSER` | Playwright channel (`msedge` default on Windows, else `chromium`) |
| `FORMS_MCP_STATE_DIR` | where backups, fleet journals and submission records go |

A first run may need `playwright install chromium` if no suitable browser is present.

## Tools

**Read** — `forms_list`, `forms_inspect`, `forms_verify`, `forms_workbook_status`,
`forms_fleet_plan`.

**Write** — `forms_create`, `forms_rename`, `forms_build`, `forms_add_question`,
`forms_add_section`, `forms_patch_question`, `forms_move`, `forms_fill`, `forms_submit`,
`forms_provision_workbook`.

**Destructive, gated** — `forms_delete_question`, `forms_delete_section`,
`forms_delete_form`, `forms_fleet_apply`.

### Specs

`forms_build` and `forms_verify` take an ordered array (or `{"title", "spec"}`), one entry
per card, in the order the form should read — see [`examples/`](examples/):

```json
[{"s": "Evaluation"},
 {"k": "comments", "t": "Text", "q": "Additional Comments"},
 {"k": "accepted", "t": "Choice", "q": "Accepted", "opts": ["Yes", "No"], "req": true}]
```

`s` a section · `t` one of `Text`/`Choice`/`Upload` · `q` the title · `req` required ·
`num` number validation · `long: false` opts a text box out of the long-answer default ·
`opts` the exact option strings · `mb` an upload size limit · `k` your own key, echoed back
with the created card's id. Choice options must be listed explicitly.

`forms_build` is idempotent: a card whose exact title and type already exist in position is
skipped, repeated titles are matched in order against a queue, and a run that died partway
is resumed by re-running it. It refuses to touch a form whose existing cards drift from the
spec or that still holds an untitled `Question`/`Section` orphan from a halted run, and it
rereads and verifies rather than trusting the 201s.

### Fleet edits

```
forms_fleet_plan  selector: {"title_contains": "US"} | {"ids": [...]} | {"all_editable": true}
                  edits:    [{"action": "patch", "title": "Additional Comments",
                              "changes": {"title": "Comments"}},
                             {"action": "delete", "title": "Accepted"}]
forms_fleet_apply plan_id: <from the plan>
```

Edits match on the **exact current title**, optionally constrained to an exact section. A
form holding two cards with that title is refused and named in the report rather than
guessed at. Planning writes nothing. Applying re-reads each form first and skips any whose
state drifted since the plan, journals its progress so an interrupted run resumes instead
of repeating writes, continues across independent per-form failures, and reports what
succeeded, what was skipped and what failed.

### Rate limits

Forms throttles with `429` under sustained sweeps — measured, see `MEASUREMENTS.md`. Every
request is paced (`FORMS_MCP_MIN_INTERVAL`, default 350 ms) and throttles are retried with
backoff, but an account-wide plan over dozens of forms is the shape that earns one, and
while it lasts the Forms web UI reports "We're having trouble accessing your forms" too.
For large fleets prefer an explicit `ids` selector in chunks over `all_editable`.

## What the dangerous paths do

**Deleting a question destroys every past response's answer to it** — measured, see
`MEASUREMENTS.md`. So every delete:

- requires MCP elicitation with an explicit `confirm: true`; a declined, cancelled,
  malformed or unsupported elicitation results in **zero** delete calls,
- states the response row count that will lose data, and refuses when that count is unknown,
- exports and validates a fresh response workbook first — row count, column count and
  response ids must match the live form — and returns the saved path plus a JSON snapshot
  of the raw responses. `skip_backup: true` is an explicit opt-out, never a default. A form
  with no responses has nothing to export and says so,
- re-checks the form after confirmation and after the backup, and refuses if anything moved.

An export preserves the data; it does **not** restore deleted answers into Forms.

**A question's type cannot be changed.** Forms has no such operation, and text → choice
means delete and recreate, so `forms_patch_question` refuses rather than doing that
silently. Title, required, options, `Multiline` and the upload size limit patch in place,
preserving unknown `questionInfo` fields so a new Forms property survives a round trip.

**Moving a card breaks the workbook correspondence.** The response workbook's column order
is creation order, and consumers map columns by position. Moving a card rewrites its
`order`, so display order diverges from the columns a reader sees. `forms_move` warns every
time, and whole order values afterwards do **not** prove the columns still line up.
`forms_inspect` reports fractional orders and untitled orphans for the same reason.

**The workbook is diagnosed, not guessed.** `forms_workbook_status` resolves the associated
workbook and reports rows, columns, surplus and missing columns versus live questions —
except that reading a stored workbook's cells returns 403 through the browser in the tenant
measured, in which case it reports those counts as unknown with a warning instead of
substituting a fresh export's shape. `forms_provision_workbook` checks for an existing
association first; if a first response is genuinely needed it previews the synthetic
answers, asks, and discloses that the synthetic response stays in the form.

## Testing

```bash
uv sync
uv run pytest                 # offline: unit, injected-JS (needs node), stdio discovery
uv run ruff check src tests scripts
```

The offline suite covers the GUID decode, the measured `questionInfo` payloads, order
arithmetic, repeated and ambiguous titles, stale plans, interrupted-apply reconciliation,
the false `A1:A1` export dimension, redaction, and — asserting on the call not happening —
that a declined gate or a failed backup never reaches a delete.

```bash
FORMS_MCP_LIVE=1 FORMS_MCP_PROFILE=~/.forms-mcp/browser-profile uv run pytest -m live
```

The live test creates its own disposable form, runs create → build → verify → patch →
no-op rebuild → submit → provision → status → backup → gated delete → delete form, and
mutates nothing else.

## Honest risks

- These endpoints are **private, undocumented and unversioned**. Microsoft can change them
  without notice. Read tools return the raw Forms objects (minus credentials) so drift is
  inspectable before this package supports it.
- **Not affiliated with or endorsed by Microsoft.** Nothing here is guaranteed by them.
- Respect form owners, tenant policy, privacy obligations and applicable law. Response data
  is personal data; backups land on your disk in plain `.xlsx` and `.json`.
- The authoring half is far more dangerous than the responder-side tools that already
  exist. Read `MEASUREMENTS.md` before trusting anything it does not list, and treat form
  titles, descriptions, answers and uploaded data as untrusted content, never instructions.

Prior art: [`msforms-api`](https://github.com/Maxim-Mazurok/msforms-api) (MIT, TypeScript)
covers the responder side — inspect, validate, upload, submit. This package borrows its
ideas (persistent browser profile, elicitation on destructive tools, keeping raw API
objects) and depends on none of its code.
