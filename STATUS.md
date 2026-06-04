# Current Status

## Current shape of the repo
- Small Python project with a CLI automation core and a FastAPI web wrapper.
- Most of the business logic lives in `main.py` and is reused from `web_app.py`.
- The repository also includes a local PDF viewer, a browser compatibility layer, and a Tk desktop UI with browser selection.

## Recommended first files to inspect
- `AI_CONTEXT.md`
- `main.py`
- `web_app.py`
- `run_web.py`

## Useful notes for future work
- Keep changes small and localized unless the user asks for a bigger refactor.
- Reuse the automation helpers in `main.py` instead of duplicating scraping logic in the web layer.
- If you change how PDFs are opened or cached, check both `pdf_viewer.py` and the callers in `main.py` / `web_app.py`.
- `app_tk.py` now lets the user pick Chrome or Edge before login, and `main.py` can launch Chrome directly.

## Open follow-ups
- No formal test suite is visible in the repo yet.
- If the automation becomes more complex, consider splitting `main.py` into smaller modules.
