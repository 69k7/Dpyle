@echo off
if not exist main.py echo [!] main.py is missing
if exist main.py powershell cd ./ && python main.py
