<#
  Surround Mixer installer for Windows.

  Double-click Install.cmd, or run:
      powershell -ExecutionPolicy Bypass -File install.ps1 [-Startup] [-Yes]
      powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall

  -Startup    also start Surround Mixer (minimized) when you log in
  -Yes        don't ask questions (installs Python with winget if it's missing)
  -Uninstall  remove the app, its shortcuts and its settings
#>
param([switch]$Startup, [switch]$Yes, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$Dest = Join-Path $env:LOCALAPPDATA 'Programs\SurroundMixer'
$Src = $PSScriptRoot
$Files = 'surround_mixer.py', 'webremote.py', 'calibrate.py', 'linux_io.py', 'make_icon.py', 'surround.ico',
         'requirements.txt', 'README.md'
$Desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Surround Mixer.lnk'
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Surround Mixer.lnk'
$StartupLnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\Surround Mixer.lnk'

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Ask($question) {
    if ($Yes) { return $true }
    return (Read-Host "$question [Y/n]") -notmatch '^(n|no)$'
}
function Stop-App {
    # find it by its script (a minimized or tray window has no title to match)
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*surround_mixer.py*' } |
        ForEach-Object {
            $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
            if (-not $p) { return }
            $p.CloseMainWindow() | Out-Null
            if (-not $p.WaitForExit(5000)) { Stop-Process -Id $p.Id -Force }
        }
}

# ------------------------------------------------------------------ uninstall
if ($Uninstall) {
    Step 'Removing Surround Mixer'
    Stop-App
    foreach ($l in $Desktop, $StartMenu, $StartupLnk) { if (Test-Path $l) { Remove-Item $l -Force } }
    if (Test-Path $Dest) {
        if ($Src -and ((Resolve-Path $Src).Path -eq (Resolve-Path $Dest).Path)) {
            Write-Host "This script is inside $Dest - delete that folder yourself after closing this window."
        } elseif (Ask "Delete $Dest (including your settings and paired phones)?") {
            Remove-Item $Dest -Recurse -Force
        }
    }
    Write-Host "`nSurround Mixer was removed." -ForegroundColor Green
    exit 0
}

# ------------------------------------------------------------------ Python
Step 'Looking for Python 3.10 or newer'
function Find-Python {
    foreach ($c in @(@('py', '-3'), @('python'))) {
        $exe = Get-Command $c[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $args_ = @($c | Select-Object -Skip 1) + @('-c', 'import sys; print(sys.executable); print(sys.version_info >= (3, 10))')
        try { $out = & $exe.Source @args_ 2>$null } catch { continue }
        if ($out -and $out[-1] -eq 'True' -and $out[0] -notmatch 'WindowsApps') { return $out[0] }
    }
    $local = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Programs\Python') -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($local) { return $local.FullName }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'Python 3.10+ is needed. Install it from https://www.python.org/downloads/ and run this again.'
    }
    if (-not (Ask 'Python is not installed. Install Python 3.13 now with winget?')) { throw 'Python is required.' }
    winget install --id Python.Python.3.13 -e --scope user --silent --accept-package-agreements --accept-source-agreements
    $Python = Find-Python
    if (-not $Python) { throw 'Python was installed but could not be found. Open a new window and run this again.' }
}
Write-Host "Using $Python"

# ------------------------------------------------------------------ files
Step "Copying the app to $Dest"
Stop-App
New-Item -ItemType Directory -Force -Path (Join-Path $Dest 'web') | Out-Null
if ((Resolve-Path $Src).Path -ne (Resolve-Path $Dest).Path) {
    foreach ($f in $Files) { Copy-Item (Join-Path $Src $f) $Dest -Force }
    Copy-Item (Join-Path $Src 'web\*') (Join-Path $Dest 'web') -Force
} else {
    Write-Host 'Already in place.'
}

# ------------------------------------------------------------------ packages
Step 'Installing Python packages (a private environment just for this app)'
$Venv = Join-Path $Dest 'venv'
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) { & $Python -m venv $Venv }
$VPy = Join-Path $Venv 'Scripts\python.exe'
$VPyw = Join-Path $Venv 'Scripts\pythonw.exe'
& $VPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VPy -m pip install -r (Join-Path $Dest 'requirements.txt') --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw 'Installing the Python packages failed (see above).' }

# ------------------------------------------------------------------ shortcuts
Step 'Creating shortcuts'
$shell = New-Object -ComObject WScript.Shell
function Make-Shortcut($path, $extra) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $VPyw
    $s.Arguments = "`"$Dest\surround_mixer.py`" $extra".Trim()
    $s.WorkingDirectory = $Dest
    $s.IconLocation = "$Dest\surround.ico"
    $s.Description = 'Surround sound across your TV and Bluetooth speakers'
    $s.Save()
}
Make-Shortcut $Desktop ''
Make-Shortcut $StartMenu ''
if ($Startup -or (Test-Path $StartupLnk)) { Make-Shortcut $StartupLnk '--minimized'; Write-Host 'Starts when you log in.' }
Write-Host 'Desktop and Start menu shortcuts are ready.'

# ------------------------------------------------------------------ audio check
Step 'Checking for a device for programs to play into'
$devices = & $VPy -c "import soundcard as sc; print('\n'.join(s.name for s in sc.all_speakers()))"
if ($devices -match 'Steam Streaming Speakers|CABLE Input') {
    Write-Host 'Found one. In Windows sound settings, choose it as your output.' -ForegroundColor Green
} else {
    Write-Host @'
No virtual audio device found. Surround Mixer needs one that you set as the Windows output
(it records it and sends the sound to your speakers). Install the free VB-CABLE driver:
    https://vb-audio.com/Cable/
then set "CABLE Input" as your Windows output.
'@ -ForegroundColor Yellow
}

Write-Host "`nSurround Mixer is installed." -ForegroundColor Green
if (Ask 'Open it now?') { Start-Process $VPyw -ArgumentList "`"$Dest\surround_mixer.py`"" -WorkingDirectory $Dest }
