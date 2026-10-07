$ErrorActionPreference = 'Stop'
$base = if ($env:WEB_PORT) { "http://127.0.0.1:$env:WEB_PORT" } else { 'http://127.0.0.1:8080' }
$live = Invoke-RestMethod "$base/api/v1/health/live"
$ready = Invoke-RestMethod "$base/api/v1/health/ready"
if ($live.status -ne 'ok' -or $ready.status -ne 'ready') { throw 'Health verification failed.' }
Add-Type -AssemblyName System.Net.Http
$client = [System.Net.Http.HttpClient]::new()
try { $body = $client.GetStringAsync("$base/").GetAwaiter().GetResult() }
finally { $client.Dispose() }
if ($body -notmatch 'id="root"') { throw 'Frontend verification failed.' }
Write-Output "Compose verification passed: $base"
