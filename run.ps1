$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pythonw = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$LogDir = Join-Path $ProjectDir "logs"

if (-not (Test-Path $Pythonw)) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show(
        "Python environment not found. Run the dependency installation from README.md first.",
        "Local Dictation"
    ) | Out-Null
    exit 1
}

$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @("python.exe", "pythonw.exe") -and
    $_.CommandLine -match "-m\s+realtime_stt"
}
if ($existing) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class DictationWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@
    $window = Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -eq "Local Dictation" } |
        Select-Object -First 1
    if ($window) {
        [DictationWindow]::ShowWindow($window.MainWindowHandle, 9) | Out-Null
        [DictationWindow]::SetForegroundWindow($window.MainWindowHandle) | Out-Null
    }
    exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$startArgs = @{
    FilePath = $Pythonw
    ArgumentList = @("-m", "realtime_stt")
    WorkingDirectory = $ProjectDir
    WindowStyle = "Hidden"
    RedirectStandardOutput = Join-Path $LogDir "stdout.log"
    RedirectStandardError = Join-Path $LogDir "app.log"
}
Start-Process @startArgs
