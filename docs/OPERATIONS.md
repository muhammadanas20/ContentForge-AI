# Operations & CLI Guide — ContentForge-AI v0.4

ContentForge-AI provides a comprehensive CLI for running, inspecting, debugging, and maintaining automated Reel production.

## Commands Overview

### Running Jobs

```bash
# Process a single recording with website context and preset
contentforge process data/input/recording.mp4 --website smallpdf.com --preset student_reel

# Run the automated daemon (watches data/input/ for drops)
contentforge run

# Retry or resume a failed job from a specific step
contentforge retry <JOB_ID> --from grounded_script
contentforge resume
```

### Inspection & Debugging (New in v0.4)

```bash
# Inspect job context, artifacts, and creative plan
contentforge inspect <JOB_ID>

# View technical quality gates and creative quality scores
contentforge quality <JOB_ID>
```

### Creative & Asset Management (New in v0.4)

```bash
# List available creative presets and styling
contentforge templates

# Search and list background music tracks
contentforge music

# Verify Canva Connect integration status and capabilities
contentforge canva

# Check environment health (FFmpeg, Whisper, Piper, Disk, LLM)
contentforge doctor
```

## Environment Configuration (`.env`)

ContentForge-AI v0.4 supports Google Gemini as the recommended AI provider:

```ini
CONTENTFORGE_LLM_PROVIDER=gemini
GEMINI_API_KEY=AIzaSy...
GEMINI_MODEL=gemini-2.5-flash

# Optional Canva Connect API
CANVA_CLIENT_ID=
CANVA_CLIENT_SECRET=
CANVA_ACCESS_TOKEN=
```
