@echo off
cd C:\aflfantasywire
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call venv\Scripts\activate
python auto_scrape.py
