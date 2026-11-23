# Registers a Windows scheduled task that starts the bot when you log in.
# Run once:  powershell -ExecutionPolicy Bypass -File install_autostart.ps1
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$action = New-ScheduledTaskAction -Execute "$dir\start_bot.bat" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "MeeshoShopifyBot" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Meesho -> Shopify Telegram bot" -Force
Write-Host "Installed. The bot will start at your next login. Start it now with: Start-ScheduledTask MeeshoShopifyBot"
