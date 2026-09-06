"""
Table support.

Table cells were invisible to everything except the whole-document replace-all
path, because paragraph extraction only walked the top level of the body. A
targeted replace could not reach a cell, and `get` reported a document as if its
tables were not there.
"""

from __future__ import annotations

import pytest

import docs_edit
from fakes import FakeDocsService, build_doc_with_table, install

DOC_ID = "doc-with-table"

BEFORE = ["Intro paragraph"]
TABLE = [
    ["System", "Tests present"],
    ["OMS", "About 30 tests"],
    ["Hub API", "73 tests"],
]
AFTER = ["Closing paragraph"]


@pytest.fixture
def service(monkeypatch):
    def _make(before=BEFORE, table=TABLE, after=AFTER, **kwargs):
        svc = FakeDocsService(build_doc_with_table(before, table, after), **kwargs)
        install(monkeypatch, docs_edit, svc)
        return svc
    return _make


def test_get_includes_table_cells_in_document_order(service):
    service()
    paragraphs = docs_edit.get(DOC_ID)["paragraphs"]
    texts = [p["text"] for p in paragraphs]

    assert texts == [
        "Intro paragraph",
        "System", "Tests present",
        "OMS", "About 30 tests",
        "Hub API", "73 tests",
        "Closing paragraph",
    ]
    # Indices must stay ascending, which the descending-order batch logic relies on.
    starts = [p["start"] for p in paragraphs]
    assert starts == sorted(starts)


def test_get_marks_which_paragraphs_are_in_a_table(service):
    service()
    paragraphs = docs_edit.get(DOC_ID)["paragraphs"]
    in_table = {p["text"]: p["in_table"] for p in paragraphs}

    assert in_table["Intro paragraph"] is False
    assert in_table["Closing paragraph"] is False
    assert in_table["About 30 tests"] is True


def test_targeted_replace_reaches_a_table_cell(service):
    svc = service()
    result = docs_edit.search_replace(
        DOC_ID, find="About 30 tests", replace="30 tests", occurrence=1
    )

    assert result["occurrences_found"] == 1
    inserts = [r for r in svc.last_requests if "insertText" in r]
    assert len(inserts) == 1
    assert inserts[0]["insertText"]["text"] == "30 tests"
    # And it is still guarded against concurrent edits.
    assert "writeControl" in svc.last_body


def test_regex_replace_all_reaches_cells_and_body(service):
    svc = service(
        before=["tests everywhere"],
        table=[["tests here", "and tests here"]],
        after=["tests at the end"],
    )
    result = docs_edit.search_replace(
        DOC_ID, find=r"tests", replace="suites", occurrence=0, regex=True
    )

    assert result["occurrences_changed"] == 4
    indices = [
        r["insertText"]["location"]["index"]
        for r in svc.last_requests
        if "insertText" in r
    ]
    assert indices == sorted(indices, reverse=True)


def test_match_spanning_two_cells_is_rejected(service):
    service(before=[], table=[["alpha", "beta"]], after=[])

    # "alpha\nbeta" is contiguous in the concatenated text but crosses a cell
    # boundary, so it is not an editable range.
    with pytest.raises(ValueError, match="cell boundary"):
        docs_edit.search_replace(DOC_ID, find="alpha\nbeta", replace="x", occurrence=1)


def test_match_spanning_body_into_a_table_is_rejected(service):
    service(before=["intro"], table=[["alpha", "beta"]], after=[])

    with pytest.raises(ValueError, match="cell boundary"):
        docs_edit.search_replace(DOC_ID, find="intro\nalpha", replace="x", occurrence=1)


def test_body_match_spanning_paragraphs_is_still_allowed(service):
    svc = service(before=["first", "second"], table=[["cell"]], after=[])

    docs_edit.search_replace(DOC_ID, find="first\nsecond", replace="merged", occurrence=1)

    assert any("insertText" in r for r in svc.last_requests)


def test_delete_paragraph_refuses_a_cells_only_paragraph(service):
    service(before=[], table=[["only line here"]], after=["body line"])

    with pytest.raises(ValueError, match="only paragraph in a table cell"):
        docs_edit.delete_paragraph(DOC_ID, anchor="only line here")


def test_delete_paragraph_still_works_on_body_text(service):
    svc = service(before=["remove me"], table=[["keep"]], after=["keep me"])

    result = docs_edit.delete_paragraph(DOC_ID, anchor="remove me")

    assert result["deleted_count"] == 1
    assert "writeControl" in svc.last_body


def test_batch_replace_reaches_table_cells(service):
    svc = service()
    result = docs_edit.batch_replace(DOC_ID, [
        {"find": "About 30 tests", "replace": "30 tests"},
        {"find": "Intro paragraph", "replace": "Overview"},
    ])

    assert result["applied"] == 2
    indices = [
        r["insertText"]["location"]["index"]
        for r in svc.last_requests
        if "insertText" in r
    ]
    assert indices == sorted(indices, reverse=True)


def test_deleting_a_cells_last_paragraph_spares_the_terminating_newline(service):
    # A cell with two paragraphs: deleting the second must not take the newline
    # that terminates the cell, or the API rejects the whole batch.
    svc = service(before=[], table=[["first para", "other cell"]], after=[])
    doc = svc.doc
    cell = doc["body"]["content"][0]["table"]["tableRows"][0]["tableCells"][0]
    extra_start = cell["content"][0]["endIndex"]
    run = "second para\n"
    cell["content"].append({
        "startIndex": extra_start,
        "endIndex": extra_start + len(run),
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [{
                "startIndex": extra_start,
                "endIndex": extra_start + len(run),
                "textRun": {"content": run},
            }],
        },
    })

    docs_edit.delete_paragraph(DOC_ID, anchor="second para")

    rng = svc.last_requests[0]["deleteContentRange"]["range"]
    assert rng["startIndex"] == extra_start - 1
    assert rng["endIndex"] == extra_start + len(run) - 1


def test_deleting_a_cells_first_paragraph_uses_the_plain_range(service):
    svc = service(before=[], table=[["first para", "other cell"]], after=[])
    doc = svc.doc
    cell = doc["body"]["content"][0]["table"]["tableRows"][0]["tableCells"][0]
    first = cell["content"][0]
    extra_start = first["endIndex"]
    run = "second para\n"
    cell["content"].append({
        "startIndex": extra_start,
        "endIndex": extra_start + len(run),
        "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [{
                "startIndex": extra_start,
                "endIndex": extra_start + len(run),
                "textRun": {"content": run},
            }],
        },
    })

    docs_edit.delete_paragraph(DOC_ID, anchor="first para")

    rng = svc.last_requests[0]["deleteContentRange"]["range"]
    assert rng["startIndex"] == first["startIndex"]
    assert rng["endIndex"] == first["endIndex"]
