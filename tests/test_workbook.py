import base64
import io
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openpyxl import Workbook

from forms_mcp.errors import FormsError
from forms_mcp.workbook import Workbooks, clean_url, read_export


def fixture_xlsx():
    book = Workbook()
    book.active.append(
        ["ID", "Start time", "Completion time", "Email", "Name", "Last modified time", "Comments"]
    )
    book.active.append([1, "start", "end", "anonymous", None, None, "Historical answer"])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_export_with_false_a1_dimension_preserves_all_answers():
    import re

    source = zipfile.ZipFile(io.BytesIO(fixture_xlsx()))
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as dest:
        for name in source.namelist():
            data = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                data = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', data)
            dest.writestr(name, data)
    parsed = read_export(target.getvalue())
    assert parsed["metadata_columns"] == 6
    assert parsed["question_columns"] == 1
    assert parsed["rows"][0][-1] == "Historical answer"


async def test_backup_refuses_missing_rows(tmp_path):
    f = {"rowCount": 2, "questions": [{}], "version": "v1"}
    api = SimpleNamespace(read=AsyncMock(return_value=f))
    books = Workbooks(api, tmp_path)
    books.responses = AsyncMock(return_value=[{"id": 1}, {"id": 2}])
    books.download = AsyncMock(return_value=fixture_xlsx())
    with pytest.raises(FormsError, match="counts"):
        await books.export("fixture")
    assert not list(tmp_path.iterdir())


async def test_export_range_is_positional_not_response_ids(tmp_path):
    """Ids starting above 1 must still export every row (measured: 8..11 exports nothing)."""
    captured = {}

    async def evaluate(_script, arg):
        captured.update(arg)
        return {"status": 200, "content": base64.b64encode(fixture_xlsx()).decode()}

    api = SimpleNamespace(session=SimpleNamespace(page=SimpleNamespace(evaluate=evaluate)))
    await Workbooks(api, tmp_path).download("fixture", [8, 9, 10, 11])
    assert captured["minId"] == 1
    assert captured["maxId"] >= 11


def stored_xlsx():
    """The stored workbook's own layout: `Id`, five metadata columns, no modified time."""
    book = Workbook()
    book.active.append(["Id", "Start time", "Completion time", "Email", "Name", "Comments"])
    book.active.append([1, "start", "end", "anonymous", None, "Historical answer"])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


async def test_stored_workbook_keeps_a_deleted_questions_column(tmp_path):
    """Measured: the synced file still carries a question Forms no longer has."""

    class Request:
        async def get(self, url, max_redirects=5):
            assert url.endswith(
                "/_layouts/15/download.aspx?UniqueId=0009C106-888D-4986-B531-11954DAA72C4"
            )
            return SimpleNamespace(
                status=200,
                headers={},
                body=AsyncMock(return_value=stored_xlsx()),
                dispose=AsyncMock(),
            )

    session = SimpleNamespace(
        context=SimpleNamespace(request=Request()),
        page=SimpleNamespace(goto=AsyncMock()),
        ready=AsyncMock(),
    )
    books = Workbooks(SimpleNamespace(session=session), tmp_path)
    result = {"warnings": []}
    await books.stored_shape(
        "fixture",
        "https://example.sharepoint.com/personal/u/_layouts/15/Doc.aspx"
        "?sourcedoc=%7B0009C106-888D-4986-B531-11954DAA72C4%7D",
        result,
        {"questions": [], "rowCount": 1},
    )
    assert result["freshness"] == "stored_file"
    assert (result["rows"], result["columns"], result["question_columns"]) == (1, 6, 1)
    assert (result["surplus_columns"], result["missing_columns"]) == (1, 0)


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "sharepoint.com.evil.test",
        "user@example.sharepoint.com",
        "example.sharepoint.com:8080",
    ],
)
async def test_stored_workbook_refuses_untrusted_download_hosts(tmp_path, host):
    session = SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(get=AsyncMock())))
    books = Workbooks(SimpleNamespace(session=session), tmp_path)
    with pytest.raises(FormsError, match="SharePoint"):
        await books.stored_shape(
            "fixture",
            f"https://{host}/personal/u/_layouts/15/Doc.aspx"
            "?sourcedoc=0009C106-888D-4986-B531-11954DAA72C4",
            {"warnings": []},
            {"questions": [], "rowCount": 0},
        )
    session.context.request.get.assert_not_called()


def test_clean_workbook_url_preserves_document_identity():
    url = clean_url(
        "https://example.sharepoint.com/Doc.aspx?sourcedoc=%7B123%7D&wdMsFormsCorrelationId=SECRET&file=a.xlsx"
    )
    assert "Correlation" not in url
    assert "sourcedoc=%7B123%7D" in url
