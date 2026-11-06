<#
  add_key.ps1 - один раз настроить вход по ключу с этой машины на ноутбук.
  После этого набор работает БЕЗ пароля: и копирование по scp, и запуск,
  и забор отчёта, и забор образа.

  Пароль спрашивается здесь ОДИН раз - его спросит сам ssh. Никуда не
  сохраняется: в файлы и в командную строку он не попадает.

  ЗАПУСК (в обычном окне PowerShell, не в ISE):
      .\add_key.ps1 -HostName 192.168.0.94
      .\add_key.ps1 -HostName 192.168.0.94 -User root -Port 2222

  Что делает:
    1) если ключа ...\.ssh\pcmcia_key нет - создаёт его (без пароля);
    2) отправляет открытую часть на ноутбук и добавляет в ~/.ssh/authorized_keys
       (пароль спрашивается один раз);
    3) проверяет вход по ключу и печатает готовую строку запуска.

  Ничего не удаляет и не перезаписывает: если ключ уже есть - используется он.
#>
[CmdletBinding()]
param(
  [string]$HostName = "192.168.0.94",
  [string]$User     = "root",
  [int]   $Port     = 22,
  [string]$KeyFile  = ""
)

$ErrorActionPreference = "Continue"   # ssh пишет в stderr - это не авария

function Say([string]$m) { Write-Host $m }

$ssh = Get-Command ssh -ErrorAction SilentlyContinue
$scp = Get-Command scp -ErrorAction SilentlyContinue
$kg  = Get-Command ssh-keygen -ErrorAction SilentlyContinue
if (-not $ssh -or -not $scp -or -not $kg) {
  Say "ОШИБКА: нужны ssh, scp и ssh-keygen (все входят в клиент OpenSSH)."
  Say "  В PowerShell от администратора: Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"
  exit 2
}

# приложение должно быть .exe (в 5.1 "& $obj" на .ps1-обёртке не запускается)
function ExeOf($cmd, [string]$name) {
  $p = $cmd.Source
  if ($p -and $p -notmatch '\.exe$') {
    $c = Join-Path (Split-Path $p) ($name + ".exe")
    if (Test-Path $c) { return $c }
  }
  return $p
}
$sshExe = ExeOf $ssh "ssh"
$scpExe = ExeOf $scp "scp"
$kgExe  = ExeOf $kg  "ssh-keygen"

if (-not $KeyFile -or $KeyFile.Trim().Length -eq 0) {
  $KeyFile = Join-Path $env:USERPROFILE ".ssh\pcmcia_key"
}
$keyDir = Split-Path $KeyFile
if (-not (Test-Path $keyDir)) { New-Item -ItemType Directory -Path $keyDir | Out-Null }

Say "=== ключ доступа к ноутбуку ==="
Say ("  ноутбук : " + $User + "@" + $HostName + " (порт " + $Port + ")")
Say ("  ключ    : " + $KeyFile)

# ВАЖНО (проверено на живой Windows): в PowerShell 5.1 пустой аргумент ("")
# до программы НЕ доезжает. Строка
#     ssh-keygen ... -N "" -C "pcmcia-kit@PC"
# приходит как  ssh-keygen ... -N -C pcmcia-kit@PC  - здесь "-C" становится
# паролем, комментарий пропадает, и ssh-keygen отвечает "Too many arguments".
# Лечится тем, что кавычки передаются как СИМВОЛЫ: '""'. Тогда Win32-OpenSSH
# получает аргумент из двух кавычек, и его CRT превращает их в пустую строку.
# Если и это не сработает - есть ещё два пути: через cmd.exe и тип rsa.
$comment = "pcmcia-kit@" + $env:COMPUTERNAME

function TryKeyGen([string[]]$a, [string]$how) {
  Say ("  попытка: " + $how)
  $out = & $kgExe @a 2>&1
  $rc = $LASTEXITCODE
  if ($rc -eq 0 -and (Test-Path $KeyFile)) { return $true }
  ($out | Out-String).Trim().Split("`n") | Select-Object -First 4 |
    ForEach-Object { if ($_.Trim()) { Say ("    " + $_.Trim()) } }
  return $false
}

