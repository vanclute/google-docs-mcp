"""
Tests for auth_setup.py output handling.

The success path prints non-ASCII characters. On a Windows console using a
legacy code page that raised UnicodeEncodeError *after* the token was saved,
so a working authorisation reported itself as a crash.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import auth_setup


class _LegacyStream(io.StringIO):
    """A stream whose reconfigure fails, as a detached or redirected one can."""

    def __init__(self, exc):
        super().__init__()
        self._exc = exc
        self.reconfigured_with = None

    def reconfigure(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        self.reconfigured_with = kwargs


def test_forces_utf8_on_both_streams(monkeypatch):
    out, err = _LegacyStream(None), _LegacyStream(None)
    monkeypatch.setattr(auth_setup.sys, "stdout", out)
    monkeypatch.setattr(auth_setup.sys, "stderr", err)

    auth_setup._force_utf8_output()

    for stream in (out, err):
        assert stream.reconfigured_with == {"encoding": "utf-8", "errors": "replace"}


def test_survives_streams_that_refuse_to_reconfigure(monkeypatch):
    monkeypatch.setattr(auth_setup.sys, "stdout", _LegacyStream(ValueError("detached")))
    monkeypatch.setattr(auth_setup.sys, "stderr", _LegacyStream(OSError("redirected")))

    auth_setup._force_utf8_output()  # must not raise


def test_survives_streams_without_reconfigure(monkeypatch):
    monkeypatch.setattr(auth_setup.sys, "stdout", io.StringIO())
    monkeypatch.setattr(auth_setup.sys, "stderr", io.StringIO())

    auth_setup._force_utf8_output()  # must not raise


def _run_under_cp1252(code: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(auth_setup.__file__).parent),
    )


def test_printing_the_success_mark_fails_without_the_fix():
    # Positive control: without the module's reconfigure, this is the crash.
    result = _run_under_cp1252("print('\\u2713 Authenticated')")

    assert result.returncode != 0
    assert "UnicodeEncodeError" in result.stderr


def test_printing_the_success_mark_works_after_importing_auth_setup():
    result = _run_under_cp1252(
        "import auth_setup; print('\\u2713 Authenticated')"
    )

    assert result.returncode == 0, result.stderr
