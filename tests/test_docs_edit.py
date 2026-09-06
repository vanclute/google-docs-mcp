"""
Correctness tests for docs_edit.py.

Three bugs are covered:
  1. regex + occurrence=0 replaced only the last match
  2. the non-regex match loop produced overlapping matches
  3. mutators wrote back with no revision assertion (writeControl)
"""

from __future__ import annotations

import pytest

import docs_edit
from fakes import (
    DEFAULT_REVISION,
    FakeDocsService,
    apply_requests,
    build_doc,
    doc_buffer,
    install,
)

DOC_ID = "doc-under-test"


@pytest.fixture
def service(monkeypatch):
    def _make(paragraphs, **kwargs):
        svc = FakeDocsService(build_doc(paragraphs), **kwargs)
        install(monkeypatch, docs_edit, svc)
        svc.paragraphs = paragraphs
        return svc
    return _make


# ---------------------------------------------------------------------------
# Bug 2 — overlapping matches
# ---------------------------------------------------------------------------

def test_find_matches_does_not_overlap():
    assert docs_edit.find_matches("aaaa", "aa") == [(0, 2), (2, 4)]


def test_find_matches_overlap_free_across_a_longer_needle():
    # "abab" inside "abababab" occurs twice, not three times.
    assert docs_edit.find_matches("abababab", "abab") == [(0, 4), (4, 8)]


def test_find_matches_rejects_empty_needle():
    # An empty needle must not be allowed to drive the scan loop.
    with pytest.raises(ValueError):
        docs_edit.find_matches("abc", "")


def test_occurrence_numbering_uses_non_overlapping_matches(service):
    svc = service(["aaaa"])
    result = docs_edit.search_replace(DOC_ID, find="aa", replace="X", occurrence=2)

    assert result["occurrences_found"] == 2
    # Second non-overlapping match starts at full-text offset 2 -> doc index 3.
    assert result["at_index"] == 3
    final = apply_requests(doc_buffer(svc.paragraphs), svc.last_requests)
    assert final[1:] == "aaX\n"


# ---------------------------------------------------------------------------
# Bug 1 — regex replace-all
# ---------------------------------------------------------------------------

def test_regex_replace_all_hits_every_match(service):
    svc = service(["cat cat cat"])
    result = docs_edit.search_replace(
        DOC_ID, find=r"c.t", replace="dog", occurrence=0, regex=True
    )

    assert result["occurrences_changed"] == 3
    inserts = [r for r in svc.last_requests if "insertText" in r]
    assert len(inserts) == 3
    assert {r["insertText"]["text"] for r in inserts} == {"dog"}


def test_regex_replace_all_is_applied_in_descending_index_order(service):
    svc = service(["cat cat cat"])
    docs_edit.search_replace(
        DOC_ID, find=r"c.t", replace="dogs", occurrence=0, regex=True
    )

    insert_indices = [
        r["insertText"]["location"]["index"]
        for r in svc.last_requests
        if "insertText" in r
    ]
    assert insert_indices == sorted(insert_indices, reverse=True)

    # Replaying the batch must produce the document we actually wanted.
    final = apply_requests(doc_buffer(svc.paragraphs), svc.last_requests)
    assert final[1:] == "dogs dogs dogs\n"


def test_regex_replace_all_spans_paragraphs(service):
    svc = service(["alpha one", "beta one", "gamma one"])
    result = docs_edit.search_replace(
        DOC_ID, find=r"one", replace="two", occurrence=0, regex=True
    )

    assert result["occurrences_changed"] == 3
    final = apply_requests(doc_buffer(svc.paragraphs), svc.last_requests)
    assert final[1:] == "alpha two\nbeta two\ngamma two\n"


def test_regex_replace_all_deleting_text(service):
    svc = service(["keep AAA keep AAA"])
    docs_edit.search_replace(
        DOC_ID, find=r"A+", replace="", occurrence=0, regex=True
    )

    final = apply_requests(doc_buffer(svc.paragraphs), svc.last_requests)
    assert final[1:] == "keep  keep \n"


# ---------------------------------------------------------------------------
# Bug 3 — writeControl / revision assertion
# ---------------------------------------------------------------------------

def _assert_revision_asserted(svc):
    body = svc.last_body
    assert "writeControl" in body, "batchUpdate body carried no writeControl"
    assert body["writeControl"] == {"requiredRevisionId": DEFAULT_REVISION}
    # The id must come from the read that produced the indices.
    assert svc.get_calls, "no documents().get() preceded the write"


def test_search_replace_asserts_revision(service):
    svc = service(["hello world"])
    docs_edit.search_replace(DOC_ID, find="world", replace="there")
    _assert_revision_asserted(svc)


def test_regex_replace_all_asserts_revision(service):
    svc = service(["cat cat"])
    docs_edit.search_replace(DOC_ID, find="c.t", replace="dog", occurrence=0, regex=True)
    _assert_revision_asserted(svc)


def test_insert_after_asserts_revision(service):
    svc = service(["anchor para", "other"])
    docs_edit.insert_after(DOC_ID, anchor="anchor", text="new", rich=False)
    _assert_revision_asserted(svc)


def test_insert_before_asserts_revision(service):
    svc = service(["anchor para", "other"])
    docs_edit.insert_before(DOC_ID, anchor="anchor", text="new", rich=False)
    _assert_revision_asserted(svc)


def test_delete_paragraph_asserts_revision(service):
    svc = service(["kill me", "keep me"])
    docs_edit.delete_paragraph(DOC_ID, anchor="kill me")
    _assert_revision_asserted(svc)


def test_batch_replace_asserts_revision(service):
    svc = service(["one two three"])
    docs_edit.batch_replace(DOC_ID, [{"find": "one", "replace": "1"}])
    _assert_revision_asserted(svc)


def test_append_asserts_revision(service):
    svc = service(["only para"])
    docs_edit.append(DOC_ID, text="tail", rich=False)
    _assert_revision_asserted(svc)


def test_revision_conflict_raises_a_clear_error(service, monkeypatch):
    class FakeResp:
        status = 400

    class FakeHttpError(Exception):
        def __init__(self):
            super().__init__("Invalid requiredRevisionId: the document has changed")
            self.resp = FakeResp()
            self.status_code = 400

    svc = service(["hello world"], batch_error=FakeHttpError())
    with pytest.raises(docs_edit.DocumentChangedError) as excinfo:
        docs_edit.search_replace(DOC_ID, find="world", replace="there")

    message = str(excinfo.value).lower()
    assert "changed" in message
    assert "not applied" in message


def test_unrelated_api_errors_are_not_relabelled(service):
    class FakeResp:
        status = 403

    class FakeHttpError(Exception):
        def __init__(self):
            super().__init__("The caller does not have permission")
            self.resp = FakeResp()
            self.status_code = 403

    service(["hello world"], batch_error=FakeHttpError())
    with pytest.raises(FakeHttpError):
        docs_edit.search_replace(DOC_ID, find="world", replace="there")
