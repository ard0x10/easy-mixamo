@echo off
rem easy-mixamo GUI launcher - starts the app without a console window.
setlocal
set "GUI=%~dp0gui.py"

rem 1) the py launcher (pyw), if present
where pyw.exe >nul 2>&1 && (
    start "" pyw.exe -3 "%GUI%"
    exit /b
)

rem 2) pythonw on PATH
where pythonw.exe >nul 2>&1 && (
    start "" pythonw.exe "%GUI%"
    exit /b
)

rem 3) typical per-user installs
for /f "delims=" %%P in ('dir /b /o-n "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do (
    if exist "%LOCALAPPDATA%\Programs\Python\%%P\pythonw.exe" (
        start "" "%LOCALAPPDATA%\Programs\Python\%%P\pythonw.exe" "%GUI%"
        exit /b
    )
)

echo Python was not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup.
pause
