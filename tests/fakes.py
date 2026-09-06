"""
Test doubles for the Google Docs API.

No network, no credentials. A FakeDocsService mimics the small slice of
`service.documents()` that docs_edit.py uses, records every batchUpdate body,
and can replay those requests against a plain-text model of the document so
that index arithmetic is checked rather than assumed.
"""

from __future__ import annotations

DEFAULT_REVISION = "rev-abc-123"


def build_doc(paragraphs: list[str], revision_id: str = DEFAULT_REVISION) -> dict:
    """
    Build a Docs API `documents().get()` response body.

    Google Docs indices are 1-based and every paragraph ends with a newline
    that occupies a real index, which is what the mapping code relies on.
    """
    content = []
    index = 1
    for text in paragraphs:
        run_text = text + "\n"
        start, end = index, index + len(run_text)
        content.append({
            "startIndex": start,
            "endIndex": end,
            "paragraph": {
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "elements": [{
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": run_text},
                }],
            },
        })
        index = end
    return {
        "title": "Fake Doc",
        "revisionId": revision_id,
        "body": {"content": content},
    }


def build_doc_with_table(
    before: list[str],
    table: list[list[str]],
    after: list[str],
    revision_id: str = DEFAULT_REVISION,
) -> dict:
    """
    Build a document with a table between two runs of body paragraphs.

    Cell paragraphs carry real document indices, exactly as the API returns
    them, so index arithmetic in the code under test is exercised for real.
    """
    content = []
    index = 1

    def paragraph(text):
        nonlocal index
        run = text + "\n"
        start, end = index, index + len(run)
        index = end
        return {
            "startIndex": start,
            "endIndex": end,
            "paragraph": {
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "elements": [{
                    "startIndex": start,
                    "endIndex": end,
                    "textRun": {"content": run},
                }],
            },
        }

    for text in before:
        content.append(paragraph(text))

    table_start = index
    rows = []
    for row in table:
        cells = []
        for cell_text in row:
            cells.append({"content": [paragraph(cell_text)]})
        rows.append({"tableCells": cells})
    content.append({
        "startIndex": table_start,
        "endIndex": index,
        "table": {"rows": len(table), "columns": len(table[0]), "tableRows": rows},
    })

    for text in after:
        content.append(paragraph(text))

    return {
        "title": "Fake Doc With Table",
        "revisionId": revision_id,
        "body": {"content": content},
    }


def doc_buffer(paragraphs: list[str]) -> str:
    """
    Plain-text model of the same document, aligned to Docs indices.

    Position 0 is a filler character so that buffer[i] is the character at
    document index i.
    """
    return "\0" + "".join(p + "\n" for p in paragraphs)


def apply_requests(buffer: str, requests: list[dict]) -> str:
    """
    Replay batchUpdate requests against the plain-text model.

    The real API applies requests sequentially, each one seeing the effects of
    the previous ones. This mirrors that, so a wrongly ordered batch produces
    visibly wrong text instead of passing silently.
    """
    for req in requests:
        if "insertText" in req:
            spec = req["insertText"]
            at = spec["location"]["index"]
            buffer = buffer[:at] + spec["text"] + buffer[at:]
        elif "deleteContentRange" in req:
            rng = req["deleteContentRange"]["range"]
            buffer = buffer[:rng["startIndex"]] + buffer[rng["endIndex"]:]
        else:
            raise AssertionError(f"unsupported request in replay: {req}")
    return buffer


class _Executable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeDocuments:
    def __init__(self, service):
        self._service = service

    def get(self, documentId, **kwargs):
        self._service.get_calls.append({"documentId": documentId, **kwargs})
        return _Executable(self._service.doc)

    def batchUpdate(self, documentId, body):
        self._service.batch_calls.append({"documentId": documentId, "body": body})
        if self._service.batch_error is not None:
            return _Executable(self._service.batch_error)
        replies = [
            {"replaceAllText": {"occurrencesChanged": self._service.occurrences_changed}}
            for _ in body.get("requests", [])
        ]
        return _Executable({"replies": replies})


class FakeDocsService:
    def __init__(self, doc: dict, occurrences_changed: int = 0, batch_error=None):
        self.doc = doc
        self.occurrences_changed = occurrences_changed
        self.batch_error = batch_error
        self.get_calls: list[dict] = []
        self.batch_calls: list[dict] = []

    def documents(self):
        return _FakeDocuments(self)

    # --- convenience accessors -------------------------------------------
    @property
    def last_body(self) -> dict:
        assert self.batch_calls, "no batchUpdate was issued"
        return self.batch_calls[-1]["body"]

    @property
    def last_requests(self) -> list[dict]:
        return self.last_body.get("requests", [])


def install(monkeypatch, docs_edit_module, service: FakeDocsService) -> FakeDocsService:
    """Point docs_edit at the fake service instead of a real authed client."""
    monkeypatch.setattr(docs_edit_module, "_get_service", lambda *a, **k: service)
    return service
