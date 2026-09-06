"""Thumbnail generator tests."""

from PIL import Image

from contentforge.config.schema import BrandingConfig, ThumbnailConfig
from contentforge.thumbnails import ThumbnailGenerator


def test_render_and_brief(tmp_path):
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1280, 720), (40, 90, 200)).save(frame)
    gen = ThumbnailGenerator(
        ThumbnailConfig(width=540, height=960, title_font_size=48), BrandingConfig()
    )
    out = gen.render(
        frame,
        tmp_path / "thumb.jpg",
        "Free PDF Converter Every Student Needs",
        "No sign-up required",
    )
    im = Image.open(out)
    assert im.size == (540, 960)
    # title area should contain plenty of near-white pixels (rendered text)
    import numpy as np

    arr = np.asarray(im.convert("RGB").crop((0, 400, 540, 700)))
    px = [tuple(p) for p in arr.reshape(-1, 3)]
    assert sum(1 for p in px if min(p) > 230) > 500

    brief = gen.build_brief(
        "Free PDF Converter Every Student Needs Today",
        "Still paying for PDF tools?",
        ["pdf", "convert"],
        "smallpdf.com",
    )
    assert brief.title == "Free PDF Converter Every Student"
    assert "pdf" in brief.prompt and "StudentTools.pk" in brief.prompt
    assert len(brief.text_suggestions) == 5
    files = gen.write_brief(brief, tmp_path)
    assert files["md"].exists() and "Canva workflow" in files["md"].read_text()
    assert files["json"].exists()
