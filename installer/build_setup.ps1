# Builds dist\EPUB-Audiobook-Setup.exe: a single installer that carries only the light parts
# (embedded CPython, this project, uv, the launcher exe) and downloads the heavy dependencies
# (torch/kokoro/Supertonic/wxPython), ffmpeg and the TTS models while it installs.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$root = Split-Path -Parent $PSScriptRoot
$payload = Join-Path $PSScriptRoot 'payload'
$pyDir = Join-Path $payload 'python'
$cache = Join-Path $root '.tools\cache'
$pyVersion = '3.12.10'
$pyZip = Join-Path $cache "python-$pyVersion-embed-amd64.zip"
$pyUrl = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip"
$venvPy = Join-Path $root '.venv\Scripts\python.exe'

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Step "Payload limpio"
if (Test-Path $payload) { Remove-Item $payload -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pyDir, $cache | Out-Null

Step "Python embebido $pyVersion"
if (-not (Test-Path $pyZip)) { Invoke-WebRequest -Uri $pyUrl -OutFile $pyZip }
Expand-Archive -Path $pyZip -DestinationPath $pyDir -Force
Set-Content -Path (Join-Path $pyDir 'python312._pth') -Encoding ASCII -Value @(
  'python312.zip'
  '.'
  'Lib\site-packages'
  'import site'
)

Step "Runtime de Visual C++"
foreach ($dll in 'msvcp140.dll', 'msvcp140_1.dll', 'concrt140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll') {
  $src = Join-Path $env:WINDIR "System32\$dll"
  if (Test-Path $src) { Copy-Item $src (Join-Path $pyDir $dll) -Force } else { Write-Warning "falta $dll" }
}

Step "Código de la aplicación"
robocopy (Join-Path $root 'epub2audiobook') (Join-Path $pyDir 'epub2audiobook') /E /NFL /NDL /NJH /NJS /NP /XD '__pycache__' | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy falló ($LASTEXITCODE)" }
Copy-Item (Join-Path $root 'pyproject.toml') $pyDir -Force -ErrorAction SilentlyContinue
Copy-Item (Join-Path $root 'LICENSE') $pyDir -Force -ErrorAction SilentlyContinue

Step "uv (instalador de paquetes) y lanzador"
Copy-Item (Join-Path $env:USERPROFILE '.local\bin\uv.exe') (Join-Path $payload 'uv.exe') -Force
$launcher = Join-Path $root 'dist\EPUB-Audiobook\EPUB a Audiobook.exe'
if (-not (Test-Path $launcher)) { throw "falta el lanzador: $launcher (corré .tools\build_portable.ps1)" }
Copy-Item $launcher (Join-Path $payload 'launcher.exe') -Force

Step "Compilando el instalador"
& $venvPy -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name 'EPUB-Audiobook-Setup' `
  --icon (Join-Path $root 'epub2audiobook\assets\app.ico') `
  --add-data "$payload;payload" `
  --distpath (Join-Path $root 'dist') `
  --workpath (Join-Path $root '.tools\build\setup') `
  --specpath (Join-Path $root '.tools\build') `
  (Join-Path $PSScriptRoot 'setup_app.py') | Out-Host
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló" }

$exe = Join-Path $root 'dist\EPUB-Audiobook-Setup.exe'
Step "Listo: $exe ($([math]::Round((Get-Item $exe).Length / 1MB, 1)) MB)"
