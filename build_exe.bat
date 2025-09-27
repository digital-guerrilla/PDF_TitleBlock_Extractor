@echo off
setlocal ENABLEDELAYEDEXPANSION

rem Resolve repository root relative to this script
set "REPO_DIR=%~dp0"
pushd "%REPO_DIR%" >nul

rem Prefer the project virtual environment if it exists
set "VENV_PYTHON=%REPO_DIR%\.venv\Scripts\python.exe"
if exist "%VENV_PYTHON%" (
    set "PYTHON_EXEC=%VENV_PYTHON%"
) else (
    rem Fallback to the first Python on PATH
    set "PYTHON_EXEC="
    for /f "delims=" %%I in ('where python 2^>nul') do (
        if not defined PYTHON_EXEC set "PYTHON_EXEC=%%I"
    )
    if not defined PYTHON_EXEC (
        echo [ERROR] Python interpreter not found. Ensure Python is installed or create .venv.
        popd >nul
        exit /b 1
    )
)

echo Using Python interpreter: "%PYTHON_EXEC%"

rem Ensure PyInstaller is available
"%PYTHON_EXEC%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    "%PYTHON_EXEC%" -m pip install --upgrade pyinstaller || (
        echo [ERROR] Failed to install PyInstaller.
        popd >nul
        exit /b 1
    )
)

rem Clean previous build output (optional)
if exist "%REPO_DIR%\build" (
    echo Removing existing build directory...
    rmdir /s /q "%REPO_DIR%\build"
)
if exist "%REPO_DIR%\dist" (
    echo Removing existing dist directory...
    rmdir /s /q "%REPO_DIR%\dist"
)

rem Build the executable using the PyInstaller spec file
if exist "%REPO_DIR%\main.spec" (
    echo Building PDF_TitleBlock_Extractor.exe from main.spec...
    "%PYTHON_EXEC%" -m PyInstaller --noconfirm --clean "%REPO_DIR%\main.spec"
) else (
    echo Building PDF_TitleBlock_Extractor.exe directly from main.py...
    "%PYTHON_EXEC%" -m PyInstaller --noconfirm --clean --name PDF_TitleBlock_Extractor --windowed "%REPO_DIR%\main.py"
)

if errorlevel 1 (
    echo [ERROR] Build failed.
    popd >nul
    exit /b 1
)

echo Build complete. The executable is located at:
echo     %REPO_DIR%\dist\PDF_TitleBlock_Extractor\PDF_TitleBlock_Extractor.exe

popd >nul
endlocal
exit /b 0
