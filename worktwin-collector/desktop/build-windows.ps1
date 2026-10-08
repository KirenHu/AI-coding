$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
py -3 -m venv .build-venv
& .\.build-venv\Scripts\python.exe -m pip install '.[package]'
& .\.build-venv\Scripts\pyinstaller.exe --clean --noconfirm --onedir --windowed --name WorkTwin --collect-data worktwin --collect-submodules worktwin --collect-submodules uvicorn desktop/launcher.py
Write-Host "Created dist/WorkTwin/WorkTwin.exe (unsigned)"
