"""Presentation catalog and document bindings owned by the gateway."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from PIL import Image

from nanobot.agent.skills import BUILTIN_SKILLS_DIR

_LOCK = threading.RLock()
_ID = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")
KIMI_ASSETS = Path(__file__).parent / "presentation_assets/kimi"
_CATALOG = (
    ("taiping-standard", "taiping", "中国太平标准", "China Taiping", "work", "pptx", ""),
    ("guizang-editorial", "guizang", "电子杂志", "Editorial", "talk", "html", "template.html"),
    ("guizang-swiss", "guizang", "瑞士极简", "Swiss", "talk", "html", "template-swiss.html"),
    ("kimi-consulting", "kimi", "海蓝研究", "Marine Research", "research", "pptx", "consulting/marine-blue-research"),
    ("kimi-finance", "kimi", "湖蓝备忘录", "Lake Blue Memo", "finance", "pptx", "finance/lake-blue-memo"),
    ("kimi-work", "kimi", "蓝焰品牌", "Blue Flame Brand", "work", "pptx", "work/blue-flame-brand"),
    ("kimi-product", "kimi", "银灰杂志", "Silver Gray Magazine", "product", "pptx", "promotion/silver-gray-luxury-magazine"),
)


class PresentationError(ValueError):
    pass


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def template_by_id(template_id: str) -> dict[str, Any]:
    for row in _CATALOG:
        if row[0] == template_id:
            return dict(zip(("id", "family", "name", "name_en", "category", "format", "design"), row))
    raise PresentationError("Unknown presentation template")


def source_digest(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise PresentationError("Presentation sources must not contain symlinks")
        if path.is_file():
            digest.update(path.relative_to(folder).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def runtime_executable(name: str) -> str | None:
    directory = Path.home() / ".nanobot/tools/presentations/node_modules/.bin"
    return shutil.which(name, path=str(directory)) or shutil.which(name)


def presentation_selection(document: dict[str, Any]) -> dict[str, Any]:
    return {key: document[key] for key in (
        "document_id", "template_id", "name", "sample_first", "page"
    ) if document.get(key) is not None}


@lru_cache(maxsize=32)
def _preview_source_digest(path: str, modified: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@lru_cache(maxsize=32)
def _cached_previews(files: tuple[tuple[str, int, int], ...], kind: str) -> tuple[str, ...]:
    """Cache prepared pixels by source identity; availability is still checked on every request."""
    prefix = "data:image/jpeg;base64,"
    if kind == "prepared":
        return tuple(prefix + base64.b64encode(Path(name).read_bytes()).decode() for name, _, _ in files)
    images = []
    path = Path(files[0][0])
    if kind == "taiping":
        with ZipFile(path) as archive:
            for name in ("image2.jpeg", "image1.jpeg", "image3.jpeg"):
                with Image.open(BytesIO(archive.read(f"ppt/media/{name}"))) as image:
                    images.append(image.convert("RGB"))
    else:
        with Image.open(path) as original:
            image = original.convert("RGB")
        if kind == "strip":
            height = round(image.width * 9 / 16)
            for index in range(min(3, (image.height + height - 1) // height)):
                top = index * height
                images.append(image.crop((0, top, image.width, min(image.height, top + height))))
            image.close()
        else:
            images.append(image)
    result = []
    for image in images:
        image.thumbnail((720, 405))
        stream = BytesIO()
        image.save(stream, "JPEG", quality=82)
        image.close()
        result.append(prefix + base64.b64encode(stream.getvalue()).decode())
    return tuple(result)


class PresentationService:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.root = self.workspace / ".nanobot" / "presentations"

    def source(self, family: str) -> Path | None:
        if family == "taiping":
            return BUILTIN_SKILLS_DIR / "corporate-ppt"
        if family not in {"guizang", "kimi"}:
            raise PresentationError("Unknown template family")
        try:
            configured = json.loads((self.root / "sources.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            configured = {}
        candidates = []
        if isinstance(configured, dict) and isinstance(configured.get(family), str):
            candidates.append(Path(configured[family]).expanduser())
        env = os.environ.get(f"NANOBOT_PRESENTATION_{family.upper()}_DIR")
        if env:
            candidates.append(Path(env).expanduser())
        if family == "kimi":
            candidates.append(KIMI_ASSETS)
        name = "guizang-ppt-skill" if family == "guizang" else "open-kimi-ppt"
        candidates.extend((self.workspace / "skills" / name, BUILTIN_SKILLS_DIR / name))
        repository = Path(__file__).resolve().parent.parent
        if (repository / ".git").exists():
            candidates.append(repository.parent / (name if family == "guizang" else "open-kimi-ppt-skill"))
        for candidate in candidates:
            root = candidate.resolve()
            if family == "kimi" and root == KIMI_ASSETS.resolve():
                return root
            if family == "kimi" and (root / "skills/open-kimi-ppt/SKILL.md").is_file():
                return root / "skills/open-kimi-ppt"
            if (root / "SKILL.md").is_file():
                return root
        return None

    def availability(self, template: dict[str, Any], source: Path | None = None) -> list[str]:
        source = source or self.source(template["family"])
        if source is None:
            return ["source_missing"]
        if template["family"] == "guizang":
            missing = [] if (source / "assets" / template["design"]).is_file() else ["template_missing"]
            if template["id"] == "guizang-swiss" and not runtime_executable("node"):
                missing.append("node")
            return missing
        if template["family"] == "kimi":
            return [] if (source / "reference/design_system" / template["design"] / "design.md").is_file() else ["template_missing"]
        return [] if (source / "assets/ppt-template.pptx").is_file() else ["template_missing"]

    def catalog(self) -> dict[str, Any]:
        templates = []
        for row in _CATALOG:
            template = template_by_id(row[0])
            missing = self.availability(template)
            templates.append({**template, "version": 1, "available": not missing,
                              "missing": missing, "requires_network": False,
                              "previews": self.previews(template)[:1]})
        from nanobot.presentation_previews import previews_pending
        return {"templates": templates, "previews_pending": previews_pending(self.root / "previews")}

    def previews(self, template: dict[str, Any]) -> list[str]:
        source = self.source(template["family"])
        if source is None:
            return []
        try:
            if template["family"] == "taiping":
                paths, kind = [source / "assets/ppt-template.pptx"], "taiping"
                info = paths[0].stat()
                digest = _preview_source_digest(str(paths[0]), info.st_mtime_ns, info.st_size)
                prepared = Path(__file__).parent / "presentation_assets/taiping"
                thumbnails = [prepared / f"{digest}-{index}.jpg" for index in range(1, 4)]
                if all(path.is_file() for path in thumbnails):
                    paths, kind = thumbnails, "prepared"
            elif template["family"] == "guizang":
                from nanobot.presentation_previews import guizang_preview
                preview = guizang_preview(source, template["design"], self.root / "previews")
                if not preview:
                    return []
                paths, kind = [preview], "image"
            else:
                prepared = source / "assets/previews" / template["design"]
                paths = [prepared / f"{index}.jpg" for index in range(1, 4)]
                if all(path.is_file() for path in paths):
                    stamps = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
                    return list(_cached_previews(stamps, "prepared"))
                preview = source / "assets/themes" / f"{template['design']}.jpg"
                if not preview.is_file():
                    preview = source.parent.parent / "docs/themes" / f"{template['design']}.jpg"
                if not preview.is_file():
                    preview = KIMI_ASSETS / "assets/themes" / f"{template['design']}.jpg"
                paths, kind = [preview], "strip"
            stamps = tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
            return list(_cached_previews(stamps, kind))
        except (OSError, KeyError, ValueError):
            return []

    def document(self, document_id: str, session_key: str | None = None) -> dict[str, Any]:
        if not isinstance(document_id, str) or not _ID.fullmatch(document_id):
            raise PresentationError("Invalid presentation document ID")
        try:
            document = json.loads((self.root / "documents" / f"{document_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PresentationError("Presentation document not found") from exc
        if not isinstance(document, dict) or (session_key and document.get("session_key") != session_key):
            raise PresentationError("Presentation document does not belong to this conversation")
        folder = Path(document.get("project_path", ""))
        root = Path(document.get("project_root", ""))
        if (not folder.is_absolute() or not root.is_absolute()
                or folder != root / "presentations" / document_id
                or folder.resolve() != folder):
            raise PresentationError("Presentation project path has changed")
        return document

    def list_documents(self, session_key: str) -> list[dict[str, Any]]:
        documents = []
        for path in (self.root / "documents").glob("*.json"):
            try:
                document = self.document(path.stem, session_key)
                documents.append(document)
            except PresentationError:
                continue
        return sorted(documents, key=lambda item: item.get("updated_at", 0), reverse=True)

    def bind(self, raw: Any, *, session_key: str, project_root: Path, title: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise PresentationError("Invalid presentation selection")
        template = template_by_id(str(raw.get("template_id") or ""))
        document_id = raw.get("document_id")
        if not isinstance(document_id, str) or not _ID.fullmatch(document_id):
            raise PresentationError("Invalid presentation document ID")
        sample_first = raw.get("sample_first", True) is not False
        page = raw.get("page")
        if page is not None and (type(page) is not int or not 1 <= page <= 100):
            raise PresentationError("Invalid presentation page")
        with _LOCK:
            record = self.root / "documents" / f"{document_id}.json"
            if record.exists():
                document = self.document(document_id, session_key)
                if document["template_id"] != template["id"]:
                    raise PresentationError("Changing templates requires a new document")
                if page and page > document.get("page_count", 100):
                    raise PresentationError("Presentation page does not exist")
                return {**document, "sample_first": sample_first, "page": page}
            if page is not None:
                raise PresentationError("Select an existing document to revise a page")
            missing = self.availability(template)
            if missing:
                raise PresentationError("Presentation unavailable: " + ", ".join(missing))
            source = self.source(template["family"])
            if source is None:
                raise PresentationError("Presentation source missing")
            root = Path(project_root).expanduser().resolve()
            folder = root / "presentations" / document_id
            if not folder.resolve().is_relative_to(root) or folder.resolve() != folder:
                raise PresentationError("Presentation path is outside the project")
            folder.mkdir(parents=True, exist_ok=False)
            try:
                (folder / "media").mkdir()
                # Snapshot guides and runtime assets so source updates cannot change this deck.
                names = ("reference",) if template["family"] == "kimi" else ("references", "reference", "assets", "scripts")
                for name in names:
                    target = source / name
                    if target.is_symlink():
                        raise PresentationError("Presentation sources must not contain symlinks")
                    if target.is_dir():
                        if any(item.is_symlink() for item in target.rglob("*")):
                            raise PresentationError("Presentation sources must not contain symlinks")
                        shutil.copytree(target, folder / ".source" / name,
                                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                (folder / ".source").mkdir(exist_ok=True)
                if (source / "SKILL.md").is_symlink():
                    raise PresentationError("Presentation sources must not contain symlinks")
                if template["family"] == "kimi":
                    shutil.copy2(KIMI_ASSETS / "LOCAL_EXPORT.md", folder / ".source/LOCAL_EXPORT.md")
                else:
                    shutil.copy2(source / "SKILL.md", folder / ".source/SKILL.md")
                license_path = source / "LICENSE"
                if not license_path.exists() and template["family"] == "kimi":
                    license_path = source.parent.parent / "LICENSE"
                if license_path.exists():
                    shutil.copy2(license_path, folder / ".source/LICENSE")
                if template["family"] == "guizang":
                    shutil.copy2(source / "assets" / template["design"], folder / "index.html")
                    shutil.copytree(source / "assets", folder / "assets")
                    (folder / "images").mkdir()
                elif template["family"] == "kimi":
                    (folder / "pages").mkdir()
                digest = source_digest(folder / ".source")
                document = {
                    "document_id": document_id, "template_id": template["id"], "template_version": 1,
                    "name": template["name"], "family": template["family"], "format": template["format"],
                    "title": title.strip()[:100] or template["name"], "session_key": session_key,
                    "project_path": str(folder), "project_root": str(root),
                    "source_digest": digest, "status": "draft",
                    "updated_at": time.time(), "sample_first": sample_first, "artifacts": [],
                }
                _write_json(folder / "presentation.json", document)
                _write_json(record, document)
            except BaseException:
                shutil.rmtree(folder)
                raise
            return {**document, "page": page}

    def complete(self, document_id: str, *, artifacts: list[dict[str, Any]], page_count: int) -> None:
        with _LOCK:
            document = self.document(document_id)
            document.update(status="ready", artifacts=artifacts, page_count=page_count, updated_at=time.time())
            _write_json(Path(document["project_path"]) / "presentation.json", document)
            _write_json(self.root / "documents" / f"{document_id}.json", document)


def presentation_runtime_lines(metadata: Any) -> list[str]:
    document = metadata.get("presentation") if isinstance(metadata, dict) else None
    if not isinstance(document, dict) or not document.get("project_path"):
        return []
    template = template_by_id(document["template_id"])
    folder = Path(document["project_path"])
    guide = folder / ".source/SKILL.md"
    if template["family"] == "kimi":
        design = folder / ".source/reference/design_system" / template["design"] / "design.md"
        guide = folder / ".source/LOCAL_EXPORT.md"
        if not guide.is_file():
            guide = KIMI_ASSETS / "LOCAL_EXPORT.md"
        details = f"Read {guide} and {design}. Write deck.pptd and pages/*.page using the local PPTD profile. All export is local using native editable PowerPoint elements. The local profile takes precedence over upstream skill/export instructions. No Kimi website, browser automation or remote fonts. Use project-local media."
    elif template["family"] == "guizang":
        suffix = "-swiss" if template["id"] == "guizang-swiss" else ""
        details = f"Read {guide} and {folder / ('.source/references/layouts' + suffix + '.md')}. Fill index.html using the selected template, preserve its presentation runtime. Use local assets/ paths. Replace the SLIDES_HERE comment with actual slides and set the document title."
        if suffix:
            details += f" Read {folder / '.source/references/swiss-layout-lock.md'} and set data-layout on every slide."
    else:
        details = f"Read {guide} and {folder / '.source/references/deck-format.md'}. Write deck.yaml with editable corporate content."
    return [
        "Presentation selection is explicitly bound by the user and takes precedence over generic PPT skill discovery.",
        f"Document ID: {document['document_id']}; template: {template['id']}; output: {template['format']}; project: {folder}.",
        details,
        "Keep all sources and media inside this document project. Never replace the selected template with another family.",
        "The .source snapshot and presentation.json are gateway-owned. Read them but never edit them.",
        "Do not install tools, update skills, use a different exporter, or upload source material to another service. The gateway owns dependency checks and export.",
        "First outline the conclusions and evidence. " + (
            "Create only three representative sample pages first; export them and wait for the user's design feedback before expanding."
            if document.get("sample_first") and document.get("status") == "draft" else
            "Create or revise the requested document, keeping unaffected pages unchanged."
        ),
        f"The user selected page {document['page']} for revision. Keep all other pages unchanged." if document.get("page") else "",
        f"After writing sources call export_presentation with document_id={document['document_id']}. It selects the pinned exporter and records artifacts. Never claim success before this tool succeeds.",
    ]
