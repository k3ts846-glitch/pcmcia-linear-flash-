<#
.SYNOPSIS
  Диагностика карты PCMCIA по SSH: запускается на ВАШЕМ компьютере (Windows),
  сам заходит на ноутбук, выполняет проверки и приносит отчёт обратно.

.DESCRIPTION
  Делает то же, что remote_collect.sh:
    1) проверяет ssh и файлы набора;
    2) пакует набор во временный .tar.gz и отправляет на ноутбук (в /tmp);
    3) запускает там collect_all.sh (через sudo, если вошли не root);
    4) сохраняет отчёт рядом: otchet-<хост>-<дата>.txt;
    5) убирает за собой временную папку на ноутбуке.

  Пароль нигде не сохраняется: его спрашивает сам ssh, а при входе обычным
  пользователем - ещё и sudo на ноутбуке.
  Отправка (шаг 2) делается ОДИН раз: в Windows ssh спрашивает пароль только
  для каждой новой команды, поэтому весь запуск уложен в один сеанс.

.PARAMETER HostName
  Адрес ноутбука. По умолчанию 192.168.0.94.
  Внимание: имя параметра - HostName, потому что $Host в PowerShell
  зарезервирован (это имя компьютера, а не адрес).

.PARAMETER User
  Пользователь SSH. По умолчанию root.

.PARAMETER Port
  Порт SSH. По умолчанию 22.

.PARAMETER KitDir
  Папка набора (где лежат collect_all.sh и pcmcia_direct.py).
  По умолчанию - папка, где лежит этот скрипт.

.PARAMETER Image
  После диагностики снять образ карты в /root/card.bin (только если паспорт
  карты прочитан).

.PARAMETER PullImage
  Вместе с -Image забрать снятый образ к себе (scp).

.PARAMETER NoSlow
  Без --selftest и --diag (быстрее).

.PARAMETER DryRun
  Ничего не подключать: только показать, что было бы выполнено.

.EXAMPLE
  .\remote_collect.ps1
.EXAMPLE
  .\remote_collect.ps1 -User alex -HostName 192.168.0.94 -Image -PullImage
.EXAMPLE
  .\remote_collect.ps1 -DryRun
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
  [switch]$DryRun,   # ничего не делать, показать план
  [string]$KeyFile = "",  # ключ вместо пароля: по scp/ssh не нужно вводить пароль
  [switch]$PlanB,    # записать на ноутбуке снятие проверки окон (нужна перезагрузка)
  [string]$Mode      = "",   # служебное: planb | reboot | waitreboot
  [int]$BootId       = 0     # служебное: какой загрузки ждём (из /proc/sys/kernel/random/boot_id)
)

$ErrorActionPreference = "Stop"
$script:Fails = 0

# ноутбук печатает UTF-8, консоль Windows по умолчанию читает cp866 ->
# русский текст отчёта выглядел бы мешаниной символов. Включаем UTF-8.
try {
  $utf8 = New-Object System.Text.UTF8Encoding($false)
  [Console]::OutputEncoding = $utf8
  $global:OutputEncoding = $utf8
} catch { }

function Say([string]$msg) { Write-Host $msg }
function Fail([string]$msg) { Write-Host ("ОШИБКА: " + $msg) -ForegroundColor Red; $script:Fails++ }

# ---- 0. что нужно --------------------------------------------------------
$ssh = Get-Command ssh -ErrorAction SilentlyContinue
$scp = Get-Command scp -ErrorAction SilentlyContinue
if (-not $ssh -or -not $scp) {
  Fail "нет ssh или scp. Они есть в комплекте Windows 10/11: Параметры ->
       Приложения -> Дополнительные компоненты -> Клиент OpenSSH.
       Либо в PowerShell от администратора:
       Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
  exit 2
}

# ---- 1. набор ------------------------------------------------------------
if (-not $KitDir -or $KitDir -eq "") { $KitDir = $PSScriptRoot }
if (-not $KitDir) { $KitDir = (Get-Location).Path }
$need = @("collect_all.sh", "run_remote.sh", "planb.sh", "pcmcia_direct.py")
foreach ($f in $need) {
  if (-not (Test-Path (Join-Path $KitDir $f))) {
    Fail "в $KitDir нет $f.
       Укажите папку набора: .\remote_collect.ps1 -KitDir 'D:\pcmcia-flash-kit'"
    exit 2
  }
}