$needNew = $true
if (Test-Path $KeyFile) {
  # у ключа может быть пароль (создан раньше вручную) - тогда вход по нему
  # спрашивал бы пароль на каждом шаге. Проверяем пустым паролем.
  $null = & $kgExe -y -P '""' -f $KeyFile 2>&1
  if ($LASTEXITCODE -eq 0) {
    Say "  ключ уже есть, без пароля - новый не создаю (использую этот)"
    $needNew = $false
  } else {
    Say "  ключ есть, но с паролем - пересоздаю без пароля"
    $bak = $KeyFile + ".bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
    Move-Item -Force $KeyFile $bak
    if (Test-Path ($KeyFile + ".pub")) { Move-Item -Force ($KeyFile + ".pub") ($bak + ".pub") }
    Say ("  старый ключ отложен: " + $bak)
  }
}
if ($needNew) {
  Say "  создаю ключ без пароля (он нужен только для этих запусков)"
  $ok = TryKeyGen @("-q", "-t", "ed25519", "-f", $KeyFile, "-N", '""', "-C", $comment) `
                   "ed25519, пустой пароль кавычками-символами"
  if (-not $ok -and (Get-Command cmd -ErrorAction SilentlyContinue)) {
    # cmd.exe не выбрасывает пустые аргументы: там -N "" доедет как "" и станет
    # пустой строкой. Путь в кавычках, чтобы пробелы не ломали команду.
    $inner = 'ssh-keygen -q -t ed25519 -f "' + $KeyFile + '" -N "" -C "' + $comment + '"'
    Say "  попытка: через cmd /c (пустой аргумент не теряется)"
    try {
      $out2 = & cmd /c $inner 2>&1
      if ($LASTEXITCODE -ne 0) {
        ($out2 | Out-String).Trim().Split("`n") | Select-Object -First 3 |
          ForEach-Object { if ($_.Trim()) { Say ("    " + $_.Trim()) } }
      }
    } catch {
      Say ("    cmd не сработал: " + $_.Exception.Message)
    }
    $ok = (Test-Path $KeyFile)
  }
  if (-not $ok) {
    $ok = TryKeyGen @("-q", "-t", "rsa", "-b", "2048", "-f", $KeyFile, "-N", '""', "-C", $comment) `
                     "rsa 2048 (если ed25519 не поддержан)"
  }
  if (-not $ok) {
    Say "ОШИБКА: ssh-keygen не смог создать ключ"
    Say "  Сделайте это одной строкой в этом же окне и запустите меня снова:"
    Say ('    ssh-keygen -q -t ed25519 -f "' + $KeyFile + '" -N ' + "''" + '""' + "''" + ' -C "' + $comment + '"')
    Say "  (кавычки вокруг пустого пароля обязательны: без них PowerShell"
    Say "   выбрасывает аргумент, и ssh-keygen отвечает Too many arguments)"
    Say "  Ключ должен лежать здесь: $KeyFile"
    exit 2
  }
}

$pub = $KeyFile + ".pub"
if (-not (Test-Path $pub)) {
  & $kgExe -y -f $KeyFile | Set-Content -Encoding ASCII $pub
}
$pubText = (Get-Content $pub -Raw).Trim()

$common = @("-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new")
$scpOpts = @("-P", "$Port") + $common          # у scp порт -P, у ssh -p!
$target  = $User + "@" + $HostName

Say ""
Say "Сейчас ssh спросит пароль ОДИН раз - чтобы добавить ключ на ноутбук."
Say "Пароль никуда не сохраняется."
Say ""
Say "КАК ЭТО ДЕЛАЕТСЯ (важно для PowerShell 5.1):"
Say "  команда для ssh собирается БЕЗ кавычек и без <, >, |, $ - иначе"
Say "  PowerShell 5.1 снимает кавычки и grep получает ключ вместо файла."
Say "  Поэтому на ноутбук по scp едут два файла: открытый ключ и маленький"
Say "  скрипт, который аккуратно дописывает его в authorized_keys."

# --- 1) готовим два файла с LF-переводами строк (не CRLF: bash их не любит)
$tmpDir  = Join-Path ([System.IO.Path]::GetTempPath()) ("pcmcia-addkey-" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Path $tmpDir | Out-Null
$pubLocal = Join-Path $tmpDir "pcmcia_pub_key.txt"
$shLocal  = Join-Path $tmpDir "pcmcia_addkey.sh"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

[System.IO.File]::WriteAllText($pubLocal, ($pubText -replace "`r", "") + "`n", $utf8NoBom)

$shText = @'
#!/bin/bash
# дописывает открытый ключ в ~/.ssh/authorized_keys БЕЗ дублей.
# Запускается на ноутбуке: bash pcmcia_addkey.sh
set -u
PUB="$HOME/pcmcia_pub_key.txt"
AK="$HOME/.ssh/authorized_keys"
KEY=$(head -n1 "$PUB" 2>/dev/null | tr -d '\r')
if [ -z "$KEY" ]; then echo "KEY_EMPTY"; rm -f "$PUB" "$0"; exit 1; fi
mkdir -p "$HOME/.ssh" 2>/dev/null && chmod 700 "$HOME/.ssh" 2>/dev/null
touch "$AK" 2>/dev/null && chmod 600 "$AK" 2>/dev/null
if grep -qxF "$KEY" "$AK" 2>/dev/null; then
  echo "KEY_PRESENT"
else
  printf '%s\n' "$KEY" >> "$AK" && echo "KEY_ADDED"
fi
rm -f "$PUB" "$0"
'@ -replace "`r", ""
[System.IO.File]::WriteAllText($shLocal, $shText, $utf8NoBom)

# --- 2) scp: ключ и скрипт (пароль спрашивается здесь)
$rc = 0
$out = & $scpExe @scpOpts @($pubLocal, $shLocal) ($target + ":") 2>&1
$rc = $LASTEXITCODE
if ($rc -ne 0) {
  ($out | Out-String).Trim().Split("`n") | ForEach-Object { if ($_.Trim()) { Say ("  " + $_.Trim()) } }
  Say ""
  Say "=== НЕ УДАЛОСЬ ПЕРЕДАТЬ ФАЙЛЫ НА НОУТБУК (scp, код $rc) ==="
  if (($out | Out-String) -match "Permission denied \(publickey") {
    Say "   * ноутбук не принимает вход ПО ПАРОЛЮ (PasswordAuthentication no)."
    Say "     Один запуск с паролем нужен, чтобы положить ключ. Варианты:"
    Say "       - на ноутбуке разрешить пароль на время:"
    Say "           printf 'PasswordAuthentication yes\n' | sudo tee /etc/ssh/sshd_config.d/99-temp.conf"
    Say "           sudo systemctl restart ssh"
    Say "         (после add_key.ps1 файл 99-temp.conf можно удалить)"
    Say "       - или добавить ключ вручную на самом ноутбуке:"
    Say ('           cat >> ~/.ssh/authorized_keys <<< "' + $pubText + '"')
    Say "  Ничего не изменено: на карту не писали."
    Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
    exit 2
  }
  Say "   * пароль не подошёл (раскладка, Caps Lock) - просто запустите снова;"
  Say "   * вход root запрещён - см. ниже или укажите своего пользователя: -User ЛОГИН;"
  Say "   * ноутбук не отвечает - проверьте адрес и что sshd запущен."
  Say "  Ничего не изменено: на карту не писали."
  Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
  exit 2
}

# --- 3) ssh: без кавычек, без спецсимволов - только запуск скрипта
Say ""
$out = & $sshExe @common -p "$Port" $target "bash pcmcia_addkey.sh" 2>&1
$rc = $LASTEXITCODE
$added = $false
foreach ($l in ($out | Out-String).Split("`n")) {
  $line = $l.TrimEnd()
  if ($line -match "KEY_ADDED")   { Say "  ключ добавлен в ~/.ssh/authorized_keys"; $added = $true }
  if ($line -match "KEY_PRESENT") { Say "  такой ключ там уже был - второй раз не добавляю"; $added = $true }
  if ($line -match "KEY_EMPTY")   { Say "  !!! файл ключа приехал пустым - повторите запуск" }
}
Remove-Item -Recurse -Force $tmpDir -ErrorAction SilentlyContinue
if ($rc -ne 0 -or -not $added) {
  Say ""
  Say "=== НЕ ПОЛУЧИЛОСЬ ДОБАВИТЬ КЛЮЧ (код $rc) ==="
  foreach ($l in ($out | Out-String).Split("`n")) {
    if ($l.Trim()) { Say ("  ноутбук: " + $l.Trim()) }
  }
  Say "  Частые причины:"
  Say "   * вход root запрещён: на ноутбуке  sudo sshd -T | grep -i permitrootlogin"
  Say "     разрешить:"
  Say "       printf 'PermitRootLogin yes\nPasswordAuthentication yes\n' | sudo tee /etc/ssh/sshd_config.d/99-root.conf"
  Say "       sudo systemctl restart ssh"
  Say "   * другой пользователь: .\add_key.ps1 -HostName $HostName -User ВАШ_ЛОГИН"
  Say "   * ручной способ (в окне cmd.exe, не в PowerShell - там кавычки сохраняются):"
  Say '       type "%USERPROFILE%\.ssh\pcmcia_key.pub" | ssh ЗАМЕНИТЕ_НА_АДРЕС "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"'
  Say ('       (вместо ЗАМЕНИТЕ_НА_АДРЕС: ' + $target + ')')
  Say "  Ничего не изменено: на карту не писали."
  exit 2
}

Say ""
Say "--- проверяю вход по ключу (пароля быть не должно) ---"
& $sshExe @common -p "$Port" -i $KeyFile -o IdentitiesOnly=yes `
    -o BatchMode=yes ($User + "@" + $HostName) "echo KEY_OK; id -un" 2>&1 | ForEach-Object {
  $line = $_.ToString()
  if ($line -match "KEY_OK") { Say "  вход по ключу работает" }
  elseif ($line.Trim().Length -gt 0) { Say ("  " + $line) }
}
if ($LASTEXITCODE -ne 0) {
  Say "  вход по ключу не подтвердился - запускайте с паролем (он спросится по шагам)"
  exit 3
}

Say ""
Say "=== ГОТОВО ==="
Say "  Дальше запускайте так (пароль больше не нужен):"
Say ("    .\RUN.ps1 -HostName " + $HostName + " -User " + $User + " -KeyFile " + $KeyFile)
if ($Port -ne 22) { Say ("    (порт: добавьте -Port " + $Port + ")") }
Say ""
Say "  Ключ лежит здесь: $KeyFile"
$tag = "pcmcia-kit@" + $env:COMPUTERNAME
Say "  Убрать доступ: на ноутбуке удалите из ~/.ssh/authorized_keys строку,"
Say ("  в которой есть " + $tag + " - больше эта машина входить не сможет")
exit 0
