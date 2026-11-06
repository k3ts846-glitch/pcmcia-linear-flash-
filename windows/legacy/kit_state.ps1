# ============================================================================
#  kit_state.ps1 - «последнее состояние карты» между запусками пульта.
#
#  Зачем: один и тот же вопрос («что за карта») задаётся каждый раз заново,
#  а ответ печатается и теряется при закрытии окна. Эти функции запоминают
#  главные строки последнего опроса и показывают их в шапке меню.
#
#  Файл отдельный, потому что логику надо ПРОВЕРЯТЬ: см. t_card_state в
#  tests/test_scripts.py (настоящий PowerShell, без сети и без карты).
# ============================================================================

# Один и тот же вопрос («что за карта») задаётся каждый раз заново, а ответ
# печатается в консоли и теряется. Запоминаем последние важные строки в файл
# рядом и показываем их в шапке меню: человек видит, с чем работает, ещё до
# того как что-то нажал.
# Папку запоминаем при загрузке: внутри функции $PSScriptRoot пуст, если файл
# подключён через точку (. kit_state.ps1) - на этом легко потерять файл
# состояния в текущем каталоге, откуда запущен пульт.
if (-not $script:KitStateDir) {
  $script:KitStateDir = if ($env:PCMCIA_STATE_DIR) { $env:PCMCIA_STATE_DIR }
                        elseif ($PSScriptRoot) { $PSScriptRoot }
                        else { (Get-Location).Path }
}

function Get-CardStatePath {
  return (Join-Path $script:KitStateDir ".card-state.txt")
}

function Save-CardState([string]$text) {
  try {
    if (-not $text) { return }
    $keep = @()
    foreach ($ln in ($text -split "`n")) {
      if ($ln -match 'микросхема\s*:|напряжение программир|сектор стирания|' +
                     'протокол команд|вид карты|переключатель защиты') {
        $keep += $ln.Trim()
      }
    }
    if ($keep.Count -gt 0) {
      $head = (Get-Date -Format "yyyy-MM-dd HH:mm") + "  " + $HostName
      Set-Content -Path (Get-CardStatePath) -Value (@($head) + $keep) -Encoding UTF8
    }
  } catch { }        # состояние - удобство, а не условие работы
}

function Show-CardState {
  $p = Get-CardStatePath
  if (-not (Test-Path $p)) { return }
  try {
    $lines = Get-Content -Path $p -Encoding UTF8
    if ($lines.Count -ge 2) {
      Say ("  карта (последний опрос " + $lines[0].Trim() + "):")
      foreach ($ln in ($lines | Select-Object -Skip 1)) { Say ("    " + $ln) }
    }
  } catch { }
}
