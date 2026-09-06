#!/usr/bin/env python3
"""
docs_edit.py — Surgical Google Docs editor
==========================================
Edits Google Docs in-place using real batchUpdate requests, preserving
document history, comments, and suggestions. LLMs never touch indices.

Usage (CLI):
    docs_edit.py get <docId>
    docs_edit.py search_replace <docId> --find "old" --replace "new" [--occurrence 1] [--regex]
    docs_edit.py insert_after <docId> --anchor "paragraph text" --text "new text" [--plain]
    docs_edit.py insert_before <docId> --anchor "paragraph text" --text "new text" [--plain]
    docs_edit.py delete_paragraph <docId> --anchor "paragraph text"
    docs_edit.py append <docId> --text "text to append" [--plain]
    docs_edit.py batch_replace <docId> --replacements '[{"find":"a","replace":"b"}]'

Rich mode is ON by default and supports a small markdown-like subset:
    # / ## / ### headings
    - item / * item bullet lists
    1. item / 1) item numbered lists
    **bold**, *italic*, ***bold italic***

Auth (in priority order):
    1. GOOGLE_DOCS_MCP_TOKEN env var  → path to standalone token JSON
    2. GOOGLE_DOCS_TOKEN_FILE env var → legacy gog-exported token JSON
    3. Default standalone path        → ~/.google-docs-mcp/token.json
    4. GOG_KEYRING_PASSWORD env var   → auto-export from gog
    5. Cached gog token               → /tmp/docs_edit_token_cache.json

Legacy GOOGLE_DRIVE_MCP_* env var aliases still work.

Python API:
    from docs_edit import get, search_replace, insert_after, insert_before,
                          delete_paragraph, append, batch_replace
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("docs_edit")

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

# Standalone defaults (auth_setup.py output)
STANDALONE_TOKEN_PATHS = [
    Path.home() / ".google-docs-mcp" / "token.json",
    Path.home() / ".google-drive-mcp" / "token.json",
]
TOKEN_ENV_VAR = "GOOGLE_DOCS_MCP_TOKEN"
TOKEN_ENV_ALIASES = (TOKEN_ENV_VAR, "GOOGLE_DRIVE_MCP_TOKEN")
CLIENT_ID_ENV_VAR = "GOOGLE_DOCS_MCP_CLIENT_ID"
CLIENT_SECRET_ENV_VAR = "GOOGLE_DOCS_MCP_CLIENT_SECRET"
CLIENT_ID_ENV_ALIASES = (CLIENT_ID_ENV_VAR, "GOOGLE_DRIVE_MCP_CLIENT_ID")
CLIENT_SECRET_ENV_ALIASES = (CLIENT_SECRET_ENV_VAR, "GOOGLE_DRIVE_MCP_CLIENT_SECRET")
APPS_SCRIPT_ID_ENV_VAR = "GOOGLE_DOCS_MCP_APPS_SCRIPT_ID"
APPS_SCRIPT_ID_ENV_ALIASES = (APPS_SCRIPT_ID_ENV_VAR, "GOOGLE_DRIVE_MCP_APPS_SCRIPT_ID")
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCRIPT_API_BASE = "https://script.googleapis.com/v1"

# Gog fallback paths (backward compat)
GOG_CREDENTIALS_PATH = Path("/config/gogcli/credentials.json")
GOG_TOKEN_CACHE = Path("/tmp/docs_edit_token_cache.json")


def _export_gog_token(email: str = "david@harriethq.com") -> dict:
    """Export token from gog keyring using GOG_KEYRING_PASSWORD env var."""
    password = os.environ.get("GOG_KEYRING_PASSWORD")
    if not password:
        raise RuntimeError("GOG_KEYRING_PASSWORD not set")
    env = {**os.environ, "GOG_KEYRING_PASSWORD": password}
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = f.name
    try:
        result = subprocess.run(
            ["gog", "auth", "tokens", "export", email, "--out", tmp, "--overwrite", "-y"],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"gog token export failed: {result.stderr}")
        return json.loads(Path(tmp).read_text())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _load_token() -> dict:
    """
    Load token data. Priority:
      1. GOOGLE_DOCS_MCP_TOKEN env var   (standalone auth_setup.py output)
      2. GOOGLE_DRIVE_MCP_TOKEN env var  (backward-compatible alias)
      3. GOOGLE_DOCS_TOKEN_FILE env var  (legacy / gog compat)
      4. ~/.google-docs-mcp/token.json   (standalone default location)
      5. ~/.google-drive-mcp/token.json  (backward-compatible fallback)
      6. GOG_KEYRING_PASSWORD auto-export (gog compat)
      7. Cached gog token
    """
    # 1. Standalone env vars
    token_file = _first_env(*TOKEN_ENV_ALIASES)
    if token_file:
        return json.loads(Path(token_file).read_text())

    # 2. Legacy env var
    token_file = os.environ.get("GOOGLE_DOCS_TOKEN_FILE")
    if token_file:
        return json.loads(Path(token_file).read_text())

    # 3. Default standalone locations
    for token_path in STANDALONE_TOKEN_PATHS:
        if token_path.exists():
            return json.loads(token_path.read_text())

    # 4. Gog auto-export
    try:
        token = _export_gog_token()
        GOG_TOKEN_CACHE.write_text(json.dumps(token))
        return token
    except RuntimeError as e:
        log.warning("Gog auto-export failed: %s", e)

    # 5. Gog cache
    if GOG_TOKEN_CACHE.exists():
        log.info("Using cached gog token")
        return json.loads(GOG_TOKEN_CACHE.read_text())

    raise RuntimeError(
        "No Google credentials found.\n"
        "Run: python3 auth_setup.py --credentials ~/credentials.json\n"
        "Or set GOOGLE_DOCS_MCP_TOKEN to your token file path "
        "(legacy GOOGLE_DRIVE_MCP_TOKEN also works)."
    )


def _load_creds():
    """Return refreshed google.oauth2.credentials.Credentials."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_data = _load_token()

    # Self-contained token — auth_setup.py embeds client_id / client_secret directly
    # (also handle legacy _client_id / _client_secret with underscore prefix)
    client_id = token_data.get("client_id") or token_data.get("_client_id")
    client_secret = token_data.get("client_secret") or token_data.get("_client_secret")
    token_uri = token_data.get("token_uri") or token_data.get("_token_uri", "https://oauth2.googleapis.com/token")

    # Environment override allows using external client credentials instead of a credentials.json file.
    client_id = client_id or _first_env(*CLIENT_ID_ENV_ALIASES)
    client_secret = client_secret or _first_env(*CLIENT_SECRET_ENV_ALIASES)

    # Gog compat: read from separate credentials.json if not embedded
    if not client_id and GOG_CREDENTIALS_PATH.exists():
        gog_creds = json.loads(GOG_CREDENTIALS_PATH.read_text())
        client_id = gog_creds.get("client_id")
        client_secret = gog_creds.get("client_secret")

    if not client_id or not client_secret:
        raise RuntimeError(
            "No OAuth client credentials found. Re-run auth_setup.py to generate a self-contained token, "
            f"or set {CLIENT_ID_ENV_VAR} and {CLIENT_SECRET_ENV_VAR} "
            "(legacy GOOGLE_DRIVE_MCP_* aliases also work)."
        )

    scopes = [s for s in token_data.get("scopes", []) if s not in ("email", "openid")]

    creds = Credentials(
        token=None,
        refresh_token=token_data["refresh_token"],
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )
    creds.refresh(Request())
    return creds


