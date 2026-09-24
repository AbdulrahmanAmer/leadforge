param([string]$Extra = "")
# Runs one `claude -p` exactly like leadforge's agent_runner (plus optional extra flags) and samples
# how many node/claude processes exist above the baseline while it runs.
$base = @(Get-Process node, claude -ErrorAction SilentlyContinue).Count
$args = "-p --model sonnet --output-format text --no-session-persistence $Extra"
$sw = [Diagnostics.Stopwatch]::StartNew()
$p = Start-Process -FilePath "cmd.exe" -ArgumentList "/c echo Reply with the single word OK | claude $args > `"$env:TEMP\probe_out.txt`" 2>&1" -PassThru -WindowStyle Hidden
$peak = 0
while (-not $p.HasExited) {
    $n = @(Get-Process node, claude -ErrorAction SilentlyContinue).Count - $base
    if ($n -gt $peak) { $peak = $n }
    Start-Sleep -Milliseconds 250
}
$sw.Stop()
"flags='$Extra' peak_extra_processes=$peak wall_s=$([math]::Round($sw.Elapsed.TotalSeconds,1)) output=$((Get-Content "$env:TEMP\probe_out.txt" -Raw).Trim())"
