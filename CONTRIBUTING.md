# Contributing

1. Fork, create a branch, `pip install -r requirements-dev.txt`.
2. Keep the style: PEP 8, type hints, docstrings, `ruff check` clean, no hard-coded paths/values (use config).
3. Add tests for every behaviour change (`docs/testing.md`), run `pytest`.
4. Update the relevant doc in `docs/` and `ROADMAP.md`.
5. Open a pull request describing *why* and *how*; include before/after frames for visual changes.

Never commit secrets, media files or model weights (`.gitignore` covers `data/` and `.env`).