def _get_service(api: str = "docs", version: str = "v1"):
    """Build a Google API service client."""
    from googleapiclient.discovery import build
    return build(api, version, credentials=_load_creds())


SUGGESTIONS_VIEW_MODE = "SUGGESTIONS_INLINE"


def _get_document(service, doc_id: str) -> dict:
    """
    Fetch a document with inline suggestions so returned indices stay usable
    for later batchUpdate calls.
    """
    return service.documents().get(
        documentId=doc_id,
        suggestionsViewMode=SUGGESTIONS_VIEW_MODE,
    ).execute()


def _refresh_access_token_stdlib() -> str:
    """Refresh an OAuth access token without requiring googleapiclient."""
    token_data = _load_token()
    payload = urllib.parse.urlencode(
        {
            "client_id": token_data["client_id"],
            "client_secret": token_data["client_secret"],
            "refresh_token": token_data["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))["access_token"]


def _apps_script_api_request(
    access_token: str,
    method: str,
    path: str,
    body: dict | None = None,
) -> dict:
    req = urllib.request.Request(f"{SCRIPT_API_BASE}{path}", method=method)
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            payload = json.loads(raw)
            message = payload.get("error", {}).get("message", raw)
        except json.JSONDecodeError:
            message = raw
        raise RuntimeError(f"Apps Script API request failed ({e.code}): {message}") from None


def _build_bookmark_bridge_files() -> list[dict]:
    code = r'''
function createBookmarkAtText(docId, anchorText, occurrence) {
  var doc = DocumentApp.openById(docId);
  var body = doc.getBody();
  var wanted = occurrence || 1;
  var found = null;

  for (var i = 0; i < wanted; i++) {
    found = body.findText(anchorText, found);
    if (!found) {
      throw new Error('Anchor text not found for occurrence ' + wanted + ': ' + anchorText);
    }
  }

  var elem = found.getElement();
  var start = found.getStartOffset();
  var end = found.getEndOffsetInclusive();
  var pos = doc.newPosition(elem, start);
  var bookmark = doc.addBookmark(pos);
  return {
    bookmarkId: bookmark.getId(),
    matchText: elem.asText().getText().substring(start, end + 1)
  };
}
'''.strip()

    manifest = {
        "timeZone": "Europe/London",
        "exceptionLogging": "STACKDRIVER",
        "runtimeVersion": "V8",
        "oauthScopes": ["https://www.googleapis.com/auth/documents"],
        "executionApi": {"access": "MYSELF"},
    }

    return [
        {"name": "Code", "type": "SERVER_JS", "source": code},
        {"name": "appsscript", "type": "JSON", "source": json.dumps(manifest)},
    ]


def _create_bookmark_via_apps_script(
    doc_id: str,
    anchor_text: str,
    occurrence: int = 1,
    *,
    script_id: str | None = None,
) -> dict:
    script_id = script_id or _first_env(*APPS_SCRIPT_ID_ENV_ALIASES)
    if not script_id:
        raise RuntimeError(
            "bookmark_jump requires a persistent Apps Script bridge. "
            f"Set {APPS_SCRIPT_ID_ENV_VAR} to the script project ID "
            "(legacy GOOGLE_DRIVE_MCP_APPS_SCRIPT_ID also works)."
        )

    access_token = _refresh_access_token_stdlib()
    _apps_script_api_request(
        access_token,
        "PUT",
        f"/projects/{script_id}/content",
        {"files": _build_bookmark_bridge_files()},
    )

    result = _apps_script_api_request(
        access_token,
        "POST",
        f"/scripts/{script_id}:run",
        {
            "function": "createBookmarkAtText",
            "parameters": [doc_id, anchor_text, occurrence],
            "devMode": True,
        },
    )

    if "error" in result:
        details = result.get("error", {}).get("details", [])
        detail = details[0] if details else {}
        message = detail.get("errorMessage") or result.get("error", {}).get("message") or json.dumps(result["error"])
        raise RuntimeError(f"Apps Script bookmark creation failed: {message}")

    payload = result.get("response", {}).get("result", {})
    bookmark_id = payload.get("bookmarkId")
    if not bookmark_id:
        raise RuntimeError("Apps Script bookmark creation returned no bookmarkId")

    return {
        "script_id": script_id,
        "bookmark_id": bookmark_id,
        "matched_text": payload.get("matchText", anchor_text),
    }


def _build_bookmark_jump_url(doc_id: str, bookmark_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit#bookmark={bookmark_id}"


# ---------------------------------------------------------------------------
# Document structure helpers
# ---------------------------------------------------------------------------

@dataclass
class TextRun:
    text: str
    start: int  # Document index (inclusive)
    end: int    # Document index (exclusive)


@dataclass
class Paragraph:
    text: str
    style: str
    start: int   # Start of paragraph including leading newline-like element
    end: int     # End of paragraph (exclusive)
    runs: list[TextRun]
    # None for body text; an opaque id identifying the table cell otherwise.
    # Ranges may never cross from one container into another.
    container: Optional[str] = None


@dataclass
class InlineStyleSpan:
    start: int
    end: int
    bold: bool = False
    italic: bool = False


@dataclass
class RichParagraph:
    text: str
    style: str = "NORMAL_TEXT"
    bullet_preset: Optional[str] = None
    inline_styles: list[InlineStyleSpan] = field(default_factory=list)


def _paragraph_from_element(elem: dict, container: Optional[str]) -> Paragraph:
    """Build a Paragraph from one structural element containing a paragraph."""
    para = elem["paragraph"]
    style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
    runs = []
    full_text = ""
    for pe in para.get("elements", []):
        if "textRun" in pe:
            content = pe["textRun"]["content"]
            runs.append(TextRun(
                text=content,
                start=pe["startIndex"],
                end=pe["endIndex"],
            ))
            full_text += content
    # Strip trailing newline for display (Google Docs always ends paragraphs with \n)
    return Paragraph(
        text=full_text.rstrip("\n"),
        style=style,
        start=elem["startIndex"],
        end=elem["endIndex"],
        runs=runs,
        container=container,
    )


def _extract_from_content(content: list[dict], container: Optional[str]) -> list[Paragraph]:
    """
    Walk structural elements in document order, descending into table cells.

    Table cell text carries ordinary document indices, so index arithmetic works
    inside a cell exactly as it does in the body. What differs is that a range
    may not cross a cell boundary, which is what `container` is for.
    """
    paragraphs = []
    for elem in content:
        if "paragraph" in elem:
            paragraphs.append(_paragraph_from_element(elem, container))
        elif "table" in elem:
            table_id = elem.get("startIndex")
            for row_i, row in enumerate(elem["table"].get("tableRows", [])):
                for cell_i, cell in enumerate(row.get("tableCells", [])):
                    cell_id = f"{container or ''}/t{table_id}r{row_i}c{cell_i}"
                    paragraphs.extend(_extract_from_content(cell.get("content", []), cell_id))
    return paragraphs


def _extract_paragraphs(doc: dict) -> list[Paragraph]:
    """
    Extract paragraphs with full structure from a Docs API response.

    Includes paragraphs inside tables, in document order.
    """
    return _extract_from_content(doc.get("body", {}).get("content", []), None)


def _build_full_text_map(paragraphs: list[Paragraph]) -> tuple[str, list[tuple[int, int, int, Optional[str]]]]:
    """
    Returns (full_text, text_map) where:
    - full_text is all characters concatenated from all text runs
    - text_map is list of (offset_in_full_text, doc_start_index, length, container)
      allowing mapping from full_text position → document index, and telling
      which table cell (if any) a position sits in
    """
    parts = []
    text_map = []
    offset = 0
    for para in paragraphs:
        for run in para.runs:
            text_map.append((offset, run.start, len(run.text), para.container))
            parts.append(run.text)
            offset += len(run.text)
    return "".join(parts), text_map


def _full_text_pos_to_doc_index(pos: int, text_map: list[tuple[int, int, int, Optional[str]]]) -> int:
    """Map a position in the concatenated full_text to a document character index."""
    for (ft_offset, doc_start, length, _container) in text_map:
        if ft_offset <= pos < ft_offset + length:
            return doc_start + (pos - ft_offset)
    raise ValueError(f"Position {pos} is not within any text run in the document.")


def _container_at(pos: int, text_map: list[tuple[int, int, int, Optional[str]]]) -> Optional[str]:
    """Return the table cell id containing a full_text position, or None for body text."""
    for (ft_offset, _doc_start, length, container) in text_map:
        if ft_offset <= pos < ft_offset + length:
            return container
    raise ValueError(f"Position {pos} is not within any text run in the document.")


def _parse_inline_rich_text(text: str) -> tuple[str, list[InlineStyleSpan]]:
    """
    Parse a very small markdown-like subset into plain text plus style spans.

    Supported forms:
      ***bold italic***
      **bold**
      *italic*

    This intentionally stays conservative and line-local.
    """
    out: list[str] = []
    spans: list[InlineStyleSpan] = []
    i = 0

    def emit_styled(inner: str, *, bold: bool = False, italic: bool = False) -> None:
        start = len("".join(out))
        out.append(inner)
        end = start + len(inner)
        spans.append(InlineStyleSpan(start=start, end=end, bold=bold, italic=italic))

    while i < len(text):
        if text.startswith("***", i):
            j = text.find("***", i + 3)
            if j != -1 and j > i + 3:
                emit_styled(text[i + 3:j], bold=True, italic=True)
                i = j + 3
                continue
        if text.startswith("**", i):
            j = text.find("**", i + 2)
            if j != -1 and j > i + 2:
                emit_styled(text[i + 2:j], bold=True)
                i = j + 2
                continue
        if text.startswith("*", i):
            j = text.find("*", i + 1)
            if j != -1 and j > i + 1:
                emit_styled(text[i + 1:j], italic=True)
                i = j + 1
                continue
        out.append(text[i])
        i += 1

    return "".join(out), spans


def _parse_rich_text(text: str) -> list[RichParagraph]:
    """
    Parse a small markdown-like subset into paragraph/list/style instructions.

    Supported block forms:
      # Heading 1
      ## Heading 2
      ### Heading 3
      - bullet item
      * bullet item
      1. numbered item
      1) numbered item
    """
    lines = text.split("\n")
    paragraphs: list[RichParagraph] = []

    for raw_line in lines:
        line = raw_line
        style = "NORMAL_TEXT"
        bullet_preset: Optional[str] = None

        if line.startswith("### "):
            style = "HEADING_3"
            line = line[4:]
        elif line.startswith("## "):
            style = "HEADING_2"
            line = line[3:]
        elif line.startswith("# "):
            style = "HEADING_1"
            line = line[2:]
        else:
            bullet_match = re.match(r"^\s*[-*]\s+(.+)$", line)
            numbered_match = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
            if bullet_match:
                bullet_preset = "BULLET_DISC_CIRCLE_SQUARE"
                line = bullet_match.group(1)
            elif numbered_match:
                bullet_preset = "NUMBERED_DECIMAL_NESTED"
                line = numbered_match.group(1)

        plain_text, inline_styles = _parse_inline_rich_text(line)
        paragraphs.append(
            RichParagraph(
                text=plain_text,
                style=style,
                bullet_preset=bullet_preset,
                inline_styles=inline_styles,
            )
        )

    return paragraphs


def _build_insert_requests(
    insert_index: int,
    text: str,
    *,
    prefix: str = "",
    suffix: str = "",
    rich: bool = False,
) -> tuple[list[dict], str]:
    """
    Build Docs batchUpdate requests for plain or rich text insertion.
    """
    if not rich:
        inserted_text = f"{prefix}{text}{suffix}"
        return [
            {
                "insertText": {
                    "location": {"index": insert_index},
                    "text": inserted_text,
                }
            }
        ], inserted_text

    paragraphs = _parse_rich_text(text)
    plain_body = "\n".join(p.text for p in paragraphs)
    inserted_text = f"{prefix}{plain_body}{suffix}"
    requests: list[dict] = [
        {
            "insertText": {
                "location": {"index": insert_index},
                "text": inserted_text,
            }
        }
    ]

    cursor = insert_index + len(prefix)
    paragraph_positions: list[tuple[RichParagraph, int, int, int]] = []
    for idx, para in enumerate(paragraphs):
        start = cursor
        end = start + len(para.text)
        paragraph_end = end + 1  # Paragraph terminator newline (inserted or existing body newline)
        paragraph_positions.append((para, start, end, paragraph_end))
        cursor = end
        if idx < len(paragraphs) - 1:
            cursor += 1

    for para, start, end, paragraph_end in paragraph_positions:
        if para.style != "NORMAL_TEXT":
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": start, "endIndex": max(end, start)},
                        "paragraphStyle": {"namedStyleType": para.style},
                        "fields": "namedStyleType",
                    }
                }
            )

        for span in para.inline_styles:
            if span.end <= span.start:
                continue
            text_style = {}
            fields = []
            if span.bold:
                text_style["bold"] = True
                fields.append("bold")
            if span.italic:
                text_style["italic"] = True
                fields.append("italic")
            if text_style:
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": {
                                "startIndex": start + span.start,
                                "endIndex": start + span.end,
                            },
                            "textStyle": text_style,
                            "fields": ",".join(fields),
                        }
                    }
                )

    # Group consecutive list items so numbered lists don't reset on every line.
    i = 0
    while i < len(paragraph_positions):
        para, start, end, paragraph_end = paragraph_positions[i]
        if not para.bullet_preset:
            i += 1
            continue
        bullet_preset = para.bullet_preset
        group_start = start
        group_end = paragraph_end
        j = i + 1
        while j < len(paragraph_positions):
            next_para, next_start, next_end, next_paragraph_end = paragraph_positions[j]
            if next_para.bullet_preset != bullet_preset:
                break
            group_end = next_paragraph_end
            j += 1
        requests.append(
            {
                "createParagraphBullets": {
                    "range": {"startIndex": group_start, "endIndex": group_end},
                    "bulletPreset": bullet_preset,
                }
            }
        )
        i = j

    return requests, inserted_text


