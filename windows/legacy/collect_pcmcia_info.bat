@echo off
rem ===========================================================================
rem  collect_pcmcia_info.bat
rem  Диагностика слота PCMCIA в Windows 7 (только ЧТЕНИЕ, ничего не меняет).
rem  Запускать от имени администратора: правый клик -> "Запуск от имени администратора".
rem  Результат: pcmcia_report.txt рядом со скриптом -> его не стыдно показать мне.
rem ===========================================================================
setlocal enabledelayedexpansion
chcp 1251 >nul 2>&1

net session >nul 2>&1
if errorlevel 1 (
  echo [ОШИБКА] Запустите файл от имени администратора.
  pause
  exit /b 1
)

set "OUT=%~dp0pcmcia_report.txt"
echo Сбор диагностики PCMCIA... (это займет несколько секунд)

> "%OUT%" echo ===== AMILO Pro V2030D / Windows 7: PCMCIA report =====
>>"%OUT%" echo Дата: %DATE% %TIME%
>>"%OUT%" echo Компьютер: %COMPUTERNAME%
>>"%OUT%" echo.
>>"%OUT%" echo ----- 1. Система -----
>>"%OUT%" systeminfo 2>nul | findstr /B /C:"OS Name" /C:"OS Version" /C:"System Type" /C:"Processor" /C:"Total Physical Memory"

>>"%OUT%" echo.
>>"%OUT%" echo ----- 2. Устройства класса PCMCIA (в т.ч. вставленные карты) -----
for /f "delims=" %%K in ('reg query "HKLM\SYSTEM\CurrentControlSet\Enum\PCMCIA" 2^>nul') do (
  >>"%OUT%" echo [%%~K]
  for /f "tokens=2*" %%A in ('reg query "%%K" /v DeviceDesc 2^>nul ^| findstr /I DeviceDesc') do >>"%OUT%" echo    DeviceDesc: %%B
  for /f "tokens=2*" %%A in ('reg query "%%K" /v Service 2^>nul ^| findstr /I Service') do >>"%OUT%" echo    Service:    %%B
  for /f "tokens=2*" %%A in ('reg query "%%K" /v Driver 2^>nul ^| findstr /I Driver') do >>"%OUT%" echo    Driver:     %%B
  for /f "tokens=2*" %%A in ('reg query "%%K" /v ConfigFlags 2^>nul ^| findstr /I ConfigFlags') do >>"%OUT%" echo    ConfigFlags:%%B
)

>>"%OUT%" echo.
>>"%OUT%" echo ----- 3. Все PnP-устройства с PCMCIA/CardBus в имени -----
>>"%OUT%" wmic path Win32_PnPEntity where "PNPDeviceID like '%%PCMCIA%%'" get DeviceID,Name,Status,Service /format:list 2>nul
>>"%OUT%" wmic path Win32_PnPEntity where "Name like '%%CardBus%%'" get DeviceID,Name,Status,Service /format:list 2>nul

>>"%OUT%" echo.
>>"%OUT%" echo ----- 4. Драйверы шины PCMCIA (файлы в системе) -----
if exist "%WINDIR%\System32\drivers\pcmcia.sys" >>"%OUT%" echo pcmcia.sys: есть
if not exist "%WINDIR%\System32\drivers\pcmcia.sys" >>"%OUT%" echo pcmcia.sys: НЕТ
>>"%OUT%" dir /b "%WINDIR%\System32\drivers\pcmcia*.sys" 2>nul
>>"%OUT%" dir /b "%WINDIR%\System32\drivers\*cardbus*" 2>nul

>>"%OUT%" echo.
>>"%OUT%" echo ----- 5. Накопители (если карта прикидывается диском) -----
>>"%OUT%" wmic logicaldisk get DeviceID,DriveType,FileSystem,Size,VolumeName /format:list 2>nul

>>"%OUT%" echo.
>>"%OUT%" echo ----- 6. Служба PCMCIA и питание слота -----
>>"%OUT%" sc query pcmcia
>>"%OUT%" reg query "HKLM\SYSTEM\CurrentControlSet\Services\Pcmcia" /v Start 2>nul

>>"%OUT%" echo.
>>"%OUT%" echo ===== конец отчета =====

echo.
echo Готово: "%OUT%"
echo Откройте файл, скопируйте содержимое (или сам файл) — по нему видно,
echo распознается ли карта вообще и каким драйвером.
echo.
echo ВАЖНО: Windows 7 не умеет читать/писать 16-битные linear flash карты
echo (Intel Series 2/2+, AMD, Catalyst). Этот отчет — только диагностика;
echo сама работа с картами делается из Linux (см. README_FIRST.txt).
pause
