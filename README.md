# PCMCIA FLASH KIT

Работа с линейной флеш-картой PCMCIA (linear flash) с обычной Windows-машины:
паспорт карты, снятие образа, запись, сверка, стирание. Сама карта стоит в
ноутбуке с Linux; пульт соединяется с ним по ssh и ничего там не устанавливает.

* Ни одного сценария PowerShell и ни одного `.cmd`: всё на Python 3.
* На ноутбук уходят только изменившиеся файлы набора.
* Запись выполняется командами микросхемы через окно моста, без драйверов.
* Понимает разные микросхемы: сектор 16/64/128 КиБ, команды байтом и словом,
  запись страницей 32 байта (AMD Am29F016/032), 5 В и 12 В на Vpp.

## Запуск без установки

```
python pcmcia-kit.pyw            окно пульта (или двойной щелчок по файлу)
python pcmcia-kit.pyw --text     текстовое меню
python pcmcia-kit.pyw --text info --dry-run
```

`pcmcia-kit.pyw` сам подключает папку `src`, поэтому настраивать нечего.
Форма `python -m pcmcia_kit` работает после установки пакета.

Требуется Python 3.9 или новее. Сторонних библиотек нет.

## Установка (необязательна)

Нужна, если хочется вызывать `pcmcia-kit` из любого каталога:

```
py -3 -m pip install .
pcmcia-kit info --host 192.168.0.71
```

Файлы набора (`pcmcia_direct.py`, `pcmcia_ctl.sh`) при этом остаются в
распакованной папке: запускайте из неё либо укажите `--kit-dir`.

## Запуск

```
python pcmcia-kit.pyw --text                     меню
python pcmcia-kit.pyw --text info                паспорт карты и вердикт
python pcmcia-kit.pyw --text read                снять образ и забрать к себе
python pcmcia-kit.pyw --text write -i card.bin   записать образ на карту
python pcmcia-kit.pyw --text verify -i card.bin  сверить файл с картой
python pcmcia-kit.pyw --text erase --yes         стереть карту
python pcmcia-kit.pyw --text chip                что за микросхема (не пишет)
python pcmcia-kit.pyw --text selftest            проверить набор (карта не нужна)
python pcmcia-kit.pyw --text key                 вход по ключу (без пароля)
```

После `pip install .` те же команды короче: `pcmcia-kit info`, `pcmcia-kit read`.

Двойной щелчок по `pcmcia-kit.pyw` открывает графический пульт: Windows
связывает `.pyw` с `pythonw.exe`, поэтому окно консоли не появляется.

Общие ключи: `--host`, `--user`, `--port`, `--key`, `--yes`, `--dry-run`,
`--image`, `--remote-path`, `--kit-dir`, `--full`.

## Что где лежит

```
pcmcia-kit.pyw            двойной щелчок -> графический пульт
pyproject.toml            пакет: команда pcmcia-kit, версия, настройки линтера
src/pcmcia_kit/           пульт (пакет)
    cli.py                разбор аргументов и вызов команды
    config.py             умолчания: адрес, порт, файлы набора, пункты меню
    console.py            сообщения и UTF-8 в консоли Windows
    transport.py          ssh/scp: запуск, копирование, живой вывод, ошибки
    sync.py               синхронизация набора по md5
    state.py              памятка «что за карта» в шапке меню
    remote.py             команды, которые выполняются на ноутбуке
    keyring.py            вход по ключу
    gui.py                графический пульт
    commands/             по модулю на команду
pcmcia_direct.py          работа с картой на ноутбуке (окна, CIS, JEDEC)
pcmcia_ctl.sh             действия на ноутбуке (info, read, write, erase...)
tests/                    проверки: test_scripts.py, test_win.py, test_gui...
PCMCIA_LINEAR_FLASH_СПРАВОЧНИК.md   справочник по картам
ИНСТРУКЦИЯ.md             инструкция по шагам
```

## Проверки

```
python tests/test_win.py         Windows-часть пульта (без сети и без карты)
python tests/test_scripts.py     скрипты набора (~30 с; --fast вдвое быстрее)
xvfb-run -a python tests/test_gui.py     интерфейс (нужен X или xvfb)
python tests/release.py --build  сборка выпуска: архивы и контрольные суммы
```

## Что проверено, а что — только на модели

Набор проверяется двумя способами: автоматическими тестами на модели микросхем
(`tests/nor_model.py`) и, когда есть физическая карта, на живой карте
Centennial FL32M (2 × Am29F016, 4 МиБ). Ниже — честная матрица, чтобы было
видно, какие части кода подтверждены на железе, а какие только имитируются.

| Раздел | Что делается | Живая карта (Centennial FL32M) | Только модель (`nor_model`) | Файлы |
|--------|-------------|--------------------------------|------------------------------|-------|
| Паспорт CIS | чтение attribute memory, идентификация | ✅ | ✅ | `pcmcia_direct.py`, `pcmcia_ctl.sh` |
| Чтение образа | снятие полного образа карты, md5 | ✅ | ✅ | `pcmcia_direct.py`, `pcmcia_ctl.sh` |
| Автоопределение чипа | JEDEC ID (90h), CFI (98h) | ✅ | ✅ | `pcmcia_direct.py` |
| Запись байта | A0h, 4 цикла, опрос DQ7 | ⏳ только частично | ✅ | `pcmcia_direct.py` |
| Unlock Bypass | 20h, запись 2 циклами, F0h | ⏳ только частично | ✅ | `pcmcia_direct.py` |
| Стирание сектора | 30h, опрос DQ7/DQ5 | ⏳ только частично | ✅ | `pcmcia_direct.py` |
| Blank check | после стирания всё 0xFF | ⏳ только частично | ✅ | `pcmcia_direct.py` |
| Защита секторов | чтение групп (A6h) | ❌ не проверялось | ✅ | `pcmcia_direct.py` |
| Erase Suspend/Resume | B0h / 30h | ❌ не проверялось | ✅ | `pcmcia_direct.py` |
| VPP/VCC слотa | Socket Control, ExCA, TPS2220B | ❌ не проверялось | ❌ не моделируется | `pcmcia_direct.py` |
| Карта износа | `.card-state.txt`, предупреждение canwrite | ❌ не проверялось | ✅ | `src/pcmcia_kit/state.py` |
| Карта отказов | bad-блоки, смещение, ожидаемое/фактическое | ❌ не проверялось | ✅ | `src/pcmcia_kit/state.py` |
| Журнал / --resume / --backup | прерванная запись, продолжение | ❌ не проверялось | ✅ | `src/pcmcia_kit/commands/*.py` |
| Машинный вывод | `--json` для info/canwrite/diag | ✅ | ✅ | `src/pcmcia_kit/cli.py` |
| Определение моста | PCI vendor/device ID (UltraMedia 0x5ca9) | ✅ | ✅ | `pcmcia_direct.py` |

**Легенда:** ✅ подтверждено на живой карте · ⏳ частично (отдельные секторы) · ❌ не проверялось.

Запись на живой карте пока не подтверждена полностью: основная проверка ведётся на модели микросхем
(`tests/nor_model.py`). Набор об этом честно пишет в отчётах.
