# Current Status

## Current shape of the repo
- Small Python project with a CLI automation core and a FastAPI web wrapper.
- Most of the business logic lives in `main.py` and is reused from `web_app.py`.
- The repository also includes a local PDF viewer, a Playwright compatibility layer, and a Tk desktop UI with browser selection.

## Recommended first files to inspect
- `AI_CONTEXT.md`
- `main.py`
- `web_app.py`
- `run_web.py`

## Useful notes for future work
- Keep changes small and localized unless the user asks for a bigger refactor.
- Reuse the automation helpers in `main.py` instead of duplicating scraping logic in the web layer.
- If you change how PDFs are opened or cached, check both `pdf_viewer.py` and the callers in `main.py` / `web_app.py`.
- Chrome is now the default browser for CLI, web, and Tk entrypoints, while Edge remains an explicit Playwright channel override.
- Cartola Banco Estado now depends on preserving the litigant row `onclick` so the popup can be built with the real RUT/parte/cuenta parameters.
- The Playwright compatibility layer now translates arbitrary `arguments[n]` placeholders, not just the first two arguments, to avoid `Page.evaluate` reference errors in cartola and related flows.

## Open follow-ups
- No formal test suite is visible in the repo yet.
- If the automation becomes more complex, consider splitting `main.py` into smaller modules.
