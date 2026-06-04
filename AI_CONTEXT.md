# AI Context Pack

This repo is a small automation project for the SITFA judicial system.

## What the project does
- Automates login and case consultation in SITFA.
- Extracts case history, liquidation, and "escritos resolver" data.
- Handles "litigantes" views and related popups.
- Opens and previews PDF documents locally.
- Exposes a web UI that wraps the same automation flows.

## Main entry points
- `main.py`: CLI automation and scraping logic with Selenium-style helpers.
- `web_app.py`: FastAPI app that exposes login, consult, history, pending cases, litigants, cartola, and PDF endpoints.
- `run_web.py`: Launcher for the web app on `127.0.0.1:8050`.
- `app_tk.py`: Desktop UI variant with browser selector and local PDF preview.
- `pdf_viewer.py`: Local PDF viewer used by the automation flow.
- `browser_compat.py`: Playwright-based compatibility layer that mirrors Selenium-like APIs.

## How to run
- CLI / automation: `python main.py`
- Desktop app: `python app_tk.py`
- Web app: `python run_web.py`

## Dependencies
- `selenium`
- `fastapi`
- `uvicorn`
- `PyMuPDF`
- `Pillow`

## Important environment notes
- `.env` is loaded automatically by `main.py`.
- `run_web.py` sets `SITFA_WEB_BROWSER=edge` by default.
- `main.py` supports `ie`, `edge`, and `chrome` browser modes for Selenium.
- The code expects a local browser driver / browser setup compatible with the selected mode.

## What to tell the AI in a new chat
- Start from this file instead of re-reading the whole repo.
- Ask for the specific area to change: login, scraping, web UI, PDF handling, or browser compatibility.
- Mention the entry point you are using, because the CLI and web app share logic but not all flows are identical.
