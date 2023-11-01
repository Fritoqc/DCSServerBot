@echo off
python --version > NUL 2>&1
if %ERRORLEVEL% EQU 9009 (
    echo python.exe is not in your PATH.
    echo Chose "Add python to the environment" in your Python-installer.
    exit /B %ERRORLEVEL%
)
SET VENV=%USERPROFILE%\.dcssb
if not exist "%VENV%" (
    echo Creating the Python Virtual Environment
    python -m venv "%VENV%"
    "%VENV%\Scripts\python.exe" -m pip install --upgrade pip
    "%VENV%\Scripts\pip" install -r requirements.txt
)
:loop
"%VENV%\Scripts\python" run.py
if %ERRORLEVEL% EQU -1 (
    goto loop
)
