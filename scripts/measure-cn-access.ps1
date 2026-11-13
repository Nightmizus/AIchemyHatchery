# Run on a mainland Windows client. No proxy, credentials, or write requests.
param([int]$Samples = 5)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$urls = @(
    'https://aichemy.club/hatchery',
    'https://hatchery.aichemy.club/',
    'https://aichemy.club/script.js',
    'https://aichemy.club/api/auth/me'
)
foreach ($url in $urls) {
    for ($i = 0; $i -lt $Samples; $i++) {
        $raw = & curl.exe --noproxy '*' --compressed -sS -L --max-time 30 -o NUL -w '%{json}' $url
        $result = $raw | ConvertFrom-Json
        $result | Select-Object url, http_code, exitcode, remote_ip, proxy_used, `
            time_namelookup, time_connect, time_appconnect, time_starttransfer, `
            time_total, size_download, num_redirects | ConvertTo-Json -Compress
    }
}
