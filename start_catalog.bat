@echo off
rem Adds the selected winning Meesho products (data\discovery\catalog_selected.json) to Shopify as Active.
rem Resumable: run it again after a stop and it continues where it left off. Progress: logs\catalog_run.out
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" catalog.py run >> logs\catalog_run.out 2>&1
