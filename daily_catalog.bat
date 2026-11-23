@echo off
rem Daily catalog growth: searches Meesho for new winning products and adds up to 100 per category
rem (women, kids, jewellery, nightwear) to Shopify as Active. Takes several hours; safe to re-run the
rem same day (it continues where it stopped). Change the number below for more/fewer per category.
rem Progress: logs\catalog_daily.out
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
".venv\Scripts\python.exe" catalog.py daily 100 >> logs\catalog_daily.out 2>&1
