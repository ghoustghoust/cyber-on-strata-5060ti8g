Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'serve\.server' } |
  ForEach-Object { Write-Host ("killing python PID " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }
Start-Sleep 2
Get-CimInstance Win32_Process -Filter "Name='strata.exe'" |
  ForEach-Object { Write-Host ("killing strata PID " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force }
Write-Host "done"
