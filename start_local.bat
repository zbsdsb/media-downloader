@echo off
title Pro Media Downloader
cd /d "%~dp0"
echo Starting Pro Media Downloader on http://127.0.0.1:8080 ...
.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8080 --reload
pause
