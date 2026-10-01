# Robot Manuales

Automatizacion en Python para consultar y trabajar con el sistema SITFA, con una interfaz CLI y una capa web local.

## Arranque rapido
- CLI: `python main.py`
- App de escritorio: `python app_tk.py`
- Web local: `python run_web.py`

## Crear el EXE
La aplicación de escritorio se empaqueta con PyInstaller:

```powershell
$env:ROBOT_MANUALES_RELEASE_VERSION="1"
py crear_exe.py
```

El build genera `dist/Robot_Manuales_v1.exe` y `dist/updater_helper.exe`.
El EXE consulta automáticamente la última release de GitHub al iniciar. Para
publicar una versión, crea un tag con formato `vN` y GitHub Actions compilará
los dos ejecutables y creará la release correspondiente.

## Contexto del proyecto
Si vas a seguir trabajando en este repo, empieza por estos archivos:
- [AI_CONTEXT.md](AI_CONTEXT.md)
- [STATUS.md](STATUS.md)
- [DECISIONS.md](DECISIONS.md)

## Pruebas de Cons. Lit.

Desde la carpeta que contiene `app_tk.py`, ejecuta:

```powershell
py -m unittest discover -s tests -v
```

Las pruebas usan Chrome local sin conectarse a SITFA. Cubren búsquedas dentro
de filas y frames, argumentos DOM de JavaScript, selección del demandado en
tablas anidadas, parámetros incompletos o de otra persona, codificación del
HTML, carga de la tabla Tk y acciones de doble clic.

Para verificar contra SITFA usando las credenciales del `.env` local:

```powershell
py tests/verify_cons_lit_live.py --rit Z-2248-2026
py tests/verify_cons_lit_live.py --pendientes
```

La verificación real consulta secuencialmente y guarda un reporte de conteos
en `debug_dumps/cons_lit_verificacion.json`, que no se publica en Git.

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