def _normalize_anchor_excerpt(anchor_text: str, limit: int = 220) -> str:
    compact = re.sub(r"\s+", " ", anchor_text.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _render_comment_with_anchor_text(comment: str, anchor_text: str) -> str:
    excerpt = _normalize_anchor_excerpt(anchor_text)
    if not excerpt:
        return comment
    if excerpt in comment:
        return comment
    return f'{comment}\n\nAnchored text: “{excerpt}”'


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

class DocumentChangedError(RuntimeError):
    """
    Raised when a document was modified between the read that produced the
    indices and the write that used them. The edit was rejected, not applied.
    """


def _require_revision_id(doc: dict) -> str:
    """
    Pull the revisionId out of a documents().get() response.

    Every index-computing mutator needs this: without it the write cannot
    assert that the document is unchanged, and a concurrent edit would land at
    stale indices and silently corrupt text.
    """
    revision_id = doc.get("revisionId")
    if not revision_id:
        raise ValueError(
            "Document response carried no revisionId, so the edit cannot be "
            "guarded against concurrent changes. Refusing to write."
        )
    return revision_id


def _is_revision_conflict(exc: Exception) -> bool:
    """Recognise the 400 the Docs API returns when requiredRevisionId is stale."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "resp", None), "status", None)
    if status != 400:
        return False
    return "revision" in str(exc).lower()


def _batch_update(service, doc_id: str, requests: list[dict], revision_id: str):
    """
    Apply requests, asserting the document is still at `revision_id`.

    Uses requiredRevisionId (strict reject) rather than targetRevisionId, which
    would attempt to transform the edit onto newer content.
    """
    body = {
        "requests": requests,
        "writeControl": {"requiredRevisionId": revision_id},
    }
    try:
        return service.documents().batchUpdate(
            documentId=doc_id,
            body=body,
        ).execute()
    except Exception as exc:
        if _is_revision_conflict(exc):
            raise DocumentChangedError(
                "The document changed after it was read (revision "
                f"{revision_id} is no longer current), so the edit was NOT "
                "applied. Re-read the document and retry."
            ) from exc
        raise


def _doc_range(ft_start: int, ft_end: int, text_map) -> tuple[int, int]:
    """
    Map a [start, end) full-text span to a [start, end) document range.

    Rejects a span that starts in one table cell and ends in another. The text
    map concatenates cell text with nothing between cells, so a search can
    produce such a span, and editing across a cell boundary is not a legal edit.
    """
    start_container = _container_at(ft_start, text_map)
    end_container = _container_at(ft_end - 1, text_map)
    if start_container != end_container:
        raise ValueError(
            "Match spans a table cell boundary, which cannot be edited as one "
            "range. Narrow the search text so it falls inside a single cell."
        )
    return (
        _full_text_pos_to_doc_index(ft_start, text_map),
        _full_text_pos_to_doc_index(ft_end - 1, text_map) + 1,
    )


def _build_replacement_requests(changes: list[tuple[int, int, str]]) -> list[dict]:
    """
    Build batchUpdate requests for a set of (doc_start, doc_end, replacement).

    Requests are emitted in DESCENDING document order. batchUpdate applies
    requests sequentially, each seeing the effects of the previous ones, so
    rewriting the last range first leaves every earlier offset untouched.
    Within one replacement the insert precedes the delete, and the delete range
    is shifted by the length of the text just inserted.
    """
    requests = []
    for doc_start, doc_end, replace_text in sorted(changes, key=lambda c: c[0], reverse=True):
        if replace_text:
            requests.append({
                "insertText": {
                    "location": {"index": doc_start},
                    "text": replace_text,
                }
            })
            requests.append({
                "deleteContentRange": {
                    "range": {
                        "startIndex": doc_start + len(replace_text),
                        "endIndex": doc_end + len(replace_text),
                    }
                }
            })
        else:
            requests.append({
                "deleteContentRange": {
                    "range": {"startIndex": doc_start, "endIndex": doc_end}
                }
            })
    return requests


def find_matches(
    full_text: str,
    find: str,
    regex: bool = False,
) -> list[tuple[int, int]]:
    """
    Locate every match of `find` in `full_text`.

    Pure function (no network, no Docs API) so that occurrence numbering can be
    tested directly. Returns a list of (start, end) offsets into `full_text`,
    in ascending order, where `end` is exclusive.
    """
    if not find:
        raise ValueError("Search text must not be empty.")

    if regex:
        matches = [(m.start(), m.end()) for m in re.finditer(find, full_text)]
        if any(start == end for start, end in matches):
            raise ValueError(
                f"Pattern {find!r} matches an empty string, which cannot be "
                "mapped to a document range."
            )
        return matches

    matches = []
    search_from = 0
    while True:
        pos = full_text.find(find, search_from)
        if pos == -1:
            break
        matches.append((pos, pos + len(find)))
        # Advance past the whole match: `pos + 1` would report overlapping
        # matches ("aa" in "aaaa" at 0, 1, 2) and corrupt occurrence numbering.
        search_from = pos + len(find)
    return matches


def get(doc_id: str) -> dict:
    """
    Fetch a Google Doc and return structured representation.

    Returns:
        {
          "title": "Document Title",
          "paragraphs": [
            {"text": "...", "style": "HEADING_1", "start": 0, "end": 45}
          ],
          "plain_text": "full document text..."
        }
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    paragraphs = _extract_paragraphs(doc)
    return {
        "title": doc.get("title", ""),
        "paragraphs": [
            {
                "text": p.text,
                "style": p.style,
                "start": p.start,
                "end": p.end,
                "in_table": p.container is not None,
            }
            for p in paragraphs
        ],
        "plain_text": "\n".join(p.text for p in paragraphs),
    }


def search_replace(
    doc_id: str,
    find: str,
    replace: str,
    occurrence: int = 1,
    regex: bool = False,
) -> dict:
    """
    Find text in a document and replace a specific occurrence.

    Args:
        doc_id:     Google Doc ID
        find:       Text to find (or regex pattern if regex=True)
        replace:    Replacement text
        occurrence: Which occurrence to replace (1-based). 0 = replace all.
        regex:      Treat `find` as a regular expression

    Returns:
        {"ok": True, "replaced": "old text", "at_index": 45, "occurrences_found": 3}
    """
    service = _get_service("docs", "v1")

    # Replace-all: use the native replaceAllText API (fast, atomic)
    if occurrence == 0 and not regex:
        result = service.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [{
                    "replaceAllText": {
                        "containsText": {"text": find, "matchCase": True},
                        "replaceText": replace,
                    }
                }]
            },
        ).execute()
        count = (
            result.get("replies", [{}])[0]
            .get("replaceAllText", {})
            .get("occurrencesChanged", 0)
        )
        return {"ok": True, "replaced": find, "occurrences_changed": count}

    # Anything else (a targeted occurrence, or a regex replace-all, which the
    # native replaceAllText API cannot express) needs indices computed here.
    doc = _get_document(service, doc_id)
    revision_id = _require_revision_id(doc)
    paragraphs = _extract_paragraphs(doc)
    full_text, text_map = _build_full_text_map(paragraphs)

    matches = find_matches(full_text, find, regex=regex)

    if not matches:
        raise ValueError(f"Text not found in document: {find!r}")

    if occurrence == 0:
        # Regex replace-all: rewrite every match in one batch.
        changes = [
            _doc_range(ft_start, ft_end, text_map) + (replace,)
            for ft_start, ft_end in matches
        ]
        _batch_update(service, doc_id, _build_replacement_requests(changes), revision_id)
        return {
            "ok": True,
            "replaced": find,
            "occurrences_changed": len(matches),
            "occurrences_found": len(matches),
        }

    if occurrence < 0:
        raise ValueError(
            f"occurrence must be 0 (all) or a positive 1-based index, got {occurrence}"
        )

    target_idx = occurrence - 1
    if target_idx >= len(matches):
        raise ValueError(
            f"Occurrence {occurrence} not found. Document has {len(matches)} occurrence(s) of {find!r}"
        )

    ft_start, ft_end = matches[target_idx]
    doc_start, doc_end = _doc_range(ft_start, ft_end, text_map)
    old_text = full_text[ft_start:ft_end]

    requests = _build_replacement_requests([(doc_start, doc_end, replace)])
    _batch_update(service, doc_id, requests, revision_id)

    return {
        "ok": True,
        "replaced": old_text,
        "at_index": doc_start,
        "occurrences_found": len(matches),
    }


