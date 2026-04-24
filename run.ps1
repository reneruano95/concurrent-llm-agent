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

$AgentData = & $Python -c $pyCode 2>$null
if (-not $AgentData) {
    Write-Host "❌ Failed to load scenario '$Scenario'" -ForegroundColor Red
    exit 1
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
    return "cd `"$DemoDir`"; & `"$Python`" $inner"
}

$DashCmd = Make-Cmd "dashboard.py --server-url '$ServerUrl' --scenario '$Scenario' --topic `"$Topic`"$tasksFlag"
$OrchCmd = Make-Cmd "orchestrator.py --scenario '$Scenario' --api-url '$ApiUrl' --topic `"$Topic`"$tasksFlag"

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
    # Build a single Windows Terminal command with multiple tabs.
    # First window: dashboard tab; new-tab for orchestrator + each specialist.
    $args = @()
    $args += "new-tab", "--title", "⚡ Dashboard", "powershell", "-NoExit", "-Command", $DashCmd
    $args += ";", "new-tab", "--title", "🧠 Orchestrator", "powershell", "-NoExit", "-Command", $OrchCmd
    foreach ($s in $SpecCmds) {
        $args += ";", "new-tab", "--title", $s.Title, "powershell", "-NoExit", "-Command", $s.Cmd
    }
    Start-Process wt.exe -ArgumentList $args
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
