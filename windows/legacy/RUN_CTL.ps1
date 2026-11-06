<#
  RUN_CTL.ps1 - ПУЛЬТ УПРАВЛЕНИЯ КАРТОЙ С WINDOWS-МАШИНЫ.

  Запускается ЗДЕСЬ (второй ноутбук), а работает с картой на первом:
  файлы набора копируются туда по scp, команда запускается по ssh,
  результат и образ возвращаются обратно тоже scp. Ничего не
  устанавливается, tar не нужен, вручную по ssh ходить не надо.

  ЗАПУСК (обычное окно PowerShell, не ISE):

      .\RUN_CTL.ps1                            # меню
      .\RUN_CTL.ps1 info                       # паспорт и вердикт
      .\RUN_CTL.ps1 read                       # снять образ и забрать к себе
      .\RUN_CTL.ps1 read -RemotePath /root/my.bin
      .\RUN_CTL.ps1 write -Image .\card.bin    # отправить образ и записать
      .\RUN_CTL.ps1 erase                      # стереть карту (спросит YES)
      .\RUN_CTL.ps1 verify -Image .\card.bin   # сверить файл с картой
      .\RUN_CTL.ps1 canwrite                   # можно ли писать эту карту
      .\RUN_CTL.ps1 diag                       # окна памяти, драйвер, dmesg
      .\RUN_CTL.ps1 report                     # полный отчёт (как RUN.ps1)
      .\RUN_CTL.ps1 chip                       # что за микросхема (не пишет)
      .\RUN_CTL.ps1 selftest                   # проверить набор (карта не нужна)

  Общие ключи (подходят к любой команде):
      -HostName 192.168.0.94   -User root   -Port 22
      -KeyFile $env:USERPROFILE\.ssh\pcmcia_key   (сначала .\add_key.ps1)
      -Yes       не спрашивать на ноутбуке YES (erase/write)
      -DryRun    показать, что было бы выполнено, и ничего не делать

  ГДЕ ЧТО ЛЕЖИТ:
      образ на ноутбуке: /root/card-ДАТА.bin (или -RemotePath)
      образ у вас:       .\card-<адрес>-ДАТА.bin в текущей папке
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet("menu","info","read","write","erase","verify","canwrite","diag","report","fwprobe","fwwrite","chip","selftest")]
  [string]$Action = "menu",
  [string]$HostName = "192.168.0.94",
  [string]$User     = "root",
  [int]   $Port     = 22,
  [string]$KeyFile  = "",
  [string]$Image    = "",        # локальный файл образа (write/verify)
  [string]$RemotePath = "",      # путь на ноутбуке (read/write)
  [switch]$Yes,                  # не спрашивать подтверждение на ноутбуке
  [switch]$DryRun,
  [switch]$Full,                  # для selftest: полный прогон, а не быстрый
  [string]$KitDir   = ""
)

$ErrorActionPreference = "Continue"      # ssh/scp пишут в stderr - это не авария

# Рядом лежит kit_sync.ps1: выбор «какие файлы набора отправить». Он отдельным
# файлом, потому что его проверяют тесты - без сети, но на настоящем коде.
$syncLib = Join-Path $PSScriptRoot "kit_sync.ps1"
if (Test-Path $syncLib) { . $syncLib }
$stateLib = Join-Path $PSScriptRoot "kit_state.ps1"
if (Test-Path $stateLib) { . $stateLib }

# КОДИРОВКА ВЫВОДА. Ноутбук печатает UTF-8, а консоль Windows по умолчанию
# читает вывод программ как cp866 - и тогда вместо русского текста видна
# мешанина из символов псевдографики (так и случилось на живой машине).
# Переключаем и вывод консоли, и разбор вывода внешних программ на UTF-8.
function Set-Utf8Console {
  try {
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8
    $global:OutputEncoding  = $utf8
    return $true
  } catch {
    Write-Host ("  (не удалось включить UTF-8 для консоли: " + $_.Exception.Message + ")")
    return $false
  }
}
$null = Set-Utf8Console
$script:RemoteDir = "/tmp/pcmcia-ctl"

function Say([string]$m)  { Write-Host $m }
function Head([string]$m) { Write-Host ""; Write-Host ("=== " + $m + " ===") -ForegroundColor Cyan }
function Bad([string]$m)  { Write-Host ("ОШИБКА: " + $m) -ForegroundColor Red }