def insert_after(doc_id: str, anchor: str, text: str, rich: bool = True) -> dict:
    """
    Insert text as a new paragraph after the paragraph containing `anchor`.

    The inserted text becomes a separate paragraph (newline appended automatically).
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    revision_id = _require_revision_id(doc)
    paragraphs = _extract_paragraphs(doc)

    target = None
    for p in paragraphs:
        if anchor.lower() in p.text.lower():
            target = p
            break

    if target is None:
        raise ValueError(f"No paragraph containing anchor: {anchor!r}")

    # Insert after the end of the paragraph (doc end index includes the \n)
    insert_index = target.end - 1  # position of the terminating \n

    requests, inserted_text = _build_insert_requests(
        insert_index,
        text,
        prefix="\n",
        rich=rich,
    )
    _batch_update(service, doc_id, requests, revision_id)

    return {
        "ok": True,
        "inserted_after": target.text[:80],
        "at_index": insert_index,
        "rich": rich,
        "inserted_text": inserted_text[:200],
    }


def insert_before(doc_id: str, anchor: str, text: str, rich: bool = True) -> dict:
    """
    Insert text as a new paragraph before the paragraph containing `anchor`.
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    revision_id = _require_revision_id(doc)
    paragraphs = _extract_paragraphs(doc)

    target = None
    for p in paragraphs:
        if anchor.lower() in p.text.lower():
            target = p
            break

    if target is None:
        raise ValueError(f"No paragraph containing anchor: {anchor!r}")

    # Insert at the start of the paragraph
    insert_index = target.start

    requests, inserted_text = _build_insert_requests(
        insert_index,
        text,
        suffix="\n",
        rich=rich,
    )
    _batch_update(service, doc_id, requests, revision_id)

    return {
        "ok": True,
        "inserted_before": target.text[:80],
        "at_index": insert_index,
        "rich": rich,
        "inserted_text": inserted_text[:200],
    }


