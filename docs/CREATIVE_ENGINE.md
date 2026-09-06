# AI Creative Engine — ContentForge-AI v0.4

The **Creative Engine** transforms raw screen recordings into cohesive, high-retention vertical Reels by executing a unified creative direction before any rendering begins.

## Architecture

```
Raw Screen Recording + Website Context
                ↓
    Video Understanding (OpenCV + OCR + Gemini Vision)
                ↓
    Creative Director (Gemini LLM / Rule-Based)
                ↓  Produces: CreativePlan (JSON + MD)
    ┌───────────┴───────────┐
    ↓                       ↓
Smart Framing & Cuts    Grounded Scripting
    ↓                       ↓
Dynamic Captions        Narration & Timing
    ↓                       ↓
Music & SFX Mixing      Canva / Pillow Cover
                ↓
    Technical + Creative Quality Gates
```

## The Creative Director (`contentforge/creative/director.py`)

The Creative Director acts as the central brain of the v0.4 pipeline:
1. **Analyzes Visual Signals**: Ingests actions (clicks, typing, reveals), text mass, scene changes, and AI vision summaries.
2. **Selects Narrative Arc**: Chronological, Problem-First, Result-First, or Tutorial step-by-step.
3. **Generates & Scores Hooks**: Produces multiple hook candidates across styles (problem, curiosity, shock, question, secret) and scores them for retention.
4. **Shapes Downstream Decisions**:
   - **Edit Pacing**: High-energy vs calm minimal cuts
   - **Layout**: Auto, Canvas, or Fill
   - **Music & SFX**: Mood, tempo energy, ducking depth
   - **Captions**: Style, sizing, and position
   - **Cover**: Headline, concept, screenshot moment

## Creative Presets

ContentForge-AI v0.4 introduces 6 production-grade creative presets configured in `config/config.yaml`:

| Preset | Target Content | Hook Style | Music Mood | Motion |
|---|---|---|---|---|
| `student_reel` | StudentTools.pk standard | Problem | Tech / Upbeat | Smooth |
| `ai_tool` | AI tools & generative web apps | Curiosity | Tech (high energy) | Dynamic |
| `productivity` | Notion/productivity systems | Problem | Upbeat / Clean | Minimal |
| `coding` | Dev tools & terminal utilities | Question | Tech ambient | Canvas layout |
| `website_discovery` | Hidden gem websites | Shock | Energetic | Fast-paced |
| `tutorial` | In-depth feature walk-throughs | Problem | Calm | Steady |
| `premium_minimal` | Aesthetic showcases | Intrigue | Ambient | Gentle |

Select a preset via:
```bash
contentforge process data/input/demo.mp4 --preset ai_tool
# or via environment variable:
export CONTENTFORGE_PRESET=website_discovery
```
