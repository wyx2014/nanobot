from pathlib import Path

from PIL import Image

from nanobot.presentations import PresentationService, _cached_previews, template_by_id


def test_builtin_thumbnails_do_not_decode_large_source_images(tmp_path, monkeypatch):
    _cached_previews.cache_clear()
    service = PresentationService(tmp_path)

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("Bundled previews must use prepared thumbnails")

    monkeypatch.setattr(Image, "open", unexpected_decode)
    for template_id in ("taiping-standard", "kimi-consulting", "kimi-finance", "kimi-work", "kimi-product"):
        images = service.previews(template_by_id(template_id))
        assert len(images) == 3
        assert all(image.startswith("data:image/jpeg;base64,") for image in images)


def test_preview_cache_reuses_pixels_and_invalidates_changed_sources(tmp_path, monkeypatch):
    _cached_previews.cache_clear()
    source = tmp_path / "kimi"
    image_path = source / "assets/themes/work/blue-flame-brand.jpg"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (320, 540), "white").save(image_path)
    service = PresentationService(tmp_path)
    monkeypatch.setattr(service, "source", lambda _: source)
    original_open = Image.open
    calls = []

    def counted_open(path, *args, **kwargs):
        calls.append(Path(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Image, "open", counted_open)
    first = service.previews(template_by_id("kimi-work"))
    assert len(first) == 3
    assert service.previews(template_by_id("kimi-work")) == first
    assert calls == [image_path]
    Image.new("RGB", (640, 1080), "black").save(image_path)
    assert service.previews(template_by_id("kimi-work")) != first
    assert calls == [image_path, image_path]


def test_gallery_returns_covers_only_but_detail_keeps_all_pages(tmp_path, monkeypatch):
    service = PresentationService(tmp_path)
    monkeypatch.setattr(service, "previews", lambda _: ["cover", "page2", "page3"])
    assert all(template["previews"] == ["cover"] for template in service.catalog()["templates"])
    assert service.previews(template_by_id("kimi-work")) == ["cover", "page2", "page3"]
