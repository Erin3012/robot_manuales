# Robot Manuales

Automatizacion en Python para consultar y trabajar con el sistema SITFA, con una interfaz CLI y una capa web local.

## Arranque rapido
- CLI: `python main.py`
- App de escritorio: `python app_tk.py`
- Web local: `python run_web.py`

## Contexto del proyecto
Si vas a seguir trabajando en este repo, empieza por estos archivos:
- [AI_CONTEXT.md](AI_CONTEXT.md)
- [STATUS.md](STATUS.md)
- [DECISIONS.md](DECISIONS.md)

## Estructura general
- `main.py`: logica principal de automatizacion y extraccion.
- `web_app.py`: API FastAPI y UI web.
- `pdf_viewer.py`: visor local de PDFs.
- `browser_compat.py`: capa de compatibilidad con navegador.
- `run_web.py`: lanzador de la version web.

## Dependencias
- `playwright`
- `fastapi`
- `uvicorn`
- `PyMuPDF`
- `Pillow`

## Nota
- El archivo `.env` se carga automaticamente desde `main.py`.
- Chrome es el navegador por defecto para CLI y la version web. Puedes overridearlo con `SITFA_BROWSER` o `SITFA_WEB_BROWSER`; `SITFA_WEB_HEADLESS=0` desactiva el modo headless en la web.
- Si necesitas forzar el canal de Playwright, usa `SITFA_PLAYWRIGHT_CHANNEL` (`chrome` o `msedge`).
- Si instalas dependencias desde cero, recuerda ejecutar `playwright install chromium` para tener el navegador disponible.
- Si cambias un flujo importante, actualiza tambien `AI_CONTEXT.md` y `STATUS.md`.