def delete_paragraph(doc_id: str, anchor: str) -> dict:
    """
    Delete the paragraph(s) containing `anchor` text.

    Deletes ALL paragraphs matching the anchor (case-insensitive substring).
    Returns count of deleted paragraphs.
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    revision_id = _require_revision_id(doc)
    paragraphs = _extract_paragraphs(doc)

    targets = [p for p in paragraphs if anchor.lower() in p.text.lower()]
    if not targets:
        raise ValueError(f"No paragraph containing anchor: {anchor!r}")

    # A table cell must keep at least one paragraph: the Docs API will not let
    # its final paragraph be removed, and sending the request anyway either
    # fails or damages the table.
    per_container = {}
    for p in paragraphs:
        if p.container is not None:
            per_container[p.container] = per_container.get(p.container, 0) + 1
    sole_cell_targets = [
        t for t in targets
        if t.container is not None and per_container[t.container] == 1
    ]
    if sole_cell_targets:
        raise ValueError(
            "Refusing to delete the only paragraph in a table cell, which the "
            "Docs API does not permit: "
            + "; ".join(repr(t.text[:60]) for t in sole_cell_targets)
            + ". Clear the text with search_replace instead."
        )

    # Sort by start index descending so deleting earlier content doesn't shift later indices
    targets.sort(key=lambda p: p.start, reverse=True)

    # The final paragraph of a cell owns the newline that terminates the cell,
    # and that newline cannot be deleted. For those, remove the preceding
    # newline and this paragraph's text instead, which leaves the cell intact.
    last_in_cell = {}
    for p in paragraphs:
        if p.container is None:
            continue
        current = last_in_cell.get(p.container)
        if current is None or p.start > current.start:
            last_in_cell[p.container] = p

    requests = []
    for t in targets:
        start, end = t.start, t.end
        if t.container is not None and last_in_cell[t.container] is t:
            start, end = t.start - 1, t.end - 1
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": start,
                    "endIndex": end,
                }
            }
        })

    _batch_update(service, doc_id, requests, revision_id)

    return {
        "ok": True,
        "deleted_count": len(targets),
        "deleted": [t.text[:80] for t in targets],
    }


def append(doc_id: str, text: str, rich: bool = True) -> dict:
    """
    Append text as a new paragraph at the end of the document.
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    revision_id = _require_revision_id(doc)

    # Find the last content index (end of the body, before the body's closing)
    body_content = doc.get("body", {}).get("content", [])
    if not body_content:
        raise ValueError("Document body is empty.")

    # The last element's endIndex is the body end (exclusive).
    # We insert just before that.
    last_elem = body_content[-1]
    insert_index = last_elem["endIndex"] - 1

    requests, inserted_text = _build_insert_requests(
        insert_index,
        text,
        prefix="\n",
        rich=rich,
    )
    _batch_update(service, doc_id, requests, revision_id)

    return {
        "ok": True,
        "appended": text[:80],
        "at_index": insert_index,
        "rich": rich,
        "inserted_text": inserted_text[:200],
    }


