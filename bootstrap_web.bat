@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT_DIR=%~dp0"
set "REPO_URL=https://github.com/Erin3012/robot_manuales.git"
set "BRANCH=main"
set "PY_CMD=python"
set "WORK_DIR="
set "USE_EXISTING=0"

call :log "Directorio actual: %ROOT_DIR%"

where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        set "FAIL_MSG=Python no esta instalado o no esta en PATH."
        goto :fail
    )
    set "PY_CMD=py -3"
)

where git >nul 2>nul
if errorlevel 1 (
    set "FAIL_MSG=Git no esta instalado o no esta en PATH."
    goto :fail
)

for %%N in ("" 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20) do (
    if not defined WORK_DIR (
        if "%%N"=="" (
            set "CANDIDATE=%ROOT_DIR%robot_manuales_repo"
        ) else (
            set "CANDIDATE=%ROOT_DIR%robot_manuales_repo_%%N"
        )

        if exist "!CANDIDATE!\.git" (
            set "WORK_DIR=!CANDIDATE!"
            set "USE_EXISTING=1"
        ) else if not exist "!CANDIDATE!" (
            set "WORK_DIR=!CANDIDATE!"
            set "USE_EXISTING=0"
        )
    )
)

if not defined WORK_DIR (
    set "FAIL_MSG=No encontre un nombre de carpeta libre para clonar el proyecto."
    goto :fail
)

if "!USE_EXISTING!"=="1" (
    call :log "Repositorio existente detectado en !WORK_DIR!"
    pushd "!WORK_DIR!" || (
        set "FAIL_MSG=No se pudo entrar al directorio del repositorio."
        goto :fail
    )
    git pull
    if errorlevel 1 (
        popd
        set "FAIL_MSG=No se pudo actualizar el repo existente."
        goto :fail
    )
    popd
) else (
    call :log "Clonando proyecto en !WORK_DIR!..."
    git clone --depth 1 --branch %BRANCH% %REPO_URL% "!WORK_DIR!"
    if errorlevel 1 (
        set "FAIL_MSG=No se pudo clonar el repo. Si es privado, autenticate en Git o usa un token/PAT."
        goto :fail
    )
)

pushd "!WORK_DIR!" || (
    set "FAIL_MSG=No se pudo entrar al directorio de trabajo."
    goto :fail
)

if not exist ".venv\Scripts\python.exe" (
    call :log "Creando entorno virtual..."
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        set "FAIL_MSG=No se pudo crear el entorno virtual."
        goto :fail
    )
)

call :log "Actualizando pip..."
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    set "FAIL_MSG=No se pudo actualizar pip."
    goto :fail
)

call :log "Instalando dependencias..."
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    set "FAIL_MSG=No se pudieron instalar las dependencias."
    goto :fail
)

call :log "Instalando navegador de Playwright..."
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    set "FAIL_MSG=No se pudo instalar Chromium para Playwright."
    goto :fail
)

set "SITFA_WEB_HOST=0.0.0.0"
set "SITFA_WEB_PORT=8050"

call :log "Iniciando la app web..."
".venv\Scripts\python.exe" run_web.py
set "EXIT_CODE=%ERRORLEVEL%"

popd

if not "%EXIT_CODE%"=="0" (
    set "FAIL_MSG=La app termino con error code %EXIT_CODE%."
    goto :fail
)

call :log "La app se cerro correctamente."
pause
exit /b 0

:fail
echo.
echo ERROR: %FAIL_MSG%
echo.
pause
exit /b 1

:log
echo [%date% %time%] %~1
exit /b 0
