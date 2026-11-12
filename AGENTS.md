# Repository Guidelines

## Project Structure

`comicdrama/` contains the FastAPI backend. Start in `app.py` for application setup, `http.py` for studio API routes, and `service.py` for workflow logic. Provider implementations live in `comicdrama/providers/`, and the LibLib client lives in `comicdrama/liblib/`.

The browser UI ships with the Python package. Edit `comicdrama/web/index.html`, scripts in `web/js/`, and styles or icons in `web/assets/` under `comicdrama/`. README images belong in `screenshots/`. Backend and frontend tests share `tests/`.

## Local Development and Build

Create and activate a Python 3.11–3.13 virtual environment, then run these commands from the repository root.

```sh
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m comicdrama
```

The first two commands install pinned development dependencies and the editable package. The last starts the studio at `http://127.0.0.1:8001/`. To build and run the packaged application with FFmpeg and CJK fonts, use `docker compose up -d --build`.

## Coding Style

Use four-space indentation, `snake_case` functions, and `PascalCase` classes in Python. Ruff targets 100 columns. Run `python -m ruff check .` for linting, `python -m ruff format --check .` for formatting, and `python -m mypy` for type checks. Match the browser scripts’ two-space indentation and `camelCase` function names.

## Testing

- Name pytest files `tests/test_<feature>.py` and test functions `test_<behavior>`. Run `python -m pytest --cov=comicdrama --cov-fail-under=85` to reproduce the CI coverage gate.
- Mark tests that require media generation or composition with `@pytest.mark.needs_ffmpeg`. They skip when FFmpeg, ffprobe, or a supported CJK font is missing.
- Run browser state tests with Node 22 using `node --test tests/test_ui_state.mjs`.

## Commits and Pull Requests

Follow the short, prefixed subjects in the Git history, such as `feat:` and `chore:`. Describe the behavior change and checks run in your PR. Link a related issue when available and include screenshots for visible UI changes.

## Agent Instructions

Use `$simplify` only on explicit request for simplification or a reuse, quality, or efficiency review. Read its `SKILL.md`. Make one pass over current-task code, including new files. Apply high-confidence changes that preserve behavior, public interfaces, and unrelated edits. For review-only requests, report findings without editing. Skip the pass when requested or when no code changed. Inspect the final diff, run relevant checks, and report changes, skipped checks, or an unavailable skill.
