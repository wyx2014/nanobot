"""Tests for the isolated Microsoft Word COM conversion worker."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from nanobot.utils._word_com_worker import WordAutomationError, convert_doc_to_docx


def _install_fake_pywin32(monkeypatch, dispatch):
    pythoncom = ModuleType("pythoncom")
    pythoncom.CoInitialize = Mock()
    pythoncom.CoUninitialize = Mock()

    client = ModuleType("win32com.client")
    client.DispatchEx = dispatch
    win32com = ModuleType("win32com")
    win32com.__path__ = []
    win32com.client = client

    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    return pythoncom


def test_convert_doc_to_docx_uses_hidden_read_only_word_instance(tmp_path: Path, monkeypatch):
    source = tmp_path / "中文需求.doc"
    destination = tmp_path / "converted.docx"
    source.write_bytes(b"legacy-doc")

    document = Mock()
    document.SaveAs2.side_effect = lambda path, _format: Path(path).write_bytes(b"PK-docx")
    application = Mock()
    application.Options = Mock()
    application.Documents.Open.return_value = document
    dispatch = Mock(return_value=application)
    pythoncom = _install_fake_pywin32(monkeypatch, dispatch)

    convert_doc_to_docx(source, destination)

    dispatch.assert_called_once_with("Word.Application")
    assert application.Visible is False
    assert application.DisplayAlerts == 0
    assert application.AutomationSecurity == 3
    assert application.Options.SaveNormalPrompt is True
    assert application.Options.UpdateLinksAtOpen is False
    open_options = application.Documents.Open.call_args.kwargs
    assert open_options["FileName"] == str(source.resolve())
    assert open_options["ReadOnly"] is True
    assert open_options["AddToRecentFiles"] is False
    document.SaveAs2.assert_called_once_with(str(destination.resolve()), 16)
    document.Close.assert_called_once_with(0)
    assert application.NormalTemplate.Saved is True
    application.Quit.assert_called_once_with(0)
    pythoncom.CoInitialize.assert_called_once_with()
    pythoncom.CoUninitialize.assert_called_once_with()
    assert destination.read_bytes() == b"PK-docx"


def test_convert_doc_to_docx_reports_missing_word_and_uninitializes_com(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "legacy.doc"
    destination = tmp_path / "converted.docx"
    source.write_bytes(b"legacy-doc")
    dispatch = Mock(side_effect=RuntimeError("class not registered"))
    pythoncom = _install_fake_pywin32(monkeypatch, dispatch)

    with pytest.raises(WordAutomationError) as raised:
        convert_doc_to_docx(source, destination)

    assert raised.value.code == "WORD_UNAVAILABLE"
    assert "not installed" in str(raised.value)
    pythoncom.CoUninitialize.assert_called_once_with()
