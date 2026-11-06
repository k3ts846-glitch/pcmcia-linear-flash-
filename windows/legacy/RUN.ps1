<#
  RUN.ps1 - запускается НА WINDOWS-МАШИНЕ (второй ноутбук).
  Сам заходит по SSH на ноутбук с картой, отправляет туда файлы набора,
  запускает проверки и приносит отчёт обратно.

  Ничего устанавливать не нужно: ssh и scp встроены в Windows 10/11.
  Файлы набора уходят на ноутбук по scp, отчёт (и образ) возвращаются тоже scp.
  На ноутбуке ssh-сервер уже есть - этот скрипт его не трогает.

  ЗАПУСК (в обычном окне PowerShell, не в ISE):
      .\RUN.ps1                                  # 192.168.0.94, root, порт 22
      .\RUN.ps1 -HostName 192.168.0.71           # адрес ноутбука из шага 1
      .\RUN.ps1 -User alex                       # обычный пользователь (нужен sudo)
      .\RUN.ps1 -Port 2222
      .\RUN.ps1 -Image -PullImage                 # снять образ карты и забрать
      .\RUN.ps1 -DryRun                           # проверить, ничего не подключая
      .\RUN.ps1 -KeyFile $env:USERPROFILE\.ssh\pcmcia_key
                                                  # без пароля (сначала add_key.ps1)
      .\RUN.ps1 -PlanB                            # план Б: снять проверку окон
                                                  # (нужна перезагрузка ноутбука)
#>
[CmdletBinding()]
param(
  [string]$HostName = "192.168.0.94",
  [string]$User     = "root",
  [int]   $Port     = 22,
  [string]$KitDir   = "",
  [switch]$Image,
  [switch]$PullImage,
  [switch]$NoSlow,
  [switch]$DryRun,
  [switch]$PlanB,
  [switch]$Reboot,        # после -PlanB сразу перезагрузить ноутбук и дождаться
  [int]$WaitSec = 180,    # сколько секунд ждать возвращения ноутбука (по умолчанию 3 мин)
  [string]$KeyFile = "",  # ключ вместо пароля (см. add_key.ps1)
  # КОМАНДЫ ПУЛЬТА (те же, что у RUN_CTL.ps1) - можно писать одной строкой:
  #   .\RUN.ps1 info | read | write | erase | verify | canwrite | diag | report
  [Parameter(Position = 0)]
  [ValidateSet("menu","info","read","write","erase","verify","canwrite","diag","report")]
  [string]$Action = "report",
  [string]$ImageFile = "" # локальный файл образа для write/verify
)

$ErrorActionPreference = "Stop"

# Нужны ssh (запуск команд на ноутбуке) и scp (копирование файлов туда и
# обратно). tar больше не нужен: набор копируется файл за файлом по scp.
foreach ($need in @("ssh","scp")) {
  if (-not (Get-Command $need -ErrorAction SilentlyContinue)) {
    Write-Host ("ОШИБКА: нет " + $need + ". Установите клиент OpenSSH - в PowerShell от администратора:") -ForegroundColor Red
    Write-Host "  Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
    exit 2
  }
}

