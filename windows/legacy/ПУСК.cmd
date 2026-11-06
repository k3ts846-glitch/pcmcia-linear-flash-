@echo off
rem =====================================================================
rem  ПУСК.cmd - ОДИН файл, с которого всё начинается на Windows.
rem
rem  Двойной щелчок открывает меню пульта: паспорт карты, образ, проба
rem  записи, запись, сверка, стирание, полный отчёт, проверка набора.
rem
rem  Нужен только Python 3 (https://www.python.org/downloads/ - при
rem  установке отметьте "Add python.exe to PATH"). PowerShell не нужен.
rem
rem  Аргументы передаются дальше, например:
rem    ПУСК.cmd info
rem    ПУСК.cmd --host 192.168.0.71
rem    ПУСК.cmd selftest
rem =====================================================================
setlocal
cd /d "%~dp0"
title PCMCIA FLASH - пульт

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo Python 3 не найден - без него пульт не запускается.
  echo Поставьте его одним из двух способов:
  echo   1) https://www.python.org/downloads/ - при установке отметьте
  echo      "Add python.exe to PATH", затем запустите ПУСК.cmd снова;
  echo   2) в Microsoft Store: Python 3.12.
  echo.
  echo Проверить, что получилось: откройте новое окно и введите  python --version
  echo.
  pause
  exit /b 2
)

%PY% "%~dp0pcmcia_win.py" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo Код возврата %RC%. Ошибки выше написаны по-русски - читайте их.
)
pause
exit /b %RC%