function ExeOf($cmd, [string]$name) {
  $p = $cmd.Source
  if ($p -and $p -notmatch '\.exe$') {
    $c = Join-Path (Split-Path $p) ($name + ".exe")
    if (Test-Path $c) { return $c }
  }
  return $p
}

# ---- 0. ssh и scp ---------------------------------------------------------
$ssh = Get-Command ssh -ErrorAction SilentlyContinue
$scp = Get-Command scp -ErrorAction SilentlyContinue
if (-not $ssh -or -not $scp) {
  Bad "нет ssh или scp. Они встроены в Windows 10/11. От администратора:
     Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
  exit 2
}
$sshExe = ExeOf $ssh "ssh"
$scpExe = ExeOf $scp "scp"

# ---- 1. где набор ---------------------------------------------------------
if (-not $KitDir -or $KitDir -eq "") { $KitDir = $PSScriptRoot }
if (-not $KitDir) { $KitDir = (Get-Location).Path }
$need = @("pcmcia_ctl.sh", "pcmcia_direct.py", "collect_all.sh", "run_remote.sh")
foreach ($f in $need) {
  if (-not (Test-Path (Join-Path $KitDir $f))) {
    Bad "в $KitDir нет $f. Укажите папку набора: .\RUN_CTL.ps1 -KitDir 'D:\pcmcia-flash-kit'"
    exit 2
  }
}

$verFound = "?"
$m = Select-String -Path (Join-Path $KitDir "pcmcia_direct.py") -Pattern '^KIT_VERSION = "([^"]+)"' -List
if ($m) { $verFound = $m.Matches[0].Groups[1].Value }

# ---- 2. опции ssh/scp -----------------------------------------------------
# у ssh порт -p, у scp -P: если перепутать, scp примет номер порта за файл
# Гигиена ssh: агент и X11 не пробрасываем (нам они не нужны, а лишний
# проброс - лишняя дверь). ControlMaster: пять шагов идут к одной машине -
# держим одно соединение вместо пяти (и пароль при работе без ключа
# спрашивается один раз на шаг, а не на каждое новое TCP-соединение).
$ctlPath = ""
if ($env:USERPROFILE) { $ctlPath = Join-Path $env:USERPROFILE ".ssh\pcmcia-ctl-%C" }
$common = @("-o","ConnectTimeout=8","-o","StrictHostKeyChecking=accept-new",
            "-o","ServerAliveInterval=15","-o","NumberOfPasswordPrompts=1",
            "-o","ForwardAgent=no","-o","ForwardX11=no")
if ($ctlPath) {
  $common += @("-o","ControlMaster=auto","-o",("ControlPath=" + $ctlPath),
               "-o","ControlPersist=60")
}
$sshOpts = @("-p","$Port") + $common
$scpOpts = @("-P","$Port") + $common
# Ключ не указан? Если он уже создан add_key.ps1 - берём его сами, чтобы не
# печатать -KeyFile каждый раз (и не вводить пароль по шагам).
if (-not $KeyFile -or $KeyFile.Trim().Length -eq 0) {
  if ($env:USERPROFILE) {
    $defKey = Join-Path $env:USERPROFILE ".ssh\pcmcia_key"
    if (Test-Path $defKey) { $KeyFile = $defKey }
  }
}

$keyOk = $false
if ($KeyFile -and $KeyFile.Trim().Length -gt 0) {
  if (Test-Path $KeyFile) {
    $sshOpts += @("-i", $KeyFile, "-o", "IdentitiesOnly=yes")
    $scpOpts += @("-i", $KeyFile, "-o", "IdentitiesOnly=yes")
    $keyOk = $true
  } else {
    $defKey2 = ""
    if ($env:USERPROFILE) { $defKey2 = Join-Path $env:USERPROFILE ".ssh\pcmcia_key" }
    if ($defKey2 -and (Test-Path $defKey2) -and ($defKey2 -ne $KeyFile)) {
      Say ("  указанный ключ не найден: " + $KeyFile + " - беру обычный: " + $defKey2)
      $KeyFile = $defKey2
      $sshOpts += @("-i", $KeyFile, "-o", "IdentitiesOnly=yes")
      $scpOpts += @("-i", $KeyFile, "-o", "IdentitiesOnly=yes")
      $keyOk = $true
    } else {
      Say ("  !!! ключ не найден: " + $KeyFile + " - работаю с паролем")
    }
  }
}
$target = $User + "@" + $HostName