# Движок: remote_collect.ps1 рядом и его копия в for-windows. Брать "первую
# попавшуюся" нельзя: в распакованной поверх старой папке копия в for-windows
# оказывается от прошлой версии, и набор останавливается с "файлы устарели"
# (именно это случилось на живой машине). Поэтому берём свежайший по дате.
$cand = @()
foreach ($p in @((Join-Path $PSScriptRoot "remote_collect.ps1"),
                 (Join-Path $PSScriptRoot "for-windows\remote_collect.ps1"))) {
  if (Test-Path $p) { $cand += (Get-Item $p) }
}
if ($cand.Count -eq 0) {
  Write-Host "ОШИБКА: рядом нет remote_collect.ps1 (это движок запуска)." -ForegroundColor Red
  Write-Host "  Убедитесь, что архив распакован целиком, а не только этот файл."
  exit 2
}
$engine = ($cand | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
if (-not (Test-Path $engine)) {
  Write-Host "ОШИБКА: рядом нет remote_collect.ps1 (это движок запуска)." -ForegroundColor Red
  Write-Host "  Убедитесь, что архив распакован целиком, а не только этот файл."
  exit 2
}
if (-not $KitDir) { $KitDir = $PSScriptRoot }
$kit = Join-Path $PSScriptRoot "for-windows"

# Именованные аргументы собираются в ХЭШ-ТАБЛИЦУ, а передаются переменной
# (@args2). Так PowerShell подставляет имена. Если написать & $engine @{ ... }
# литералом - он передаст САМУ таблицу как позиционный аргумент, и получится
# "-HostName System.Collections.Hashtable" (это уже случалось на живой машине).
# КОМАНДЫ ПУЛЬТА: info / read / write / erase / verify / canwrite / diag
# уходят в RUN_CTL.ps1 (он возит файлы по scp и запускает pcmcia_ctl.sh).
# report - это обычный полный отчёт через движок remote_collect.ps1.
$ctlActions = @("info","read","write","erase","verify","canwrite","diag")
if ($ctlActions -contains $Action) {
  $ctl = Join-Path $PSScriptRoot "RUN_CTL.ps1"
  if (-not (Test-Path $ctl)) {
    Write-Host ("ОШИБКА: для команды " + $Action + " нужен RUN_CTL.ps1 рядом.") -ForegroundColor Red
    Write-Host "  Полный отчёт без него: .\RUN.ps1 report"
    exit 2
  }
  $ctlArgs = @{ Action = $Action; HostName = $HostName; User = $User; Port = $Port }
  if ($KitDir)    { $ctlArgs["KitDir"] = $KitDir }
  if ($KeyFile)   { $ctlArgs["KeyFile"] = $KeyFile }
  if ($DryRun)    { $ctlArgs["DryRun"] = $true }
  if ($NoSlow)    { $ctlArgs["Yes"] = $true }        # -NoSlow исторически = без вопросов
  if ($ImageFile) { $ctlArgs["Image"] = $ImageFile } # в RUN_CTL.ps1 это файл образа
  & $ctl @ctlArgs
  exit $LASTEXITCODE
}

$args2 = @{ HostName = $HostName; User = $User; Port = $Port }
if ($Image)     { $args2["Image"] = $true }
if ($PullImage) { $args2["PullImage"] = $true }
if ($NoSlow)    { $args2["NoSlow"] = $true }
if ($DryRun)    { $args2["DryRun"] = $true }
if ($PlanB)     { $args2["PlanB"] = $true }
if ($KeyFile)   { $args2["KeyFile"] = $KeyFile }

& $engine @args2
$rc = $LASTEXITCODE

# ---- -Reboot: перезагрузить ноутбук и дождаться, пока он вернётся ----------
# Делается только вместе с -PlanB: без записанных настроек перезагрузка
# ничего не изменит. Ждём по смене загрузки (boot_id), а не по часам, чтобы
# не запутаться в часовых поясах и на медленной загрузке.
if ($Reboot -and $PlanB) {
  Write-Host ""
  Write-Host "=== ПЕРЕЗАГРУЗКА НОУТБУКА ===" -ForegroundColor Cyan
  $bidBefore = & ssh -p $Port -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new ($User + "@" + $HostName) "cat /proc/sys/kernel/random/boot_id" 2>$null
  Write-Host ("  было: " + $bidBefore)
  Write-Host "  отправляю перезагрузку..."
  # ВАЖНО: вызывать движок только явными параметрами: при передаче
  # хэш-таблицы PowerShell подставил её текст вместо адреса
  # ("System.Collections.Hashtable") - это уже случалось на живой машине.
  $null = & $engine -HostName $HostName -User $User -Port $Port -Mode "reboot"
  Start-Sleep -Seconds 5
  $t0 = Get-Date
  $ok = $false
  while (((Get-Date) - $t0).TotalSeconds -lt $WaitSec) {
    $bidNow = & ssh -p $Port -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o BatchMode=yes ($User + "@" + $HostName) "cat /proc/sys/kernel/random/boot_id" 2>$null
    if ($LASTEXITCODE -eq 0 -and $bidNow -and $bidNow.Trim() -ne "" -and $bidNow.Trim() -ne $bidBefore) {
      $ok = $true
      Write-Host ("  ноутбук вернулся, новая загрузка: " + $bidNow)
      Write-Host ("  прошло: " + [int]((Get-Date) - $t0).TotalSeconds + " с")
      break
    }
    Start-Sleep -Seconds 5
  }
  if (-not $ok) {
    Write-Host "  за $WaitSec с ноутбук не вернулся (это нормально при медленной загрузке)." -ForegroundColor Yellow
    Write-Host "  Подождите и запустите обычный отчёт:"
    Write-Host ("    .\RUN.ps1 -HostName " + $HostName)
    exit 3
  }
  Write-Host ""
  Write-Host "=== СНИМАЮ ОТЧЁТ ПОСЛЕ ПЕРЕЗАГРУЗКИ ===" -ForegroundColor Cyan
  & $engine -HostName $HostName -User $User -Port $Port
  exit $LASTEXITCODE
}

exit $rc
