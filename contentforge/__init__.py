"""ContentForge-AI: an AI content factory that turns screen recordings into Reels/Shorts.

The package is organised as a set of loosely coupled modules that are composed by
:mod:`contentforge.pipeline`:

* ``config``      - typed YAML configuration loading.
* ``log``         - Rich console + dated file logging.
* ``utils``       - FFmpeg wrapper, filesystem, hashing, timing helpers.
* ``input``       - folder watcher and job intake.
* ``processing``  - audio and video processing built on FFmpeg/OpenCV.
* ``ai``          - transcription, script writing, TTS backends, LLM client.
* ``subtitles``   - caption segmentation and SRT/ASS rendering.
* ``thumbnails``  - thumbnail image + Canva brief generation.
* ``analytics``   - metrics storage and performance analysis.
* ``output``      - upload package builder.
* ``archive``     - archival of finished jobs.
* ``cleanup``     - disk hygiene with safety rails.
* ``scheduling``  - APScheduler-based orchestration.
* ``dashboard``   - Streamlit UI.
"""

__version__ = "0.3.0"
__all__ = ["__version__"]