# ---- 3. разбор того, что ответил ssh --------------------------------------
# Отказ входа поднимает «аварию» PowerShell (NativeCommandError), если
# ErrorActionPreference=Stop и вывод пишется в общий поток. Поэтому: Continue,
# текст разбираем сами и печатаем по-русски один раз.
function Explain($text) {
  if ($text -match "Permission denied") {
    Say ""
    Say "  ВХОД НЕ ВЫПОЛНЕН: неверный пароль или запрещён вход для $User."
    Say "  1) пароль - проверьте раскладку и Caps Lock;"
    Say "  2) вход root - на ноутбуке: grep -i permitrootlogin /etc/ssh/sshd_config"
    Say "  3) вход своим пользователем - .\RUN_CTL.ps1 -User имя (нужен sudo без пароля);"
    Say "  4) без пароля вообще - .\add_key.ps1 -HostName $HostName"
    Say "  Ничего не изменено: файлы не копировались, на карту не писали."
    return $true
  }
  if ($text -match "Connection refused|No route to host|timed out|Could not resolve") {
    Say ""
    Say "  НОУТБУК НЕ ОТВЕЧАЕТ по $HostName порт $Port."
    Say "  Проверьте: включён ли ноутбук, тот ли адрес (на ноутбуке: hostname -I),"
    Say "  работает ли sshd (sudo systemctl status ssh), не блокирует ли файрвол."
    return $true
  }
  if ($text -match "subsystem request failed|sftp") {
    Say ""
    Say "  НА НОУТБУКЕ ВЫКЛЮЧЕН sftp - а новые scp работают через него."
    Say "  Набор уже попробовал старый протокол (scp -O). Если и он не прошёл,"
    Say "  включите sftp на ноутбуке одной строкой:"
    Say "    echo 'Subsystem sftp /usr/lib/openssh/sftp-server' | sudo tee -a /etc/ssh/sshd_config"
    Say "    sudo systemctl restart ssh"
    Say "  Ничего не изменено: файлы не копировались, на карту не писали."
    return $true
  }
  if ($text -match "Host key verification failed") {
    Say ""
    Say "  КЛЮЧ ХОСТА ИЗМЕНИЛСЯ (переустановка системы на ноутбуке)."
    Say ("  Удалите старую запись: ssh-keygen -R " + $HostName)
    return $true
  }
  return $false
}

function RunSsh([string]$cmd) {
  $out = & $sshExe @sshOpts $target $cmd 2>&1
  $rc = $LASTEXITCODE
  $text = ($out | Out-String)
  return @{ rc = $rc; out = $out; text = $text }
}

# Выполнение на ноутбуке С ПОКАЗОМ ХОДА РАБОТЫ.
# Зачем: чтение образа идёт секунды-десятки секунд, а обычный вызов ждёт конца
# и не печатает ничего - со стороны это неотличимо от зависания. Здесь вывод
# ssh читается по мере поступления (и stdout, и stderr) и сразу печатается.
# Строкой считается и кусок, закрытый возвратом каретки: так печатаются
# проценты, которые pcmcia_direct.py перерисовывает на месте.
# Возвращаемое значение то же, что у RunSsh: rc + весь текст (для проверок).
function QuoteArg([string]$a) {
  if ($null -eq $a) { return '""' }
  $a = [string]$a
  if ($a.Length -eq 0) { return '""' }
  if ($a -match '[\s"]') { return '"' + ($a -replace '"', '\"') + '"' }
  return $a
}