# ---- 2. временная папка и архив -----------------------------------------
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("pcmcia-push-" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
  # Версия набора: если в отправляемом pcmcia_direct.py её нет - это старый
  # файл (распаковали новый архив рядом со старой папкой). Лучше сказать сразу.
  $verExpected = "2.16.0"
  $directPath = Join-Path $KitDir "pcmcia_direct.py"
  $verFound = "?"
  if (Test-Path $directPath) {
    $m = Select-String -Path $directPath -Pattern '^KIT_VERSION = "([^"]+)"' -List
    if ($m) { $verFound = $m.Matches[0].Groups[1].Value }
  }
  Say ("  версия набора: $verFound (ожидается $verExpected)")
  if ($verFound -ne $verExpected) {
    Say "  !!! ВНИМАНИЕ: файлы набора устаревшие или чужие."
    Say "      Возьмите новый архив и распакуйте в ЧИСТУЮ папку,"
    Say "      затем запускайте RUN.ps1 из неё."
    Say "      Путь, откуда берутся файлы: $KitDir"
    Say ""
    if (-not $DryRun) { Say "  Останавливаюсь, чтобы не получить отчёт по старому коду."; exit 3 }
  }
  Say ""

  $copy = @("collect_all.sh", "run_remote.sh", "planb.sh", "pcmcia_direct.py", "pcmcia_flash.sh", "pcmcia_gui.py",
            "README_FIRST.txt", "SSH_РАБОТА.txt")
  foreach ($f in $copy) {
    $src = Join-Path $KitDir $f
    if (Test-Path $src) { Copy-Item $src -Destination $stage }
  }
  # подсказка, почему в отчёте может не быть разделов 4-5
  if (-not (Test-Path (Join-Path $stage "pcmcia_flash.sh"))) {
    Say "  ВНИМАНИЕ: рядом нет pcmcia_flash.sh - разделы 4 (--selftest) и"
    Say "  5 (--diag) будут пропущены. Остальное (включая главный раздел 7)"
    Say "  соберётся как обычно."
    Say ""
  }
  # архив НЕ собираем: файлы уходят на ноутбук по scp поштучно (см. шаг 1).
  # tar на ноутбуке не нужен, tar на Windows не нужен.
  $stamp    = Get-Date -Format "yyyyMMdd-HHmmss"
  $outLocal = Join-Path (Get-Location).Path ("otchet-" + $HostName + "-" + $stamp + ".txt")
  $target   = ($User + "@" + $HostName)
  $remoteDir = "/tmp/pcmcia-collect"
  # отчёт кладём ВНУТРЬ временной папки: чужие файлы в /tmp (например от
  # прошлого запуска через sudo) не мешают и ничего не остаётся после уборки
  $remoteRep = "$remoteDir/otchet.txt"

  $extra = ""
  if ($PlanB)  { $extra += " --planb" }
  if ($Image)  { $extra += " --image" }
  if ($NoSlow) { $extra += " --no-slow" }

  # Команда на ноутбуке: файлы уже скопированы scp, поэтому остаётся только
  # запустить run_remote.sh (он сам сделает sudo, отчёт и уборку). Нарочно
  # короткая и БЕЗ кавычек, >, ||, $ - так её не исказит ни cmd.exe, ни sh.
  # В stdin ssh ничего не идёт: все данные переданы отдельным scp.
  switch ($Mode) {
    "planb"      { $remoteCmd = "bash $remoteDir/run_remote.sh --planb" }
    "reboot"     { $remoteCmd = "sync; (sleep 1; reboot) >/dev/null 2>&1 & echo 'команда перезагрузки отправлена'" }
    "waitreboot" { $remoteCmd = "id" }   # отметка boot_id снимается ДО перезагрузки
    default      { $remoteCmd = "bash $remoteDir/run_remote.sh$extra" }
  }

  # ВНИМАНИЕ: у ssh порт задаётся -p, а у scp -P (заглавная). Если передать
  # scp ключ -p, он примет номер порта за имя файла ("stat local 22: ...").
  # Это ловилось живым прогоном на sshd: поэтому наборы ключей разные.
  $common = @(
    "-o", "ConnectTimeout=8",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=15"
  )
  $sshOpts = @("-p", "$Port") + $common
  $scpOpts = @("-P", "$Port") + $common
  # ключ вместо пароля: тогда ни один шаг не спрашивает пароль
  # ключ не указан, но он уже создан add_key.ps1 - используем его молча
  if (-not $KeyFile -or $KeyFile.Trim().Length -eq 0) {
    $defKey = ""
    if ($env:USERPROFILE) { $defKey = Join-Path $env:USERPROFILE ".ssh\pcmcia_key" }
    if ($defKey -and (Test-Path $defKey)) {
      $KeyFile = $defKey
      Say ("  ключ найден автоматически: " + $KeyFile)
    }
  }
  $keyOk = $false
  if ($KeyFile -and $KeyFile.Trim().Length -gt 0) {
    if (Test-Path $KeyFile) {
      $sshOpts += @("-i", $KeyFile, "-o", "IdentitiesOnly=yes")
      $scpOpts += @("-i", $KeyFile, "-o", "IdentitiesOnly=yes")
      $keyOk = $true
    } else {
      Say ("  !!! ключ не найден: " + $KeyFile + " - работаю с паролем")
    }
  }

  Say "=== ЗАПУСК ДИАГНОСТИКИ ПО SSH ==="
  $thisHost = $env:COMPUTERNAME
  if (-not $thisHost) { $thisHost = [System.Net.Dns]::GetHostName() }
  Say ("  с этой машины : " + $thisHost + ", " + (Get-Date -Format "yyyy-MM-dd HH:mm"))
  Say ("  ноутбук       : " + $target + " (порт " + $Port + ")")
  Say ("  набор         : " + $KitDir)
  Say ("  отчёт придёт в: " + $outLocal)
  Say ""

  if ($DryRun) {
    Say "--DryRun: реальных подключений не делаю. Что было бы выполнено:"
    Say ""
    Say (" 1) копирование набора на ноутбук (scp, файл за файлом):")
    Say ("     scp " + ($scpOpts -join " ") + " <файлы набора> " + $target + ":" + $remoteDir + "/")
    Say ("     файлы: " + ((Get-ChildItem $stage | Select-Object -ExpandProperty Name) -join " "))
    Say ("  2) запуск проверок (ssh):")
    Say ("     ssh " + ($sshOpts -join " ") + " " + $target + " " + $remoteCmd)
    Say ("  3) отчёт забирается обратно (scp):")
    Say ("     scp " + ($scpOpts -join " ") + " " + $target + ":" + $remoteRep + " " + $outLocal)
    if ($PullImage) {
      Say ("  4) образ забирается обратно (scp):")
      Say ("     scp " + ($scpOpts -join " ") + " " + $target + ":/root/card.bin card-" + $HostName + "-" + $stamp + ".bin")
    }
    Say ""
    Say "  проверка готовности:"
    Say ("    ssh  : " + $ssh.Source)
    Say ("    scp  : " + $scp.Source)
    Say ("    файлов для отправки: " + (Get-ChildItem $stage).Count)
    exit 0
  }

  Say "ВАЖНО: запускать в обычном окне PowerShell, не в PowerShell ISE -"
  Say "в ISE ssh не сможет спросить пароль с клавиатуры."
  Say ""
  if ($keyOk) {
    Say ("Вход по ключу: " + $KeyFile + " - пароль спрашивать не буду.")
  } else {
    Say ("Сейчас ssh попросит пароль пользователя $User на $HostName.")
    Say "Файлы копируются по scp, поэтому пароль вводится на каждом шаге:"
    Say "  создать папку, скопировать файлы, запуск, забрать отчёт, уборка."
    Say "Это не ошибка. Хотите один раз или совсем без пароля - см. ниже."
    Say "Пароли нигде не сохраняются и в файлы не попадают."
  }
  Say ""
  Say "Чтобы пароль не вводить каждый раз (по желанию):"
  Say "  добавьте ключ доступа одной командой с этой машины:"
  Say ("       .\\add_key.ps1 -HostName " + $HostName + " -User " + $User)
  Say "  (пароль спросится ОДИН раз, дальше весь набор работает без пароля;"
  Say "   после этого запуск можно делать так:"
  Say ("       .\RUN.ps1 -HostName " + $HostName + " -KeyFile " + "$env:USERPROFILE\\.ssh\\pcmcia_key)")
  Say ""

  # Приложение должно быть .exe: в Windows PowerShell 5.1 "& $obj" на команде,
  # найденной как .ps1-обёртка, даёт "не является приложением".
  $sshExe = $ssh.Source
  if ($sshExe -and $sshExe -notmatch '\.exe$') {
    $exe = Join-Path (Split-Path $sshExe) "ssh.exe"
    if (Test-Path $exe) { $sshExe = $exe }
  }
  $scpExe = $scp.Source
  if ($scpExe -and $scpExe -notmatch '\.exe$') {
    $exe = Join-Path (Split-Path $scpExe) "scp.exe"
    if (Test-Path $exe) { $scpExe = $exe }
  }
  # Чтобы отказ входа не выглядел как авария PowerShell: stderr команды - это
  # обычный текст. Разбираем его сами и говорим по-русски, что делать.
  $ErrorActionPreference = "Continue"

  # --- 1) скопировать файлы набора на ноутбук: scp, файл за файлом ----------
  # Файл за файлом (а не одним архивом) - чтобы путь к папке на ноутбуке
  # точно существовал и не зависел от кавычек cmd.exe. tar на Windows нужен
  # только для scp каталога целиком, здесь он не используется.
  Say "--- шаг 1: проверяю вход и копирую набор (scp) ---"
  $errFile = $outLocal + ".err"
  $sshErr = ""
  # Одна проверка входа: если пароль/пользователь не подошли - сразу стоп, а не
  # ещё три запроса пароля на scp, запуск и забор отчёта.
  & $sshExe @sshOpts $target ("rm -rf " + $remoteDir + "; mkdir -p " + $remoteDir) 2> $errFile
  if ($LASTEXITCODE -ne 0) { $rc = $LASTEXITCODE } else { $rc = 0 }
  if ($rc -eq 0) {
    $srcFiles = Get-ChildItem $stage | Select-Object -ExpandProperty FullName
    & $scpExe @scpOpts @srcFiles ($target + ":" + $remoteDir + "/") 2>> $errFile
    $rc = $LASTEXITCODE
  } else {
    $srcFiles = @()
  }
  if ((Test-Path $errFile) -and (Get-Item $errFile).Length -gt 0) {
    $sshErr = [System.IO.File]::ReadAllText($errFile, [System.Text.Encoding]::UTF8)
  }
  if ($rc -ne 0) {
    Say ("  копирование не выполнено (код " + $rc + ") - причины разберу ниже")
  } else {
    Say ("  скопировано файлов: " + $srcFiles.Count)
  }

  # --- 2) запуск проверок (ssh) и забор отчёта (scp) -----------------------
  $rcRun = 0
  if ($rc -eq 0) {
    Say ""
    Say "--- шаг 2: запускаю проверки на ноутбуке (ssh) ---"
    & $sshExe @sshOpts $target $remoteCmd
    $rcRun = $LASTEXITCODE
    $rc = $rcRun

    Say ""
    Say "--- шаг 3: забираю отчёт (scp) ---"
    $errFile = $outLocal + ".err"
    & $scpExe @scpOpts ($target + ":" + $remoteRep) $outLocal 2> $errFile
    $rcScp = $LASTEXITCODE
    if ($rcScp -ne 0) {
      # причину не затираем: если это был отказ входа, разбор ниже должен её видеть
      if ((Test-Path $errFile) -and -not $sshErr.ToLower().Contains("permission denied")) {
        $sshErr = [System.IO.File]::ReadAllText($errFile, [System.Text.Encoding]::UTF8)
      }
      Remove-Item -Force $errFile -ErrorAction SilentlyContinue
      Say "  отчёт забрать не удалось - смотрите сообщения ssh/scp ниже"
    } else {
      Remove-Item -Force $errFile -ErrorAction SilentlyContinue
      Say ("  отчёт получен: " + $outLocal)
    }

    # --- 4) убрать за собой: на ноутбуке остаётся только образ (если снимали)
    Say ""
    Say "--- шаг 4: убираю набор с ноутбука ---"
    if ($rcScp -eq 0) {
      & $sshExe @sshOpts $target ("rm -rf " + $remoteDir)
      if ($LASTEXITCODE -eq 0) { Say "  убрано: $remoteDir" }
      else { Say ("  убрать не удалось (код " + $LASTEXITCODE + ") - не страшно: папка в /tmp") }
    } else {
      Say "  (набор оставлен: отчёт ещё не забран - повторите запуск или разберите вручную)"
    }
  }

  # сообщения scp/ssh (предупреждения, отказ в доступе и т.п.) уже собраны
  $errFile = $outLocal + ".err"
  if ((Test-Path $errFile) -and (Get-Item $errFile).Length -gt 0) {
    $sshErr = [System.IO.File]::ReadAllText($errFile, [System.Text.Encoding]::UTF8)
    Remove-Item -Force $errFile -ErrorAction SilentlyContinue
  }
  if ($sshErr.Trim().Length -gt 0) {
    Say ""
    Say "  --- сообщения ssh/scp ---"
    Say $sshErr.Trim()
  }

  if (-not (Test-Path $outLocal) -or (Get-Item $outLocal).Length -eq 0) {
    if (Test-Path $outLocal) { Remove-Item -Force $outLocal -ErrorAction SilentlyContinue }
    Say ""
    Say "=== НЕ УДАЛОСЬ (код $rc) ==="
    $e = $sshErr.ToLower()
    # Windows-клиент печатает "Permission denied, please try again." по-английски,
    # даже когда остальной вывод русский; иногда текст приходит без причины -
    # тогда считаем это отказом входа (раньше всего вероятнее).
    if ($e -match "permission denied" -or $e.Trim().Length -eq 0) { $e = "permission denied " + $e }
    $fixRoot = "  На ноутбуке (одна команда):`n" +
               "    printf 'PermitRootLogin yes\nPasswordAuthentication yes\n' | sudo tee /etc/ssh/sshd_config.d/99-root.conf && sudo systemctl restart ssh"
    if ($e -match "connection refused") {
      Say "  Что случилось: на этом порту никто не принимает подключения."
      Say "  Это значит: sshd не запущен ИЛИ слушает другой порт."
      Say "  На ноутбуке (одна команда):"
      Say "    sudo ss -ltnp | grep sshd ; sudo sshd -T | grep -i '^port '"
      Say "  Если порт другой - запустите с ним:  -Port НОМЕР"
    }
    elseif ($e -match "timed out|no route to host|network is unreachable|host is down") {
      Say "  Что случилось: до ноутбука нет сети (пакеты не доходят)."
      Say ("  Проверьте:  ping " + $HostName)
      Say "  Частое: разные сети (гостевой Wi-Fi), включённый файрвол на ноутбуке,"
      Say "  или адрес изменился - на ноутбуке:  hostname -I"
    }
    elseif ($e -match "permission denied") {
      Say "  Что случилось: ноутбук отклонил вход. Пароль мог быть неверным,"
      Say "  либо вход под этим пользователем/root запрещён в настройках sshd."
      Say "  Разбор по порядку:"
      Say "   1) опечатка в пароле или Caps Lock: запустите ещё раз и введите"
      Say "      аккуратно (пароль не отображается - это нормально);"
      Say "   2) вход root запрещён (обычное дело на Debian/Ubuntu, даже если"
      Say "      пароль верный). Проверить на ноутбуке:"
      Say "        sudo sshd -T | grep -i permitrootlogin"
      Say "      Разрешить:"
      Say $fixRoot
      Say "   3) если не хотите входить под root - заходите своим пользователем"
      Say "      и разрешите ему sudo без пароля на ноутбуке (одна команда):"
      Say "        echo 'ВАШ_ЛОГИН ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/pcmcia"
      Say "      затем:  .\RUN.ps1 -HostName $HostName -User ВАШ_ЛОГИН"
      Say ""
      Say "  Ничего не изменено: на карту не писали, на ноутбуке файлы не тронуты."
    }
    elseif ($e -match "host key verification failed|remote host identification has changed") {
      Say "  Что случилось: у ssh сохранён старый ключ этого адреса."
      Say ("  Выполните:  ssh-keygen -R " + $HostName)
    }
    elseif ($e -match "too many authentication failures") {
      Say "  Что случилось: ssh перебрал слишком много ключей до пароля."
      Say "  Запустите с ключом -o IdentitiesOnly=yes (или удалите лишние ключи из агента)."
    }
    elseif ($e -match "kex_exchange_identification|connection (closed|reset) by") {
      Say "  Что случилось: порт открыт, но сервер обрывает соединение."
      Say "  Смотрите на ноутбуке:  sudo journalctl -u ssh -n 30 --no-pager"
      Say "  Частое: AllowUsers/DenyUsers, fail2ban, tcpwrappers."
    }
    elseif ($e -match "could not resolve hostname|name or service not known") {
      Say "  Что случилось: адрес не похож на IP и не находится по имени."
      Say ("  Укажите IP ноутбука:  -HostName " + $HostName)
    }
    else {
      Say '  Разбор не подошёл - пришлите блок "сообщения ssh (stderr)" выше.'
      Say ("  Проверка сети:  Test-NetConnection " + $HostName + " -Port " + $Port)
    }
    Say ""
    Say "  Если SSH не нужен вообще - отчёт можно снять прямо на ноутбуке:"
    Say "    sudo bash ./collect_all.sh"
    exit 2
  }

  # отчёт бывает в UTF-8 и с \r от ssh - приводим к нормальному виду
  $text = [System.IO.File]::ReadAllText($outLocal, [System.Text.Encoding]::UTF8)
  $text = $text -replace "`r", ""
  $idx = $text.IndexOf("ОТЧЁТ ПО КАРТЕ PCMCIA")
  if ($idx -gt 0) { $text = $text.Substring($idx) }
  [System.IO.File]::WriteAllText($outLocal, $text, (New-Object System.Text.UTF8Encoding($false)))

  Say ""
  Say "=== ГОТОВО ==="
  Say ("  отчёт: " + $outLocal + "  (" + (($text -split "`n").Count) + " строк)")
  Say ""
  Say "  Пришлите этот файл в переписку. Главное место - раздел"
  Say '  "7. ПАСПОРТ КАРТЫ": есть кортежи/производитель = карта жива,'
  Say "  сплошные 0xFF/0x00 = карта или контакт слота не отвечают."
  Say ""

  # ---- что делать по итогу отчёта (без догадок со стороны) --------------
  # ВАЖНО: прогон с -PlanB - это НЕ отчёт о карте: в нём нет раздела 7.
  # Поэтому сначала отдельно разбираем план Б, иначе получаются советы
  # "паспорт не прочитан" там, где паспорт и не читался.
  $isPlanbRun = $text -match "ПЛАН Б: снимаю проверку"
  $cisOk    = $text -match "ИТОГ РАЗДЕЛА 7: паспорт прочитан: ДА"
  $cisPart  = $text -match "ИТОГ РАЗДЕЛА 7: паспорт прочитан: ЧАСТИЧНО"
  $blankFf  = $text -match "attribute memory = сплошные 0xFF"
  $noHigh   = $text -match "no high memory space available"
  $noSysfs  = $text -match "в sysfs нет файла окна"
  $strict   = $text -match "Operation not permitted"
  $planbCmd = $text -match "iomem=relaxed в cmdline *: ДА"
  # готовность определяем по факту в grub.cfg, а не по строке вердикта
  $planbReady = ($text -match "iomem=relaxed ЕСТЬ") -or
                ($text -match "ЗАГРУЗКА ГОТОВА")

  if ($isPlanbRun) {
    if ($planbReady) {
      Say "=== ПЛАН Б ПРИМЕНЁН, ЗАГРУЗКА ГОТОВА ==="
      Say "  iomem=relaxed уже в /boot/grub/grub.cfg - запись сделана правильно."
      Say "  Дальше нужна ОДНА перезагрузка ноутбука С КАРТОЙ В СЛОТЕ:"
      Say "    .\RUN.ps1 -HostName $HostName -PlanB -Reboot"
      Say "  (или вручную: ssh $target, затем  sudo reboot)"
      Say "  После перезагрузки скрипт снимет отчёт и сам проверит, что план Б"
      Say "  активен (в разделе 1.1 будет "iomem=relaxed: ДА")."
    }
    else {
      Say "=== ПЛАН Б НЕ ДОВЕДЁН ДО КОНЦА - НЕ ПЕРЕЗАГРУЖАЙТЕСЬ ==="
      Say "  Параметр не попал в меню загрузки, поэтому перезагрузка ничего"
      Say "  не включит. Причина - в отчёте выше (раздел 2 блока плана Б)."
      if ($text -match "нет /etc/default/grub") {
        Say "  Тут: на ноутбуке нет /etc/default/grub - значит грузит не grub"
        Say "  (или другой загрузчик). Пришлите отчёт, подберём способ."
      }
      elseif ($text -match "ОШИБКА: нет ни update-grub") {
        Say "  Тут: нет ни update-grub, ни grub-mkconfig - меню не пересобрать."
        Say "  Пришлите отчёт, подберём способ."
      }
      else {
        Say "  Пришлите этот отчёт - в нём видно, что именно не сработало."
      }
    }
  }
  elseif ($cisPart) {
    Say "=== КАРТА ОТВЕЧАЕТ: ПАСПОРТ ВИДЕН ЧАСТИЧНО ==="
    Say "  Attribute memory читается, строки паспорта найдены, но цепочку"
    Say "  кортежей собрать не удалось. Это НЕ молчащая карта: обмен идёт."
    Say "  Пришлите раздел 7 - по байтам подберём сдвиг/ширину точно."
  }
  elseif ($cisOk) {
    Say "=== ПАСПОРТ ПРОЧИТАН ==="
    Say "  Карта отвечает, паспорт получен напрямую из attribute memory."
    Say "  Следующий шаг - дать ядру принять карту (план Б), если ещё не:"
    Say "    .\RUN.ps1 -HostName $HostName -PlanB"
  }
  elseif ($blankFf) {
    Say "=== КАРТА НЕ ОТВЕЧАЕТ ==="
    Say "  attribute memory = сплошные 0xFF: на шине нет ответа карты."
    Say "  Порядок действий (от простого к сложному):"
    Say "    1) вынуть и вставить карту до щелчка;"
    Say "    2) протереть контакты ластиком;"
    Say "    3) вставить другую 16-битную карту - если и она 0xFF, дело в слоте;"
    Say "    4) только после этого обсуждать драйверы и прошивку."
  }
  elseif ($planbCmd -and -not $cisOk) {
    Say "=== ПЛАН Б АКТИВЕН, НО ПАСПОРТ НЕ ПРОЧИТАН ==="
    Say "  Ядро больше не мешает (iomem=relaxed действует), и всё равно"
    Say "  кортежей CIS нет. Смотрите раздел 7 отчёта - там причина."
  }
  elseif ($planbWritten -and -not $planbActive) {
    Say "=== ПЛАН Б ЗАПИСАН ЧАСТИЧНО ==="
    Say "  Файл probe_mem=0 на ноутбуке есть, но параметр ядра не действует."
    Say "  Прогоните план Б этим архивом (2.6.3): он правит /etc/default/grub,"
    Say "  вызывает update-grub и ПРОВЕРЯЕТ, попал ли параметр в загрузку:"
    Say "    .\RUN.ps1 -HostName $HostName -PlanB"
  }
  elseif ($text -match "план Б НЕ применён") {
    Say "=== СНАЧАЛА ПЛАН Б ==="
    Say "  iomem=relaxed не действует, поэтому прямой доступ к attribute"
    Say "  memory закрыт. Без этого раздел 7 прочитать нельзя:"
    Say "    .\RUN.ps1 -HostName $HostName -PlanB"
  }
  elseif ($noHigh -or $noSysfs -or $strict) {
    Say "=== ЯДРО НЕ ПРИНЯЛО КАРТУ: ПРОВЕРКА ОКОН ПАМЯТИ ==="
    Say "  Нужен план Б (окна без проверки + прямой доступ к /dev/mem):"
    Say "    .\RUN.ps1 -HostName $HostName -PlanB"
  }
  else {
    Say "=== ПАСПОРТ НЕ ПРОЧИТАН ==="
    Say "  Раздел 7 сформирован, но кортежей CIS в attribute memory нет."
    Say "  Пришлите отчёт - разберу по строкам."
  }

  # ---- 3. забрать образ (по желанию) -----------------------------------
  if ($PullImage) {
    if (-not $Image) {
      Say ""
      Say "--- образ не снимался: нужен ключ -Image (снять и забрать) ---"
    }
    $imgRemote = "/root/card.bin"
    $imgLocal  = Join-Path (Get-Location).Path ("card-" + $HostName + "-" + $stamp + ".bin")
    Say ""
    Say "--- забираю образ карты с ноутбука ---"
    $scp = Get-Command scp -ErrorAction SilentlyContinue
    if ($scp) {
      & scp -P $Port -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new ($target + ":" + $imgRemote) $imgLocal
      if ($LASTEXITCODE -eq 0) {
        Say ("    образ: " + $imgLocal + " (" + (Get-Item $imgLocal).Length + " байт)")
        Say ("    md5  : " + (Get-FileHash -Algorithm MD5 -Path $imgLocal).Hash.ToLower())
      } else {
        Say ("    не удалось забрать " + $imgRemote + " - он остался на ноутбуке")
      }
    } else {
      Say "    (нет scp) образ остался на ноутбуке: /root/card.bin"
    }
  }
  exit 0
}
finally {
  Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
  if ($archive -and (Test-Path $archive)) { Remove-Item -Force $archive -ErrorAction SilentlyContinue }
}
