@echo off
rem Sync and analyse every org (for Windows Task Scheduler). Log: data\run.log
cd /d "%~dp0"
if not exist data mkdir data
py -3.10 hub.py run --all >> data\run.log 2>&1
