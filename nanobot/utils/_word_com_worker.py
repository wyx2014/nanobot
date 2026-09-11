"""Isolated Microsoft Word COM conversion worker for legacy DOC files."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import NoReturn


class WordAutomationError(RuntimeError):
    def __init__(self, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


def convert_doc_to_docx(source: Path, destination: Path) -> None:
    """Convert one DOC using a new hidden Word instance with macros disabled."""
    try:
        import pythoncom
        from win32com.client import DispatchEx
    except ImportError as exc:
        raise WordAutomationError(
            "PYWIN32_UNAVAILABLE",
            "Windows Word integration is unavailable because pywin32 is not installed",
            str(exc),
        ) from exc

    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source.suffix.lower() != ".doc":
        raise WordAutomationError("INVALID_SOURCE", "the source file must use the .doc extension")
    if destination.suffix.lower() != ".docx":
        raise WordAutomationError(
            "INVALID_DESTINATION", "the conversion destination must use the .docx extension"
        )

    application = None
    document = None
    pythoncom.CoInitialize()
    try:
        try:
            application = DispatchEx("Word.Application")
        except Exception as exc:
            raise WordAutomationError(
                "WORD_UNAVAILABLE",
                "Microsoft Word desktop is not installed, activated, or registered for COM",
                str(exc),
            ) from exc

        application.Visible = False
        application.DisplayAlerts = 0
        try:
            # msoAutomationSecurityForceDisable. Abort if Word cannot enforce it.
            application.AutomationSecurity = 3
        except Exception as exc:
            raise WordAutomationError(
                "WORD_SECURITY_SETUP_FAILED",
                "Microsoft Word could not disable document macros",
                str(exc),
            ) from exc

        for option, value in (
            ("SaveNormalPrompt", True),
            ("ConfirmConversions", False),
            ("UpdateLinksAtOpen", False),
        ):
            try:
                setattr(application.Options, option, value)
            except Exception:
                pass

        try:
            document = application.Documents.Open(
                FileName=str(source),
                ConfirmConversions=False,
                ReadOnly=True,
                AddToRecentFiles=False,
                PasswordDocument="",
                PasswordTemplate="",
                Revert=False,
                WritePasswordDocument="",
                WritePasswordTemplate="",
                Visible=False,
                OpenAndRepair=True,
                NoEncodingDialog=True,
            )
        except Exception as exc:
            raise WordAutomationError(
                "WORD_OPEN_FAILED",
                "Microsoft Word could not open the .doc file; it may be protected, damaged, or blocked",
                str(exc),
            ) from exc

        try:
            # wdFormatDocumentDefault (DOCX).
            document.SaveAs2(str(destination), 16)
        except Exception as exc:
            raise WordAutomationError(
                "WORD_CONVERSION_FAILED",
                "Microsoft Word could not convert the .doc file to .docx",
                str(exc),
            ) from exc
    finally:
        if document is not None:
            try:
                document.Close(0)
            except Exception:
                pass
        if application is not None:
            try:
                application.NormalTemplate.Saved = True
            except Exception:
                pass
            try:
                application.Quit(0)
            except Exception:
                pass
        pythoncom.CoUninitialize()

    if not destination.is_file() or destination.stat().st_size == 0:
        raise WordAutomationError(
            "WORD_OUTPUT_MISSING", "Microsoft Word produced no converted .docx file"
        )


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def _fail(code: str, message: str, detail: str = "") -> NoReturn:
    _emit({"ok": False, "code": code, "message": message, "detail": detail})
    raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        _fail("INVALID_ARGUMENTS", "expected source .doc and destination .docx paths")

    try:
        convert_doc_to_docx(Path(arguments[0]), Path(arguments[1]))
    except WordAutomationError as exc:
        _fail(exc.code, str(exc), exc.detail)
    except FileNotFoundError as exc:
        _fail("SOURCE_NOT_FOUND", "the source .doc file was not found", str(exc))
    except Exception as exc:
        _fail("WORD_CONVERSION_FAILED", "unexpected Microsoft Word conversion failure", str(exc))

    _emit({"ok": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
