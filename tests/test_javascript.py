"""Exercise the actual injected scripts with an in-memory HTTP boundary, no network."""

import json
import shutil
import subprocess

import pytest

from forms_mcp.api import script


def run_builder(spec, fail_title=False):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for offline injected-JS tests")
    program = """
global.window=global; global.setTimeout=(fn)=>{fn();return 0};
const form={questions:[],descriptiveQuestions:[]}, writes=[];
window.__formsRequest=async ({url,method,body})=>{
 if(method==='GET') return {status:200,data:structuredClone(form)};
 writes.push({method,body});
 const section=url.includes('descriptiveQuestions'), rows=section?form.descriptiveQuestions:form.questions;
 if(method==='POST') { rows.push(structuredClone(body));return {status:201,data:body}; }
 if(FAIL && body.title) return {status:503};
 const id=url.match(/\\('(r[0-9a-f]+)'\\)$/)[1];
 Object.assign(rows.find(c=>c.id===id),body); return {status:204};
};
""".replace("FAIL", "true" if fail_title else "false")
    program += script("forms_dump.js") + ";\nconst build=(" + script("forms_build.js") + ");\n"
    program += (
        "(async()=>{const spec="
        + json.dumps(spec)
        + ";const first=await build({root:'https://fixture',formId:'fixture',spec});const count=writes.length;const second=await build({root:'https://fixture',formId:'fixture',spec});console.log(JSON.stringify({first,second,writes,extraWrites:writes.length-count,form}));})().catch(e=>{console.error(e);process.exit(1)});"
    )
    result = subprocess.run(
        [node, "--input-type=commonjs", "-"],
        input=program,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_measured_payloads_and_repeated_title_resume():
    result = run_builder(
        [
            {"s": "Details"},
            {"t": "Text", "q": "Notes"},
            {"t": "Text", "q": "Notes", "num": True},
            {"t": "Choice", "q": "Pick", "opts": ["Yes", "No"]},
            {"t": "Upload", "q": "Attach", "mb": 100},
        ]
    )
    assert result["first"]["status"] == "completed"
    assert result["second"]["status"] == "already_applied"
    assert result["extraWrites"] == 0
    posts = [x["body"] for x in result["writes"] if x["method"] == "POST"]
    assert len(posts) == 5
    assert all(c["title"] in ("Question", "Section") for c in posts)
    assert json.loads(posts[1]["questionInfo"]) == {
        "Multiline": True,
        "ShuffleOptions": False,
        "ShowRatingLabel": False,
    }
    assert json.loads(posts[2]["questionInfo"])["NumberValidationRule"] == "IsNumber"
    assert json.loads(posts[3]["questionInfo"])["Choices"][0] == {
        "Description": "Yes",
        "FormsProDisplayRTText": "Yes",
    }
    assert json.loads(posts[4]["questionInfo"])["MaxFileSize"] == 10
    assert json.loads(result["form"]["questions"][-1]["questionInfo"])["MaxFileSize"] == 100


def test_halted_create_refuses_to_duplicate_orphan():
    result = run_builder([{"t": "Text", "q": "Notes"}], fail_title=True)
    assert result["first"]["status"] == "partial"
    assert result["second"]["reason"] == "untitled_orphan"
    assert result["extraWrites"] == 0
