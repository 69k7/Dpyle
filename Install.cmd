@echo off
title Installing Dependencies for Dpyle...
mkdir WorkFolder
pip install -r requirements.txt
title Installing UV...
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
title Installing Pylingual with UV
cd pylingual-main
call setup-pl.cmd