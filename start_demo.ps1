$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

$BundledPython = "C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
    $PythonExe = (Resolve-Path ".\.venv\Scripts\python.exe").Path
} elseif (Test-Path -LiteralPath $BundledPython) {
    $PythonExe = $BundledPython
} else {
    throw "未找到可用的 Python。"
}

& $PythonExe -m streamlit run app.py --server.address 127.0.0.1