def batch_replace(doc_id: str, replacements: list[dict]) -> dict:
    """
    Apply multiple find→replace operations atomically (all or nothing).

    Replacements are sorted by position (end→start) automatically, so earlier
    indices remain valid throughout the batch.

    Args:
        doc_id:       Google Doc ID
        replacements: List of {"find": "...", "replace": "...", "occurrence": 1}
                      `occurrence` defaults to 1. Use 0 for replace-all.

    Returns:
        {"ok": True, "applied": N, "changes": [...]}
    """
    service = _get_service("docs", "v1")
    doc = _get_document(service, doc_id)
    revision_id = _require_revision_id(doc)
    paragraphs = _extract_paragraphs(doc)
    full_text, text_map = _build_full_text_map(paragraphs)

    # Collect all changes with their document indices
    changes = []  # (doc_start, doc_end, replace_text, find_text)
    for rep in replacements:
        find = rep["find"]
        replace_text = rep.get("replace", "")
        occurrence = rep.get("occurrence", 1)
        is_regex = rep.get("regex", False)

        # Build list of (ft_start, ft_end) tuples
        matches = find_matches(full_text, find, regex=is_regex)

        if not matches:
            raise ValueError(f"Text not found: {find!r}")

        if occurrence == 0:
            # Replace all
            for ft_start, ft_end in matches:
                ds, de = _doc_range(ft_start, ft_end, text_map)
                changes.append((ds, de, replace_text, full_text[ft_start:ft_end]))
        else:
            if occurrence < 0:
                raise ValueError(
                    f"occurrence must be 0 (all) or a positive 1-based index, "
                    f"got {occurrence} for {find!r}"
                )
            idx = occurrence - 1
            if idx >= len(matches):
                raise ValueError(f"Occurrence {occurrence} not found for {find!r}")
            ft_start, ft_end = matches[idx]
            ds, de = _doc_range(ft_start, ft_end, text_map)
            changes.append((ds, de, replace_text, full_text[ft_start:ft_end]))

    requests = _build_replacement_requests(
        [(ds, de, replace_text) for (ds, de, replace_text, _old) in changes]
    )
    _batch_update(service, doc_id, requests, revision_id)

    return {
        "ok": True,
        "applied": len(changes),
        "changes": [
            {"replaced": old, "with": new, "at_index": ds}
            for (ds, de, new, old) in sorted(changes, key=lambda c: c[0])
        ],
    }


