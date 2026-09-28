# PreToolUse hook: deny MACHINE-WIDE process kills, allow PID-scoped ones.
#
# WHY (2026-07-29 incident): runs execute in parallel, one per Maxio site, all on this one
# machine. The task prompt tells the agent to stop its previous app instance before starting
# another -- and an agent in run -006 implemented that as:
#
#     Get-Process -Name dotnet,PublicApi | Stop-Process -Force
#
# which is not scoped to its own workspace. Within 25 seconds it killed the app hosts and
# build/test processes of the three SIBLING runs building alongside it and froze a concurrent
# grading task mid-gate. Three runs (~$30 and ~75 minutes of agent work) were lost. The same
# hazard had already been recorded once before in this project as
# `taskkill /F /IM PublicApi.exe` killing a sibling run's server.
#
# An image name (dotnet.exe, PublicApi.exe, node.exe) carries NO run identity, and neither
# does the app host's command line -- agents launch it as a RELATIVE path
# (`dotnet bin/Debug/net8.0/PublicApi.dll`), so even a command-line exclusion of the run id
# cannot work. The only safe scope is a PID, and the reliable way to get one is from the
# port block this run owns (APP_PORT_BLOCK_BASE .. +APP_PORT_BLOCK_SIZE-1).
#
# Identical for every arm -- tool policy must never differ between arms.
# Keyword filtering is best-effort; the transcript audit remains the backstop.

$raw = [Console]::In.ReadToEnd()
try { $evt = $raw | ConvertFrom-Json } catch { exit 0 }

# Only shell-executing tools can kill a process.
if ($evt.tool_name -ne 'Bash' -and $evt.tool_name -ne 'PowerShell') { exit 0 }

$cmd = [string]$evt.tool_input.command
if (-not $cmd) { exit 0 }

# WHAT MATTERS IS HOW THE PROCESS SET IS SELECTED, NOT HOW THE KILL IS ISSUED.
#
# The first version of this guard exempted anything that resolved a PID, on the theory that a
# PID is inherently scoped. Transcript analysis of run -001 disproved that. It ran:
#
#     Get-CimInstance Win32_Process -Filter "Name='PublicApi.exe'" |
#       ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
#
# which enumerates EVERY PublicApi.exe on the machine and then kills each by PID. Identical
# blast radius to `Stop-Process -Name PublicApi`; the PID is just the delivery mechanism. The
# same run also did `Win32_Process | Where CommandLine -match 'PublicApi' | Stop-Process`,
# which matches sibling runs too because every run launches
# `dotnet run --project src/PublicApi/PublicApi.csproj`.
#
# So the test is: was the SET chosen by image name / command-line pattern (machine-wide), or by
# something that belongs to THIS run (a literal PID it already holds, or a PID resolved from a
# port in its own block)? Allowlist the second, deny the first.
$reason = $null

$killIntent = $cmd -match '(?i)(\bStop-Process\b|\btaskkill\b|\bpkill\b|\bkillall\b)'
if (-not $killIntent) { exit 0 }

# Selection by IMAGE NAME or COMMAND-LINE PATTERN -- machine-wide however the kill is delivered.
$nameSelected =
    ($cmd -match '(?i)\btaskkill\b[^;|\r\n]*[/-]IM\b') -or
    ($cmd -match '(?i)\b(Get-Process|Stop-Process)\b[^;|\r\n]*-(Name|ProcessName)\b') -or
    ($cmd -match '(?i)Name\s*(=|-eq|-match|-like)\s*[''"]') -or
    ($cmd -match '(?i)\b(pkill|killall)\b')

# Any process ENUMERATION feeding a kill (Get-Process / Win32_Process sweeps), which is
# machine-wide unless anchored to this run's own ports.
$enumerates = $cmd -match '(?i)(\bGet-Process\b|Win32_Process|\bGet-WmiObject\b|\bGet-CimInstance\b)'

# The sanctioned anchor: a PID resolved from a port. The run owns APP_PORT_BLOCK_BASE..+SIZE-1.
$portAnchored = ($cmd -match '(?i)Get-NetTCPConnection') -and ($cmd -match '(?i)(LocalPort|OwningProcess)')

if ($nameSelected) {
    $reason = 'the processes to kill are selected by IMAGE NAME or COMMAND-LINE PATTERN, which matches every concurrent run on this machine -- resolving a PID from that set does not narrow it.'
}
elseif ($enumerates -and -not $portAnchored) {
    $reason = 'this enumerates processes machine-wide and pipes them into a kill; nothing ties the set to your run.'
}

if ($reason) {
    $guidance = @"
BLOCKED: $reason

Several runs are building on this machine at the same time, each in its own workspace and on
its own port block. An image name identifies no run, and the app host's command line holds a
relative path, so it cannot be filtered by run id either.

Kill by PID, resolved from the port block YOU own (`APP_PORT_BLOCK_BASE` ..
`APP_PORT_BLOCK_BASE + APP_PORT_BLOCK_SIZE - 1`):

  # PowerShell -- stop only what is listening on YOUR port
  Get-NetTCPConnection -LocalPort <your-port> -State Listen |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id `$_ -Force }

Better still, keep the PID when you start the app (`Start-Process -PassThru`, or `$!` in bash)
and stop that PID. Never kill by image name here.
"@
    $decision = @{
        hookSpecificOutput = @{
            hookEventName            = 'PreToolUse'
            permissionDecision       = 'deny'
            permissionDecisionReason = $guidance
        }
    }
    Write-Output ($decision | ConvertTo-Json -Depth 5 -Compress)
}
exit 0
