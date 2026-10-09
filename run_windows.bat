@echo off
setlocal
cd /d %~dp0
if not exist .venv (
    py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
set PYTHONPATH=src
python main.py
endlocal
