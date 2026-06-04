# AI Context Pack

This repo is a small automation project for the SITFA judicial system.

## What the project does
- Automates login and case consultation in SITFA.
- Extracts case history, liquidation, and "escritos resolver" data.
- Handles "litigantes" views and related popups.
- Opens and previews PDF documents locally.
- Exposes a web UI that wraps the same automation flows.

## Main entry points
- `main.py`: CLI automation and scraping logic built on the local Playwright compatibility layer.
- `web_app.py`: FastAPI app that exposes login, consult, history, pending cases, litigants, cartola, and PDF endpoints.
- `run_web.py`: Launcher for the web app on `127.0.0.1:8050`.
- `app_tk.py`: Desktop UI variant with browser selector and local PDF preview.
- `pdf_viewer.py`: Local PDF viewer used by the automation flow.
- `browser_compat.py`: Playwright-based compatibility layer that mirrors Selenium-like APIs.
- Litigant selections now preserve the original row `onclick` so Cartola Banco Estado can reuse the real popup parameters instead of guessing them from the visible row text.
- The Playwright wrapper now rewrites any `arguments[n]` placeholder it sees in a script, which matters for cartola filters and similar multi-argument `execute_script` calls.

## How to run
- CLI / automation: `python main.py`
- Desktop app: `python app_tk.py`
- Web app: `python run_web.py`

## Dependencies
- `playwright`
- `fastapi`
- `uvicorn`
- `PyMuPDF`
- `Pillow`

## Important environment notes
- `.env` is loaded automatically by `main.py`.
- `run_web.py` sets `SITFA_WEB_BROWSER=chrome` by default.
- `main.py` supports `chrome` and `edge` browser channels, with Chrome as the default.
- `SITFA_PLAYWRIGHT_CHANNEL` can force the underlying Playwright channel (`chrome` or `msedge`).
- The code expects Playwright plus a Chrome/Chromium browser install available locally.
- Cartola Banco Estado is sensitive to the litigant row metadata; when debugging empty account lists, verify that the selected litigant carries the original `onclick` from the SITFA table.
- If a Playwright `Page.evaluate` error mentions `arguments is not defined`, check whether the script was using more than two positional arguments and needs to be translated by `browser_compat.py`.

## What to tell the AI in a new chat
- Start from this file instead of re-reading the whole repo.
- Ask for the specific area to change: login, scraping, web UI, PDF handling, or browser compatibility.
- Mention the entry point you are using, because the CLI and web app share logic but not all flows are identical.
