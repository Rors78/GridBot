# Stop every running GridBot process: the launcher windows (run.bat,
# run_gitbash.sh and launch.bat) that hold the restart loops, gridbot.py,
# its oracle.py scanner, and any Lattice console. Used by launch.bat so the
# GridPick desktop shortcut is a kill-then-launch: a click always ends with
# exactly one fresh bot and no leftover window.
#
# ORDER MATTERS. gridbot.py sits inside run.bat's :loop, which restarts it
# 15 s after a non-zero exit. Killing python first would just schedule a
# respawn, so the launcher cmd goes first, then the python it was minding.
#
# Matching on script names and D:\GridBot paths, never on "python": this
# machine runs ~20 python processes when the fleet is up. gridbot.py is
# started with a RELATIVE name ("python -X utf8 gridbot.py"), so a gridbot.py
# only counts when its parent is one of our launchers or is already gone.
# Our own process tree is never touched, so the launch.bat that calls this
# survives to start the new bot.
#
# A hard kill is safe here: state.json is written tmp + os.replace, the
# instance lock is an OS file lock released on death, and restored grids
# catch up from REST before any exit decision.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File gridbot_stop.ps1 [-DryRun]
param([switch]$DryRun)

$dir = 'D:\GridBot'
$launchers = @("*$dir\run.bat*", "*$dir\launch.bat*", "*run_gitbash.sh*")

# Every line also goes to launcher.log (open/close per line, ASCII: a Tee that
# held the file open blocked launch.bat's own "lattice exited" echo).
function Say($m) {
    Write-Output $m
    try { Add-Content -Path "$dir\launcher.log" -Encoding ascii -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) } catch {}
}

# GRACEFUL FIRST (audit 2026-09-23): the journal showed 18 STARTs and 0 STOPs
# -- every stop was a hard kill, so the bot's final save and STOP row never
# ran. gridbot.py polls for stop.request every 0.5 s and exits with code 0
# (run.bat then ends its loop by itself). Wait up to 12 s, then force the rest.
if (-not $DryRun) {
    $bots = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*.exe' -and $_.CommandLine -like '*gridbot.py*' })
    if ($bots) {
        Set-Content -Path "$dir\stop.request" -Value 'stop' -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(12)
        while ((Get-Date) -lt $deadline) {
            $alive = @($bots | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
            if (-not $alive) { break }
            Start-Sleep -Milliseconds 300
        }
        if ($alive) {
            Say "gridbot_stop: no clean stop within 12 s -- forcing"
        } else {
            Say "gridbot_stop: GridBot stopped cleanly"
        }
    }
}

# GRACEFUL FOR THE CONSOLE TOO. Windows Terminal keeps a tab open when its
# process is killed ("[process exited with code -1]"), so every icon click
# used to leave the previous Lattice window behind. lattice.py polls for
# lattice.stop and exits 0; launch.bat's loop then ends and cmd exits 0,
# which closes the tab. The file is removed afterwards either way so a
# fresh console never quits on a stale request. Belt and braces: the
# GridPick terminal profile also has closeOnExit "always".
if (-not $DryRun) {
    $consoles = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like 'python*.exe' -and $_.CommandLine -like "*$dir\tui\lattice.py*" })
    if ($consoles) {
        Set-Content -Path "$dir\lattice.stop" -Value 'stop' -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(6)
        while ((Get-Date) -lt $deadline) {
            $alive = @($consoles | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
            if (-not $alive) { break }
            Start-Sleep -Milliseconds 200
        }
        if ($alive) { Say "gridbot_stop: Lattice did not quit within 6 s -- forcing" }
        else {
            Say "gridbot_stop: Lattice console closed cleanly"
            # Its launch.bat cmd now ends on its own (exit 0 -> the tab closes).
            # Killing it first would turn that into a non-zero exit. Wait for
            # it; anything still here after 3 s is force-killed below.
            $hosts = @($consoles | ForEach-Object { [int]$_.ParentProcessId } | Select-Object -Unique)
            $deadline = (Get-Date).AddSeconds(3)
            while ((Get-Date) -lt $deadline) {
                $left = @($hosts | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
                if (-not $left) { break }
                Start-Sleep -Milliseconds 200
            }
            Say("gridbot_stop: old console host(s) {0}" -f $(if ($left) { "still alive: $($left -join ',')" } else { "ended on their own" }))
        }
    }
    Remove-Item -Path "$dir\lattice.stop" -Force -ErrorAction SilentlyContinue
}

$all = Get-CimInstance Win32_Process
$byId = @{}
foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }

# Never kill ourselves or anything above us (launch.bat's own cmd).
$protect = @()
$cur = [int]$PID
while ($cur -and $byId.ContainsKey($cur) -and ($protect -notcontains $cur)) {
    $protect += $cur
    $cur = [int]$byId[$cur].ParentProcessId
}

function Matches($p, $patterns) {
    foreach ($pat in $patterns) { if ($p.CommandLine -like $pat) { return $true } }
    return $false
}

$launcherIds = @()
$targets = @()
foreach ($p in $all) {
    if ($protect -contains [int]$p.ProcessId) { continue }
    if (($p.Name -in 'cmd.exe', 'bash.exe', 'sh.exe') -and (Matches $p $launchers)) {
        $targets += $p
        $launcherIds += [int]$p.ProcessId
    }
}
foreach ($p in $all) {
    if ($protect -contains [int]$p.ProcessId) { continue }
    if ($p.Name -notlike 'python*.exe') { continue }
    if ($p.CommandLine -like "*$dir\oracle.py*") { $targets += $p; continue }
    # The Lattice console. Killing launch.bat's cmd does not kill it, and an
    # orphaned console child keeps the old window open.
    if ($p.CommandLine -like "*$dir\tui\lattice.py*") { $targets += $p; continue }
    if ($p.CommandLine -like '*gridbot.py*') {
        $parent = [int]$p.ParentProcessId
        $orphan = -not $byId.ContainsKey($parent)
        if ($orphan -or ($launcherIds -contains $parent) -or ($p.CommandLine -like "*$dir\gridbot.py*")) {
            $targets += $p
        }
    }
}

if (-not $targets) {
    Say "gridbot_stop: nothing running"
    exit 0
}

foreach ($p in $targets) {
    $what = ($p.CommandLine -replace '^.*[\\/]', '') -replace '"', ''
    if ($DryRun) {
        Say("would stop  {0,-10} {1,6}  {2}" -f $p.Name, $p.ProcessId, $what)
        continue
    }
    try {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
        Say("stopped     {0,-10} {1,6}  {2}" -f $p.Name, $p.ProcessId, $what)
    } catch {
        Say("could not stop {0} {1}: {2}" -f $p.Name, $p.ProcessId, $_.Exception.Message)
    }
}
exit 0
