"""Tests for Music Catalog and Track Selection."""

from __future__ import annotations

import json
from pathlib import Path

from contentforge.media.music import MusicCatalog, MusicTrack


def test_music_catalog_empty_selection(tmp_path):
    catalog_dir = tmp_path / "music"
    catalog_dir.mkdir()
    (catalog_dir / "catalog.json").write_text('{"tracks": []}')

    cat = MusicCatalog(catalog_dir)
    assert len(cat.list_tracks()) == 0

    selection = cat.select(mood="tech")
    assert selection.track is None


def test_music_catalog_mood_match(tmp_path):
    catalog_dir = tmp_path / "music"
    catalog_dir.mkdir()

    tracks = [
        {
            "id": "track1",
            "title": "Future Cyber",
            "artist": "SynthLab",
            "license": "CC0",
            "mood": ["tech", "energetic"],
            "energy": 0.8,
            "local_path": "cyber.mp3",
        },
        {
            "id": "track2",
            "title": "Calm Study",
            "artist": "LoFi Beats",
            "license": "CC-BY",
            "mood": ["calm", "study"],
            "energy": 0.3,
            "local_path": "study.mp3",
        },
    ]
    (catalog_dir / "catalog.json").write_text(json.dumps({"tracks": tracks}))

    cat = MusicCatalog(catalog_dir)
    assert len(cat.list_tracks()) == 2

    # Select tech track
    sel = cat.select(mood="tech", target_energy=0.8)
    assert sel.track is not None
    assert sel.track.id == "track1"

    # Search
    results = cat.search("Study")
    assert len(results) == 1
    assert results[0].id == "track2"
