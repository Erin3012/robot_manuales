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
- `selenium`
- `fastapi`
- `uvicorn`
- `PyMuPDF`
- `Pillow`

## Nota
- El archivo `.env` se carga automaticamente desde `main.py`.
- Si cambias un flujo importante, actualiza tambien `AI_CONTEXT.md` y `STATUS.md`.
