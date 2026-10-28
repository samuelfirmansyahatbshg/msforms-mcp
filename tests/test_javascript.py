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
 writes.push({method,url,body});
 const sub=url.match(/\\('(r[0-9a-f]+)'\\)\\/choices$/);
 if(sub) { const q=form.questions.find(c=>c.id===sub[1]);
  q.choices=q.choices||[]; q.choices.push({...structuredClone(body),id:q.choices.length+1});
  return {status:201,data:body}; }
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
        + ";const first=await build({root:'https://fixture',formId:'fixture',spec});const count=writes.length;const second=await build({root:'https://fixture',formId:'fixture',spec});const extraWrites=writes.length-count;const normalized=FORMS.normalize(form);const ranking=form.questions.find(c=>c.type==='Question.Ranking');if(ranking)ranking.choices.reverse().forEach((c,i)=>c.order=i+1);const drift=ranking?await build({root:'https://fixture',formId:'fixture',spec}):null;console.log(JSON.stringify({first,second,extraWrites,normalized,drift,form}));})().catch(e=>{console.error(e);process.exit(1)});"
    )
    result = subprocess.run(
        [node, "--input-type=commonjs", "-"],
        input=program,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_repeated_titles_resume_as_distinct_questions():
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
    cards = result["normalized"]
    assert [(c["title"], c["type"]) for c in cards] == [
        ("Details", "Section"),
        ("Notes", "Text"),
        ("Notes", "Text"),
        ("Pick", "Choice"),
        ("Attach", "Upload"),
    ]
    assert cards[1]["id"] != cards[2]["id"]
    assert cards[1]["multiline"] is True and cards[2]["number"] is True
    assert cards[3]["options"] == ["Yes", "No"]
    assert cards[4]["max_file_size_mb"] == 100


def test_halted_create_refuses_to_duplicate_orphan():
    result = run_builder([{"t": "Text", "q": "Notes"}], fail_title=True)
    assert result["first"]["status"] == "partial"
    assert result["second"]["reason"] == "untitled_orphan"
    assert result["extraWrites"] == 0


def test_ranking_option_reordering_blocks_resume_instead_of_silently_accepting_drift():
    result = run_builder(
        [
            {
                "t": "Rating",
                "q": "Service",
                "levels": 10,
                "shape": "Number",
                "left": "Low",
                "right": "High",
            },
            {"t": "Date", "q": "When"},
            {"t": "Ranking", "q": "Order these", "opts": ["Alpha", "Beta"]},
            {"t": "NPS", "q": "Recommend"},
        ]
    )
    assert result["first"]["status"] == "completed"
    assert result["second"]["status"] == "already_applied"
    assert result["extraWrites"] == 0
    assert result["normalized"][2]["options"] == ["Alpha", "Beta"]
    assert result["drift"]["reason"] == "existing_property_drift"
    assert result["drift"]["mismatches"] == [
        {
            "index": 2,
            "id": result["normalized"][2]["id"],
            "field": "options",
            "actual": ["Beta", "Alpha"],
            "expected": ["Alpha", "Beta"],
        }
    ]
