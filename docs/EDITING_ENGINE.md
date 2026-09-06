# Editing Engine Design — ContentForge-AI v0.4

## Current Architecture (v0.3) — KEEP

The editing engine is built around two core modules:

### `processing/editor.py` — Plan + Render

```
VideoUnderstanding + silences
        ↓
plan_edit()
  ├── _keep_ranges()     → remove dead time
  ├── _fit_budget()      → speed up or drop material
  ├── _cut_points()      → shot boundaries on actions
  ├── _build_shots()     → Shot objects with timing
  ├── _assign_roles()    → hook/setup/demo/payoff/cta
  └── _plan_views()      → content-aware framing per shot
        ↓
EditPlan (typed artifact)
        ↓
SmartEditor.render()
  ├── per-shot: crop → layout → zoompan
  └── stream-copy concat
        ↓
Composed 9:16 video
```

### `processing/framing.py` — View Scoring

```
score = text_kept·1.0 − text_cut·2.0 + cursor·0.5 + action·0.9
      + prominence·0.6 + content·0.35 + legibility·1.2 − zoom·0.25
```

- Grid search over candidate views
- Canvas vs fill layout selection
- Smoothed camera path (deadzone + EMA)

## v0.4 Enhancements

### 1. Creative Director Integration

The editing engine will receive a `CreativePlan` from the new Creative Director
stage, which influences:

- Narrative order (chronological vs result-first)
- Pacing targets per role
- Zoom intensity preference
- Transition style

### 2. Editing Score Output

After rendering, the edit is scored on:

- `hook_strength` — is the first 3 seconds compelling?
- `visual_change_rate` — does the edit feel dynamic?
- `information_density` — words per second vs visual content
- `pacing` — rhythm and variation
- `payoff_strength` — is the result moment clear?

### 3. Restraint Principle

> Professional editing is rhythm, hierarchy, intention, clarity, restraint.
> Every effect must answer: "Why is this here?"

The v0.3 engine already implements this well:
- `zoom_max: 1.07` — subtle 7% push-in, never a punch
- Transitions are short fades (0.08s)
- Click rings are subtle
- No random effects

## Layout Strategy (Unchanged from v0.3)

| Layout | When |
|---|---|
| **fill** | Content survives a 9:16 crop safely |
| **canvas** | Wide content (dashboards, tables, full pages) |

The canvas layout places the 16:9 recording inside a designed 9:16 composition
with branded background and accent border. This is superior to destructive cropping.
