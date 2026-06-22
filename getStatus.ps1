Write-Host "STATUS: CloudFlare Tunnel Ports..."
Get-Process cloudflared
Get-Service cloudflared

Write-Host "---"
Write-Host "STATUS: Flask Ports..."
(Invoke-WebRequest -Uri "http://127.0.0.1:5000/health" -Method GET -UseBasicParsing).Content
netstat -ano | findstr :5000

Write-Host "---"
Write-Host "STATUS: Activity Log..."
Get-Content -Path remote_shutdown.log | Measure-Object -Line