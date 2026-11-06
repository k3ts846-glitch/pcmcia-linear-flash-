@echo off
rem ===========================================================================
rem  RUN.cmd - двойной щелчок по этому файлу.
rem
rem  Нужен, когда PowerShell отказывается запускать RUN.ps1:
rem    "выполнение сценариев отключено в этой системе"
rem
rem  Ничего в системе не меняет: снимает запрет только для ОДНОГО окна
rem  (ExecutionPolicy Bypass -Scope Process) и запускает RUN.ps1.
rem
rem  Аргументы передаются дальше, например:
rem    RUN.cmd -HostName 192.168.0.94
rem    RUN.cmd -DryRun
rem ===========================================================================
setlocal
cd /d "%~dp0"
echo Запускаю RUN.ps1 с обходом политики (только для этого окна)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN.ps1" %*
echo.
echo Готово. Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
endlocal
