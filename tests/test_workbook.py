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


def test_clean_workbook_url_preserves_document_identity():
    url = clean_url(
        "https://example.sharepoint.com/Doc.aspx?sourcedoc=%7B123%7D&wdMsFormsCorrelationId=SECRET&file=a.xlsx"
    )
    assert "Correlation" not in url
    assert "sourcedoc=%7B123%7D" in url
