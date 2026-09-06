# Canva Integration Plan — ContentForge-AI v0.4

## Overview

Canva Connect APIs provide premium design generation for covers, thumbnails,
end cards, and quote cards. The integration is **optional** — the existing Pillow
rendering is the always-available fallback.

## Architecture

```
ContentForge
    ↓ generate: cover text, screenshot, brand colors, layout
Canva Connect API
    ↓ create design from template
    ↓ upload assets
    ↓ autofill (if available)
    ↓ export as image
    ↓ download
ContentForge
    ↓ use in final package
```

## Canva Connect API Capabilities

Based on the current Canva Connect REST API:

| Feature | Endpoint | Notes |
|---|---|---|
| OAuth 2.0 | `/oauth/authorize` | Required for all API access |
| Create design | `POST /v1/designs` | Requires template or blank design |
| Upload asset | `POST /v1/asset-uploads` | Images for cover backgrounds |
| Export design | `POST /v1/designs/{id}/export` | PNG/JPEG export |
| List templates | `GET /v1/brand-templates` | **Requires Canva Pro/Teams** |
| Autofill | `POST /v1/autofills` | **Requires Canva Enterprise** |

## Capability Detection

```python
@dataclass
class CanvaCapabilityReport:
    authenticated: bool
    can_create_designs: bool
    can_upload_assets: bool
    can_export: bool
    can_list_templates: bool  # Pro/Teams only
    can_autofill: bool        # Enterprise only
    plan_type: str            # free | pro | teams | enterprise
    limitations: list[str]
```

At runtime, the system probes available capabilities and adapts:

- **Full capability**: Create design from template, autofill, export
- **Partial (Pro)**: Create blank design, upload screenshot, export
- **Minimal (Free)**: Upload asset only — use Pillow for composition
- **No Canva**: Pure Pillow rendering (existing behavior)

## Template System

```
data/assets/templates/
    studenttools/
        cover_bold.json      # text-heavy, bold title
        cover_result.json    # screenshot-focused
        cover_minimal.json   # clean, minimal text
        endcard_cta.json     # CTA end card
```

Each template defines:
- Typography (font, size, weight, color)
- Layout regions (title, subtitle, image, logo, CTA)
- Color scheme
- Brand elements

Templates are data-driven JSON — not hardcoded design logic.

## Security

- OAuth tokens stored in `.env`, never in code or logs
- Tokens are refreshed automatically
- API keys are redacted in all log output
- Graceful handling of expired/revoked tokens

## Fallback Guarantee

**The pipeline NEVER breaks because Canva is unavailable.**

```python
try:
    cover = canva_client.create_premium_cover(...)
except (CanvaError, CanvaUnavailable):
    cover = pillow_cover_generator.generate(...)  # always works
```
