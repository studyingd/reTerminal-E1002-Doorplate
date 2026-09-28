# 一键重启 Sensecraft 三服务(脱离终端独立运行,日志写入 logs/)
# 用法:右键"用 PowerShell 运行",或终端里 .\start_services.ps1
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
Remove-Item "$logs\*.log" -Force -ErrorAction SilentlyContinue

# 解析真实 python 可执行文件(WindowsApps 的 stub 可能挂起,绕开它)
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
$candidate = "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\python.exe"
if (Test-Path $candidate) { $py = $candidate }
if (-not $py) { Write-Host "找不到 python"; exit 1 }

# 停掉旧实例,避免端口占用(按命令行特征匹配)
Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -like "*Sensecraft\bridge.py*" -or
  $_.CommandLine -like "*Sensecraft\webapp\server.py*" -or
  $_.CommandLine -like "*http.server 8000*"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Start-Process $py -ArgumentList "-u", "$root\bridge.py" `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\bridge.log" -RedirectStandardError "$logs\bridge.err.log"

Start-Process $py -ArgumentList "-u", "$root\webapp\server.py" `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\webapp.log" -RedirectStandardError "$logs\webapp.err.log"

Start-Process $py -ArgumentList "-m", "http.server", "8000", "--directory", "$root\out" `
  -WorkingDirectory $root -WindowStyle Hidden `
  -RedirectStandardOutput "$logs\http8000.log" -RedirectStandardError "$logs\http8000.err.log"

Start-Sleep -Seconds 3
Write-Host "监听状态:"
netstat -ano | Select-String ":8899.*LISTENING|:8000.*LISTENING"
Write-Host "日志目录:$logs"