function RunSshLive([string]$cmd) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $sshExe
  $psi.Arguments = ((@($sshOpts) + @($target) + @($cmd)) | ForEach-Object { QuoteArg $_ }) -join " "
  $psi.UseShellExecute = $false          # без этого перенаправление вывода не работает
  $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $proc = [System.Diagnostics.Process]::Start($psi)

  $enc = New-Object System.Text.UTF8Encoding($false)
  $decOut = $enc.GetDecoder()
  $decErr = $enc.GetDecoder()
  $bufO = New-Object byte[] 8192
  $bufE = New-Object byte[] 8192
  $textO = ""                             # накопленный текст (для проверок)
  $textE = ""
  $pendO = ""                             # хвост без перевода строки
  $pendE = ""
  $lastPct = @(-1)                        # проценты печатаем на каждый новый %
                                          # (массив, чтобы менять из Flush)
  $taskO = $proc.StandardOutput.BaseStream.ReadAsync($bufO, 0, $bufO.Length)
  $taskE = $proc.StandardError.BaseStream.ReadAsync($bufE, 0, $bufE.Length)
  $openO = $true
  $openE = $true

  # печатаем всё, что закрыто переводом строки или возвратом каретки
  function Flush([string]$pend, [ref]$text) {
    $last = -1
    for ($i = $pend.Length - 1; $i -ge 0; $i--) {
      if ($pend[$i] -eq "`n" -or $pend[$i] -eq "`r") { $last = $i; break }
    }
    if ($last -lt 0) { return $pend }
    $whole = $pend.Substring(0, $last + 1)
    $rest = $pend.Substring($last + 1)
    $text.Value += $whole
    $shown = ($whole -replace "`r`n", "`n") -replace "`r", "`n"
    foreach ($ln in ($shown -split "`n")) {
      if ($ln.Trim().Length -eq 0) { continue }
      # строки-проценты (" 91%  58.60 МиБ"): печатаем только при смене
      # процента, иначе на 64 МиБ экран забивается сотнями одинаковых строк
      if ($ln -match '^\s*(\d{1,3})%') {
        $pct = [int]$Matches[1]
        if ($pct -eq $lastPct[0]) { continue }
        $lastPct[0] = $pct
      }
      Write-Host $ln
    }
    return $rest
  }

  try {
    while ($openO -or $openE -or (-not $proc.HasExited)) {
      if ($openO) {
        if ($taskO.Wait(120)) {
          $n = $taskO.Result
          if ($n -le 0) { $openO = $false }
          else {
            $chars = New-Object char[] ($decOut.GetCharCount($bufO, 0, $n))
            $null = $decOut.GetChars($bufO, 0, $n, $chars, 0)
            $pendO += (-join $chars)
            $pendO = Flush $pendO ([ref]$textO)
            $taskO = $proc.StandardOutput.BaseStream.ReadAsync($bufO, 0, $bufO.Length)
          }
        }
      }
      if ($openE) {
        if ($taskE.Wait(120)) {
          $n = $taskE.Result
          if ($n -le 0) { $openE = $false }
          else {
            $chars = New-Object char[] ($decErr.GetCharCount($bufE, 0, $n))
            $null = $decErr.GetChars($bufE, 0, $n, $chars, 0)
            $pendE += (-join $chars)
            $pendE = Flush $pendE ([ref]$textE)
            $taskE = $proc.StandardError.BaseStream.ReadAsync($bufE, 0, $bufE.Length)
          }
        }
      }
      if ($proc.HasExited -and (-not $openO) -and (-not $openE)) { break }
      if ($proc.HasExited) { Start-Sleep -Milliseconds 60 }
    }
    $proc.WaitForExit()
    if ($pendO.Trim().Length -gt 0) { Write-Host $pendO.TrimEnd() }
    if ($pendE.Trim().Length -gt 0) { Write-Host $pendE.TrimEnd() }
    $textO += $pendO
    $textE += $pendE
    return @{ rc = $proc.ExitCode; out = @($textO, $textE); text = ($textO + "`n" + $textE) }
  } finally {
    if (-not $proc.HasExited) { try { $proc.Kill() } catch { } }
  }
}

function RunScp([string[]]$files, [string]$dest) {
  $out = & $scpExe @scpOpts @files $dest 2>&1
  $rc = $LASTEXITCODE
  $text = ($out | Out-String)
  # Новые scp ходят через «Subsystem sftp». Если на ноутбуке sftp выключен,
  # достаточно попросить старый протокол (-O), а не править sshd_config на
  # чужой машине. Меняем конфиг сервера только если и это не помогло.
  if ($rc -ne 0 -and ($text -match "subsystem request failed|sftp|not a valid command")) {
    Say "  scp не смог через sftp - пробую старый протокол (-O)"
    $out2 = & $scpExe "-O" @scpOpts @files $dest 2>&1
    $rc2 = $LASTEXITCODE
    $text2 = ($out2 | Out-String)
    if ($rc2 -eq 0) {
      Say "  получилось по старому протоколу (sftp на ноутбуке не нужен)"
    }
    return @{ rc = $rc2; text = $text2 }
  }
  return @{ rc = $rc; text = $text }
}

