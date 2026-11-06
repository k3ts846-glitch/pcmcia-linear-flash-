@echo off
rem ========================================================================
rem  RUN_CTL.cmd - ПУЛЬТ УПРАВЛЕНИЯ КАРТОЙ (чтение, запись, стирание).
rem
rem  Открывает обычное окно PowerShell и передаёт ему RUN_CTL.ps1 вместе
rem  со всеми аргументами. Нужно потому, что в PowerShell ISE и при запуске
rem  .ps1 двойным кликом ssh не может спросить пароль.
rem
rem  Примеры:
rem    RUN_CTL.cmd                       меню
rem    RUN_CTL.cmd info                  паспорт и вердикт
rem    RUN_CTL.cmd read                  снять образ и забрать к себе
rem    RUN_CTL.cmd write -Image card.bin записать образ на карту
rem ========================================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_CTL.ps1" %*
echo.
echo Готово. Если ниже были ошибки, они написаны по-русски, читайте их.
pause >nul
endlocal
