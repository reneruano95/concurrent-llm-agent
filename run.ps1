# ─────────────────────────────────────────────────────────────
# run.ps1 — Windows launcher for the multi-agent demo.
#
# Opens Windows Terminal tabs (or fallback PowerShell windows):
#   - Dashboard
#   - Orchestrator
#   - N specialist agents
#
# Usage:
#   .\run.ps1 -Scenario translate -Topic "Hello world"
#   .\run.ps1 -Scenario svg -Topic "Technology and AI" -Tasks 10 -Port 1234
# ─────────────────────────────────────────────────────────────

[CmdletBinding()]
param(
    [string]$Scenario = "translate",
    [string]$Topic    = "Gemma is Google DeepMind most capable open AI model",
    [int]$Port        = 8080,
    [int]$Tasks       = 0
)

$ErrorActionPreference = "Stop"

# Resolve paths
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DemoDir   = Join-Path $ScriptDir "demo"
$Python    = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$ApiUrl    = "http://127.0.0.1:$Port/v1/chat/completions"
$ServerUrl = "http://127.0.0.1:$Port"

if (-not (Test-Path $Python)) {
    Write-Host "❌ .venv not found at $ScriptDir\.venv — run 'uv sync' first" -ForegroundColor Red
    exit 1
}

# ─── Load agents from scenario ──────────────────────────────

$nArg = ""
if ($Tasks -gt 0) { $nArg = ", n_agents=$Tasks" }

$pyCode = @"
import sys
sys.path.insert(0, r'$DemoDir')
from scenarios import get_scenario
s = get_scenario('$Scenario'$nArg)
for a in s['agents']:
    print(f"{a['name']}|{a['emoji']}|{a['color']}")
"@

$tmpPy = [System.IO.Path]::GetTempFileName() + ".py"
Set-Content -Path $tmpPy -Value $pyCode -Encoding UTF8
try {
    $prevPyIO = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = "utf-8"
    $AgentData = & $Python $tmpPy 2>&1
    $env:PYTHONIOENCODING = $prevPyIO
    if ($LASTEXITCODE -ne 0 -or -not $AgentData) {
        Write-Host "❌ Failed to load scenario '$Scenario'" -ForegroundColor Red
        Write-Host $AgentData -ForegroundColor DarkGray
        exit 1
    }
} finally {
    Remove-Item $tmpPy -ErrorAction SilentlyContinue
}

$Agents = @()
foreach ($line in ($AgentData -split "`r?`n")) {
    if (-not $line.Trim()) { continue }
    $parts = $line.Split("|")
    $Agents += [PSCustomObject]@{
        Name  = $parts[0]
        Emoji = $parts[1]
        Color = $parts[2]
    }
}
$NumAgents = $Agents.Count

# ─── Build commands ─────────────────────────────────────────

$tasksFlag = ""
if ($Tasks -gt 0) { $tasksFlag = " --tasks $Tasks" }

# Each command cd's into demo/ and runs the script. After it finishes, keep window open.
function Make-Cmd([string]$inner) {
    # -NoExit keeps the window open after script ends (in case orchestrator's input() is skipped)
    # PYTHONIOENCODING=utf-8 + chcp 65001 ensure emojis don't crash stdout on Windows.
    return "chcp 65001 > `$null; `$env:PYTHONIOENCODING='utf-8'; cd `"$DemoDir`"; & `"$Python`" $inner"
}

$DashCmd = Make-Cmd "dashboard.py --server-url '$ServerUrl' --scenario '$Scenario' --topic '$Topic'$tasksFlag"
$OrchCmd = Make-Cmd "orchestrator.py --scenario '$Scenario' --api-url '$ApiUrl' --topic '$Topic'$tasksFlag"

$SpecCmds = @()
foreach ($a in $Agents) {
    $SpecCmds += [PSCustomObject]@{
        Title = $a.Name
        Cmd   = Make-Cmd "specialist.py --name '$($a.Name)' --emoji '$($a.Emoji)' --color '$($a.Color)' --api-url '$ApiUrl'"
    }
}

# ─── Launch ─────────────────────────────────────────────────

$wt = Get-Command wt.exe -ErrorAction SilentlyContinue

if ($wt) {
    # wt.exe treats ';' as a tab separator. Escape any ';' inside a per-tab
    # command (ANSI color codes like '1;35', and the '; ' between our own
    # inner PowerShell statements) so wt passes them through literally.
    function Escape-WtArg([string]$s) { return $s -replace ';', '\;' }

    # Build the wt argv. Calling wt.exe directly (not Start-Process) lets
    # PowerShell quote each argument correctly, including titles with spaces.
    $wtArgs = @()
    $wtArgs += @("new-tab", "--title", "Dashboard",
                 "powershell", "-NoExit", "-Command", (Escape-WtArg $DashCmd))
    $wtArgs += @(";", "new-tab", "--title", "Orchestrator",
                 "powershell", "-NoExit", "-Command", (Escape-WtArg $OrchCmd))
    foreach ($s in $SpecCmds) {
        $wtArgs += @(";", "new-tab", "--title", $s.Title,
                     "powershell", "-NoExit", "-Command", (Escape-WtArg $s.Cmd))
    }

    & wt.exe @wtArgs
    Write-Host "🚀 Launched in Windows Terminal: $Scenario ($NumAgents agents + dashboard + orchestrator)" -ForegroundColor Green
} else {
    # Fallback: separate PowerShell windows
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $DashCmd
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $OrchCmd
    foreach ($s in $SpecCmds) {
        Start-Process powershell -ArgumentList "-NoExit", "-Command", $s.Cmd
    }
    Write-Host "🚀 Launched $($NumAgents + 2) PowerShell windows ($Scenario)" -ForegroundColor Green
    Write-Host "💡 Tip: install Windows Terminal (wt.exe) for tabbed view." -ForegroundColor DarkGray
}