# ---- 4. что именно запускать ---------------------------------------------
function BuildRemote([string]$a, [string]$rp) {
  switch ($a) {
    "info"     { return "bash $($script:RemoteDir)/pcmcia_ctl.sh info --json" }
    "canwrite" { return "bash $($script:RemoteDir)/pcmcia_ctl.sh canwrite --json" }
    "diag"     { return "bash $($script:RemoteDir)/pcmcia_ctl.sh diag" }
    "read"     {
      if (-not $rp) { $rp = "/root/card-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".bin" }
      return "bash $($script:RemoteDir)/pcmcia_ctl.sh read '$rp'"
    }
    "write"    {
      if (-not $rp) { $rp = "/root/card-write.bin" }
      $yes = if ($Yes) { " --yes" } else { "" }
      return "bash $($script:RemoteDir)/pcmcia_ctl.sh write '$rp'$yes"
    }
    "erase"    {
      $yes = if ($Yes) { " --yes" } else { "" }
      return "bash $($script:RemoteDir)/pcmcia_ctl.sh erase$yes"
    }
    "verify"   {
      if (-not $rp) { $rp = "/root/card-write.bin" }
      return "bash $($script:RemoteDir)/pcmcia_ctl.sh verify '$rp'"
    }
    "report"   { return "bash $($script:RemoteDir)/run_remote.sh" }
    # запись МИМО ядра, командами JEDEC через окно моста (с 2.14.0).
    # Это отдельные действия: старое "write" работает через /dev/mtdN.
    "fwprobe"  {
      # подтверждение тоже уходит через -Yes: по ssh терминала нет, и без него
      # пульт честно отвечал «отменено» — проба просто не запускалась
      $yes = if ($Yes) { " --yes" } else { "" }
      return "bash $($script:RemoteDir)/pcmcia_ctl.sh fwprobe$yes"
    }
    "fwwrite"  {
      if (-not $rp) { $rp = "/root/card-write.bin" }
      $yes = if ($Yes) { " --yes" } else { "" }
      return "bash $($script:RemoteDir)/pcmcia_ctl.sh fwwrite '$rp'$yes"
    }
    # что за микросхема в карте: сектор, протокол, число чипов, напряжение.
    # Только опрос (команды 90h/98h - чтение), ничего не стирается.
    "chip"     { return "bash $($script:RemoteDir)/pcmcia_ctl.sh chip" }
    # проверка самого набора на ноутбуке (карта не нужна): быстрый режим по
    # умолчанию, полный - ключом -Full
    "selftest" {
      $fast = if ($Full) { "" } else { " --fast" }
      return "bash $($script:RemoteDir)/pcmcia_ctl.sh selftest$fast"
    }
    default    { return "" }
  }
}

# ---- 5. меню --------------------------------------------------------------
function ShowMenu() {
  Say ""
  Say "================================================================"
  Say "  ПУЛЬТ УПРАВЛЕНИЯ КАРТОЙ (Windows -> ноутбук, всё по ssh/scp)"
  Say "================================================================"
  Say ("  ноутбук: " + $target + " порт " + $Port + "   набор: " + $verFound)
  Say ("  вход:   " + $(if ($keyOk) { "по ключу " + $KeyFile } else { "по паролю (на каждом шаге)" }))
  Show-CardState
  Say ""
  Say "   1) Паспорт и вердикт (что можно, что нельзя)"
  Say "   2) Снять образ карты и забрать к себе"
  Say "   3) Записать образ на карту (файл с этой машины)"
  Say "   4) Стереть карту"
  Say "   5) Сверить файл с картой (без записи)"
  Say "   6) Можно ли писать именно эту карту"
  Say "   7) Диагностика: окна памяти, драйвер, dmesg"
  Say "   8) Полный отчёт (как RUN.ps1, все разделы)"
  Say "   9) ПРОБА записи БЕЗ драйвера: один сектор, данные возвращаются"
  Say "  10) Записать образ БЕЗ драйвера (файл с этой машины)"
  Say "  11) Что за микросхема в карте (тип, сектор, протокол, напряжение)"
  Say "  12) Проверить сам набор (тесты; карта не нужна, ~30 с)"
  Say "   0) Выход"
  Say ""
  $c = Read-Host "  Пункт"
  return $c
}

if ($Action -eq "menu") {
  $c = ShowMenu
  switch ($c) {
    "1" { $Action = "info" }
    "2" { $Action = "read" }
    "3" { $Action = "write" }
    "4" { $Action = "erase" }
    "5" { $Action = "verify" }
    "6" { $Action = "canwrite" }
    "7" { $Action = "diag" }
    "8" { $Action = "report" }
    "9" { $Action = "fwprobe" }
    "10" { $Action = "fwwrite" }
    "11" { $Action = "chip" }
    "12" { $Action = "selftest" }
    default { Say "  ничего не выбрано - выхожу"; exit 0 }
  }
}

# для write/verify нужен локальный файл
if (($Action -eq "write" -or $Action -eq "verify" -or $Action -eq "fwwrite") -and -not $Image) {
  $img = Read-Host "  Путь к файлу-образу на этой машине"
  if ($img) { $Image = $img.Trim('"') }
}
if (($Action -eq "write" -or $Action -eq "verify" -or $Action -eq "fwwrite")) {
  if (-not $Image -or -not (Test-Path $Image)) {
    Bad "файл образа не найден: '$Image'"
    exit 2
  }
  if (-not $RemotePath) {
    $RemotePath = "/root/" + (Split-Path $Image -Leaf)
  }
}
if ($Action -eq "read" -and -not $RemotePath) {
  $RemotePath = "/root/card-" + (Get-Date -Format "yyyyMMdd-HHmm") + ".bin"
}

$remoteCmd = BuildRemote $Action $RemotePath

Head "ЧТО БУДЕТ СДЕЛАНО"
Say ("  команда: " + $Action)
Say ("  ноутбук: " + $target + " (порт " + $Port + ")")
Say ("  папка:   " + $script:RemoteDir)
Say ("  запуск:  " + $remoteCmd)
if ($Action -eq "read" -or $Action -eq "write" -or $Action -eq "verify") {
  Say ("  файл на ноутбуке: " + $RemotePath)
}
if ($DryRun) {
  Say ""
  Say "  --DryRun: ничего не подключаю. Было бы выполнено:"
  Say ("    ssh " + ($sshOpts -join " ") + " " + $target + " 'mkdir -p " + $script:RemoteDir + "'")
  Say ("    scp " + ($scpOpts -join " ") + " <файлы набора> " + $target + ":" + $script:RemoteDir + "/")
  Say ("    ssh " + ($sshOpts -join " ") + " " + $target + " '" + $remoteCmd + "'")
  if ($Action -eq "read") {
    Say ("    scp " + ($scpOpts -join " ") + " " + $target + ":" + $RemotePath + " .\card-" + $HostName + ".bin")
  }
  if ($Action -eq "write" -or $Action -eq "verify") {
    Say ("    scp " + ($scpOpts -join " ") + " " + $Image + " " + $target + ":" + $RemotePath)
  }
  Say ""
  $keyTxt = if ($keyOk) { "есть" } else { "нет" }
  Say ("  проверка: ssh=" + $sshExe + "  scp=" + $scpExe + "  ключ=" + $keyTxt)
  exit 0
}

if ($keyOk) {
  Say ""
  Say ("  Вход по ключу: " + $KeyFile + " - пароль спрашивать не буду.")
} else {
  Say ""
  Say "  Пароль спрашивается на КАЖДОМ шаге (scp/ssh без ключа) - это нормально."
  Say "  Чтобы вводить один раз: .\add_key.ps1 -HostName $HostName"
}
Say ""

# ---- 6. шаг 1: папка на ноутбуке -----------------------------------------
Head "ШАГ 1/5: папка на ноутбуке (ssh)"
$r = RunSsh ("mkdir -p " + $script:RemoteDir)
if ($r.rc -ne 0) {
  if (-not (Explain $r.text)) { Say $r.text }
  exit 1
}
Say ("  готово: " + $script:RemoteDir)

# ---- 7. шаг 2: файлы набора по scp (только изменившиеся) -----------------
# Раньше набор копировался ЦЕЛИКОМ при каждом запуске: 6 файлов и полминуты
# на каждой итерации, хотя меняется обычно один. Теперь сравниваем md5 с тем,
# что уже лежит на ноутбуке, и отправляем только разницу.
Head "ШАГ 2/5: синхронизирую набор (scp, только изменившееся)"
$files = @()
# Что живёт на ноутбуке. kit_sync.ps1, kit_state.ps1 и справочник остаются на
# Windows: это части пульта, а не рабочие файлы Debian-машины.
$kitFiles = @("pcmcia_ctl.sh","pcmcia_direct.py","collect_all.sh","run_remote.sh","planb.sh","pcmcia_flash.sh")
$localMd5 = @{}
foreach ($f in $kitFiles) {
  $p = Join-Path $KitDir $f
  if (Test-Path $p) {
    $localMd5[$f] = (Get-FileHash -Algorithm MD5 -Path $p).Hash.ToLower()
  }
}
$remoteMd5 = @{}
$r = RunSsh ("cd '" + $script:RemoteDir + "' 2>/dev/null && md5sum " + ($kitFiles -join " ") + " 2>/dev/null")
if ($r.rc -eq 0) {
  $remoteMd5 = Read-Md5List ($r.text -split "`n")
} else {
  Say "  (состояние на ноутбуке не прочиталось - отправлю все файлы)"
}
foreach ($f in (Compare-KitFiles -Local $localMd5 -Remote $remoteMd5)) {
  $files += (Join-Path $KitDir $f)
}
if ($files.Count -eq 0) {
  Say "  набор на ноутбуке уже актуален - копировать нечего"
} else {
  $r = RunScp $files ($target + ":" + $script:RemoteDir + "/")
  if ($r.rc -ne 0) {
    if (-not (Explain $r.text)) { Say $r.text }
    Say "  файлы не скопированы - дальше идти нельзя, на карту ничего не писали."
    exit 1
  }
  Say ("  отправлено файлов: " + $files.Count + " из " + $localMd5.Count +
       " (остальные уже были там и совпали по md5)")
}

# ---- 8. шаг 3: образ ВВЕРХ (только для write/verify) ---------------------
if ($Action -eq "write" -or $Action -eq "verify" -or $Action -eq "fwwrite") {
  Head "ШАГ 3/5: отправляю образ на ноутбук (scp)"
  $r = RunScp @($Image) ($target + ":" + $RemotePath)
  if ($r.rc -ne 0) {
    if (-not (Explain $r.text)) { Say $r.text }
    Say "  образ не отправлен - на карту ничего не писали."
    exit 1
  }
  $md5 = (Get-FileHash -Algorithm MD5 -Path $Image).Hash.ToLower()
  Say ("  отправлено: " + (Split-Path $Image -Leaf) + "  md5 " + $md5)
  # сверяем, что на ноутбуке лежит именно этот файл: запись не того файла -
  # самая дорогая ошибка из всех, что тут возможны
  $r = RunSsh ("md5sum '" + $RemotePath + "'")
  if ($r.rc -eq 0) {
    $txt = $r.text.Trim()
    if ($txt -match '^([0-9a-f]{32})') {
      if ($Matches[1] -eq $md5) { Say "  md5 на ноутбуке совпал - файл тот самый" }
      else {
        Bad ("md5 на ноутбуке ДРУГОЙ: " + $Matches[1] + " вместо " + $md5)
        Say "  Запись отменена, чтобы не залить испорченный файл."
        exit 1
      }
    }
  } else { Say "  (md5 на ноутбуке проверить не удалось - продолжаю, но это стоит знать)" }
} else {
  Head "ШАГ 3/5: файл образа не нужен для этой команды"
  Say "  пропущено"
}

# ---- 9. шаг 4: выполнение по ssh -----------------------------------------
Head "ШАГ 4/5: работаю с картой (ssh)"
if ($Action -eq "read") {
  Say "  чтение образа: идёт вживую, проценты ниже (4 МиБ - это несколько секунд..."
  Say "  ...десятков секунд; если проценты растут, набор работает, а не висит)"
  Say "  образ читается ДВАЖДЫ и сверяется: время будет примерно вдвое больше"
}
$r = RunSshLive $remoteCmd
$rc = $r.rc
$text = $r.text

# отказ в sudo из-за пароля - отдельный, понятный случай
if ($text -match "НУЖЕН ПАРОЛЬ SUDO" -or $text -match "sudo: a password is required") {
  Say ""
  Bad "работа требует root, а sudo просит пароль (его нельзя ввести без терминала)"
  Say "  Выберите одно:"
  Say ("   1) .\RUN_CTL.ps1 -User root " + $Action)
  Say "   2) на ноутбуке разрешить sudo без пароля:"
  Say "        echo '$USER ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/pcmcia"
  Say ("   3) сделать это прямо на ноутбуке: sudo bash ./pcmcia_ctl.sh " + $Action)
  exit 3
}

if ($Action -eq "report") {
  # отчёт приезжает отдельным scp из временной папки
  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $localRep = Join-Path (Get-Location).Path ("otchet-" + $HostName + "-" + $stamp + ".txt")
  $r2 = RunScp @(($target + ":" + $script:RemoteDir + "/otchet.txt")) $localRep
  if ($r2.rc -eq 0) {
    Say ""
    Say ("  отчёт получен: " + $localRep)
    if (Test-Path $localRep) {
      Say ""
      Say "---- главное из отчёта ----"
      Select-String -Path $localRep -Pattern "ИТОГ РАЗДЕЛА 7|версия набора|паспорт|ВЕРДИКТ" |
        ForEach-Object { Say ("  " + $_.Line.Trim()) }
    }
  } else {
    Bad "отчёт не забрался"
    $rc = 1
  }
}
if ($Action -eq "info" -or $Action -eq "chip") { Save-CardState $text }

# Вывод шага 4 уже напечатан по ходу (RunSshLive), повторять его здесь не надо:
# второй раз он выглядел как «команда выполнилась дважды».

# ---- 10. шаг 5: образ ОБРАТНО (только для read) --------------------------
if ($Action -eq "read") {
  if ($text -match "образ не создан" -or $text -match "ОШИБКА паспорт не читается") {
    Say ""
    Bad "образ не создан на ноутбуке - забирать нечего"
    Say "  Причина в выводе выше. Чаще всего: карта отвечает нестабильно"
    Say "  (два чтения различаются) или паспорт не читается."
    exit 2
  }
  Head "ШАГ 5/5: забираю образ к себе (scp)"
  $leaf = Split-Path $RemotePath -Leaf
  $local = Join-Path (Get-Location).Path ("card-" + $HostName + "-" + $leaf)
  $r = RunScp @(($target + ":" + $RemotePath)) $local
  if ($r.rc -ne 0) {
    if (-not (Explain $r.text)) { Say $r.text }
    Bad "образ остался на ноутбуке: $RemotePath (забрать вручную по scp)"
    exit 1
  }
  Say ("  образ у вас: " + $local)
  if (Test-Path $local) {
    $md5l = (Get-FileHash -Algorithm MD5 -Path $local).Hash.ToLower()
    $size = (Get-Item $local).Length
    Say ("  размер: " + $size + " байт   md5: " + $md5l)
    if ($text -match "md5 ([0-9a-f]{32})") {
      if ($Matches[1] -eq $md5l) { Say "  md5 совпал с ноутбуком - файл дошёл целым" }
      else { Bad ("md5 НЕ совпал: на ноутбуке " + $Matches[1] + ", у вас " + $md5l) }
    }
  }
} else {
  Head "ШАГ 5/5: забирать файл не нужно"
  Say "  пропущено"
}

# ---- 11. уборка -----------------------------------------------------------
# Закрываем общее соединение (ControlMaster): иначе окно выглядит «занятым»
# ещё минуту после конца работы.
$null = & $sshExe @sshOpts "-O" "exit" $target 2>&1

# Убираем набор с ноутбука, но НЕ образ в /root: его могли не забрать.
$null = RunSsh ("cd /; find " + $script:RemoteDir + " -mindepth 1 -maxdepth 1 ! -name 'otchet.txt' -exec rm -rf {} + 2>/dev/null")
Say ""
Say "  убрано с ноутбука: $($script:RemoteDir) (образ в /root не тронут)"

exit $rc
