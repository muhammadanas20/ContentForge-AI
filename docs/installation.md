# Installation

## 1. System requirements

| Requirement | Notes |
|---|---|
| Linux (Fedora 39+ recommended; Debian/Ubuntu/Arch work too) | Tested on Fedora and Debian 12 |
| Python 3.10 - 3.12 | `python3 --version` |
| FFmpeg **with libass, libfreetype, libx264** | Fedora needs RPM Fusion for the full build |
| 4 GB RAM (8 GB comfortable) | Whisper `base` + FFmpeg fit easily; `small`/`medium` need more |
| ~3 GB disk for models | Whisper base ≈ 150 MB, Piper voice ≈ 60 MB, Kokoro ≈ 350 MB + torch |
| A DejaVu / Liberation TrueType font | Used for subtitles and thumbnails |

GPU is optional. Set `transcription.device: cuda` and `compute_type: float16` if you have an NVIDIA card with CUDA
and cuDNN installed.

## 2. Fedora - automated

```bash
git clone https://github.com/muhammadanas20/ContentForge-AI
cd ContentForge-AI
scripts/install_fedora.sh
```

The script enables RPM Fusion, installs `ffmpeg python3 python3-devel gcc dejavu-sans-fonts fontconfig inotify-tools`,
creates `.venv`, installs the requirements, copies `.env.example` → `.env` and runs `contentforge doctor`.

## 3. Fedora - manual

```bash
# RPM Fusion (free) - needed for a full ffmpeg build
sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
sudo dnf install -y --allowerasing ffmpeg
ffmpeg -hide_banner -filters | grep -E " (ass|zoompan|loudnorm) "   # all three must appear

sudo dnf install -y python3 python3-pip python3-devel gcc dejavu-sans-fonts fontconfig

cd ContentForge-AI
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
pip install -e .            # provides the `contentforge` command
cp .env.example .env
contentforge doctor
```

### Debian / Ubuntu

```bash
sudo apt install -y ffmpeg python3-venv python3-dev build-essential fonts-dejavu-core
# then the same venv steps as above
```

If your distro's ffmpeg lacks `libass`, the wrapper falls back to the static build shipped with
`pip install imageio-ffmpeg`, or set `FFMPEG_BINARY=/path/to/ffmpeg`.

## 4. Python environment details

* `requirements.txt` - runtime dependencies (Whisper, OpenCV headless, Pillow, watchdog, APScheduler, Streamlit, Piper, Edge TTS).
* `requirements-dev.txt` - adds pytest, coverage and ruff.
* `pyproject.toml` - package metadata; extras: `pip install -e ".[tts,dashboard,postgres,dev]"`.

Always activate the venv before using the CLI: `source .venv/bin/activate`.

## 5. Models

| Component | When downloaded | Where |
|---|---|---|
| Faster-Whisper `base` (configurable) | First transcription | `data/models/whisper/` |
| Piper voice `en_US-lessac-medium` | First narration | `data/models/piper/` |
| Kokoro (optional) | First narration | HuggingFace cache |

Pre-download on a machine with internet:

```bash
python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root='data/models/whisper')"
python -c "from contentforge.config import load_settings; from contentforge.ai.tts import PiperTTS; PiperTTS(load_settings().tts.piper).ensure_model()"
```

To use another Piper voice pick a name from https://huggingface.co/rhasspy/piper-voices (e.g. `en_GB-alan-medium`,
`en_US-amy-medium`) and set `tts.piper.voice`.

For a step-by-step verification of the models on a real Fedora laptop (TTS listen test, word-alignment check,
first video, low-disk settings) follow **`docs/fedora-real-system-test.md`**.

## 6. Optional components

### Kokoro TTS (higher quality, heavier)

```bash
pip install kokoro soundfile          # pulls torch (~2 GB)
sudo dnf install espeak-ng            # phonemiser fallback
```
Set `tts.engine: kokoro` and pick a voice (`af_heart`, `af_bella`, `am_adam`, `bf_emma`, ...).

### Edge TTS (online, free, no key)

`pip install edge-tts` (already in requirements). Set `tts.engine: edge`, choose `tts.edge.voice`
(e.g. `en-US-AriaNeural`, `en-IN-NeerjaNeural`, `ur-PK-UzmaNeural`). Requires internet.

### LLM script polishing

The rule-based script writer works offline. To let a model rewrite scripts and captions:

```bash
# Local, private:
ollama pull llama3.1
echo CONTENTFORGE_LLM_PROVIDER=ollama >> .env

# or OpenAI:
echo CONTENTFORGE_LLM_PROVIDER=openai >> .env
echo OPENAI_API_KEY=sk-... >> .env
```
LLM output is validated against the transcript (`script.strict_grounding`) - if it invents features the
rule-based script is used instead.

### PostgreSQL

```bash
pip install psycopg2-binary
echo CONTENTFORGE_DATABASE_URL=postgresql://user:pass@localhost/contentforge >> .env
```

## 7. Run as a service (systemd, user session)

```bash
scripts/install_service.sh
systemctl --user status contentforge contentforge-dashboard
journalctl --user -u contentforge -f
```
The units live in `scripts/systemd/`; `loginctl enable-linger` keeps them alive after logout.

## 8. Verify

```bash
contentforge doctor
pytest -q          # optional, ~40 s
```
Then drop a recording into `data/input/` and check `data/output/`.