def add_comment(
    doc_id: str,
    comment: str,
    anchor_text: str,
    occurrence: int = 1,
    include_anchor_text: bool = True,
    bookmark_jump: bool = False,
    apps_script_id: str | None = None,
) -> dict:
    """
    Add a comment anchored to specific text in a Google Doc.

    Current implementation detail: this creates a Docs named range and then
    posts a Drive comment anchored to that named range.

    Important limitation: despite the named range, Google Docs still renders
    these API-created comments as "Original content deleted" in the UI rather
    than as a proper inline highlight. This function is kept for API-level
    comment access, but a real Apps Script automation path is being investigated
    for true inline Docs comments.

    Steps:
      1. Find anchor_text in the document → get character indices
      2. CreateNamedRangeRequest via batchUpdate → get named_range_id
      3. Drive API comments.create with anchor JSON → attached comment

    Args:
        doc_id:      Google Doc ID
        comment:     Comment text to post
        anchor_text: Text in the document to attach the comment to
        occurrence:  Which occurrence to anchor to (default 1 = first)

    Returns:
        {"ok": True, "comment_id": "...", "anchored_to": "...", "at_index": N,
         "named_range_id": "..."}
    """
    import uuid

    docs_service = _get_service("docs", "v1")
    doc = _get_document(docs_service, doc_id)
    paragraphs = _extract_paragraphs(doc)
    full_text, text_map = _build_full_text_map(paragraphs)

    # Find all occurrences of anchor_text
    matches = []
    search_from = 0
    while True:
        pos = full_text.find(anchor_text, search_from)
        if pos == -1:
            break
        matches.append((pos, pos + len(anchor_text)))
        search_from = pos + 1

    if not matches:
        raise ValueError(f"Anchor text not found in document: {anchor_text!r}")

    target_idx = occurrence - 1
    if target_idx >= len(matches):
        raise ValueError(
            f"Occurrence {occurrence} not found — document has {len(matches)} "
            f"occurrence(s) of {anchor_text!r}"
        )

    ft_start, ft_end = matches[target_idx]
    doc_start = _full_text_pos_to_doc_index(ft_start, text_map)
    doc_end = _full_text_pos_to_doc_index(ft_end - 1, text_map) + 1

    # Create a named range at the anchor position
    range_name = f"comment-anchor-{uuid.uuid4().hex[:12]}"
    result = docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [{
                "createNamedRange": {
                    "name": range_name,
                    "range": {
                        "startIndex": doc_start,
                        "endIndex": doc_end,
                        "segmentId": "",  # empty = main document body
                    },
                }
            }]
        },
    ).execute()

    named_range_id = (
        result.get("replies", [{}])[0]
        .get("createNamedRange", {})
        .get("namedRangeId")
    )
    if not named_range_id:
        raise RuntimeError("Failed to create named range — no ID returned by Docs API")

    # The correct anchor format for Google Docs is the bare named range ID
    # (e.g. "kix.abc123") as a plain string — NOT a JSON object.
    # quotedFileContent provides the fallback display text.
    from googleapiclient.discovery import build
    drive = build("drive", "v3", credentials=_load_creds())
    bookmark = None
    if bookmark_jump:
        bookmark = _create_bookmark_via_apps_script(
            doc_id,
            full_text[ft_start:ft_end],
            occurrence=occurrence,
            script_id=apps_script_id,
        )

    rendered_comment = (
        _render_comment_with_anchor_text(comment, full_text[ft_start:ft_end])
        if include_anchor_text
        else comment
    )
    if bookmark is not None:
        rendered_comment = (
            f"{rendered_comment}\n\nJump: {_build_bookmark_jump_url(doc_id, bookmark['bookmark_id'])}"
        )
    comment_result = drive.comments().create(
        fileId=doc_id,
        body={
            "content": rendered_comment,
            "anchor": named_range_id,
            "quotedFileContent": {
                "mimeType": "text/html",
                "value": full_text[ft_start:ft_end],
            },
        },
        fields="id,content,anchor",
    ).execute()

    return {
        "ok": True,
        "comment_id": comment_result.get("id"),
        "anchored_to": full_text[ft_start:ft_end],
        "at_index": doc_start,
        "named_range_id": named_range_id,
        "comment_content": rendered_comment,
        "bookmark_id": bookmark["bookmark_id"] if bookmark is not None else None,
        "bookmark_url": (
            _build_bookmark_jump_url(doc_id, bookmark["bookmark_id"])
            if bookmark is not None
            else None
        ),
        "apps_script_id": bookmark["script_id"] if bookmark is not None else None,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Surgical Google Docs editor — LLMs never touch indices.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="Output JSON (default for scripting)")

    sub = p.add_subparsers(dest="command", required=True)

    # get
    g = sub.add_parser("get", help="Get document structure")
    g.add_argument("doc_id")

    # search_replace
    sr = sub.add_parser("search_replace", help="Search and replace text")
    sr.add_argument("doc_id")
    sr.add_argument("--find", required=True)
    sr.add_argument("--replace", required=True)
    sr.add_argument("--occurrence", type=int, default=1, help="Which occurrence (1=first, 0=all)")
    sr.add_argument("--regex", action="store_true")

    # insert_after
    ia = sub.add_parser("insert_after", help="Insert paragraph after anchor")
    ia.add_argument("doc_id")
    ia.add_argument("--anchor", required=True)
    ia.add_argument("--text", required=True)
    ia.set_defaults(rich=True)
    ia.add_argument("--plain", dest="rich", action="store_false", help="Insert literal text without rich formatting")

    # insert_before
    ib = sub.add_parser("insert_before", help="Insert paragraph before anchor")
    ib.add_argument("doc_id")
    ib.add_argument("--anchor", required=True)
    ib.add_argument("--text", required=True)
    ib.set_defaults(rich=True)
    ib.add_argument("--plain", dest="rich", action="store_false", help="Insert literal text without rich formatting")

    # delete_paragraph
    dp = sub.add_parser("delete_paragraph", help="Delete paragraph(s) matching anchor")
    dp.add_argument("doc_id")
    dp.add_argument("--anchor", required=True)

    # append
    ap = sub.add_parser("append", help="Append text to end of document")
    ap.add_argument("doc_id")
    ap.add_argument("--text", required=True)
    ap.set_defaults(rich=True)
    ap.add_argument("--plain", dest="rich", action="store_false", help="Insert literal text without rich formatting")

    # batch_replace
    br = sub.add_parser("batch_replace", help="Multiple replacements (atomic)")
    br.add_argument("doc_id")
    br.add_argument(
        "--replacements",
        required=True,
        help='JSON array: [{"find":"...","replace":"...","occurrence":1}]',
    )

    # add_comment
    ac = sub.add_parser("add_comment", help="Add a comment anchored to specific text")
    ac.add_argument("doc_id")
    ac.add_argument("--anchor", required=True, help="Text in the document to anchor the comment to")
    ac.add_argument("--comment", required=True, help="Comment text to post")
    ac.add_argument("--occurrence", type=int, default=1, help="Which occurrence to anchor to (default 1)")
    ac.add_argument(
        "--no-include-anchor-text",
        action="store_true",
        help="Do not append the anchored text excerpt into the comment body",
    )
    ac.add_argument(
        "--bookmark-jump",
        action="store_true",
        help="Create an Apps Script bookmark and append a #bookmark jump URL into the comment body",
    )
    ac.add_argument(
        "--apps-script-id",
        help=f"Override {APPS_SCRIPT_ID_ENV_VAR} for bookmark-jump mode",
    )

    return p


def main():
    logging.basicConfig(level=logging.WARNING)
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.command == "get":
            result = get(args.doc_id)
        elif args.command == "search_replace":
            result = search_replace(
                args.doc_id, args.find, args.replace, args.occurrence, args.regex
            )
        elif args.command == "insert_after":
            result = insert_after(args.doc_id, args.anchor, args.text, rich=args.rich)
        elif args.command == "insert_before":
            result = insert_before(args.doc_id, args.anchor, args.text, rich=args.rich)
        elif args.command == "delete_paragraph":
            result = delete_paragraph(args.doc_id, args.anchor)
        elif args.command == "append":
            result = append(args.doc_id, args.text, rich=args.rich)
        elif args.command == "batch_replace":
            replacements = json.loads(args.replacements)
            result = batch_replace(args.doc_id, replacements)
        elif args.command == "add_comment":
            result = add_comment(
                args.doc_id,
                args.comment,
                args.anchor,
                args.occurrence,
                include_anchor_text=not args.no_include_anchor_text,
                bookmark_jump=args.bookmark_jump,
                apps_script_id=args.apps_script_id,
            )
        else:
            parser.print_help()
            sys.exit(1)

        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
