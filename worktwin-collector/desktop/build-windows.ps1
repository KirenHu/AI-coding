$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
py -3 -m venv .build-venv
& .\.build-venv\Scripts\python.exe -m pip install '.[package]'
& .\.build-venv\Scripts\pyinstaller.exe --clean --noconfirm --onedir --windowed --name WorkTwin --collect-data worktwin --collect-data certifi --collect-submodules worktwin --collect-submodules mcp.server --collect-submodules mcp.shared --collect-submodules mcp_types --copy-metadata mcp --copy-metadata mcp-types --collect-submodules uvicorn --collect-submodules keyring.backends desktop/launcher.py
New-Item -ItemType Directory -Force dist/WorkTwin/third_party/rowboat | Out-Null
Copy-Item third_party/rowboat/* dist/WorkTwin/third_party/rowboat/
Write-Host "Created dist/WorkTwin/WorkTwin.exe (unsigned)"
