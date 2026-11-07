# -*- coding: utf-8 -*-
"""Образ карты: чтение, размер, обрезка, сводка, сравнение файлов."""

import os
import time

from .bridge import CB_MEM_PAGE0, I365_ADDRWIN, I365_MEM0_OFF, I365_MEM0_START, I365_MEM0_STOP, PROBE_LEN, PROGRESS_EVERY, STALL_TIMEOUT, _read_page, _window_text, looks_blank, mem_for_addr, window_span
from .cis import TRUST_CIS_MAX, find_cis, read_cis
from .errors import DirectError


# --------------------------------------------------------- образ карты
#
# ЧТО БЫЛО НЕ ТАК ДО 2.8.0 (нашлось по коду, а не на карте):
# образ читался по PCI-адресу окна напрямую, БЕЗ настройки ExCA. Но CIS
# читается через то же окно, настроенное на attribute memory (I365_MEM_REG).
# Значит «чтение по адресу окна» попадало в attribute memory, а не в саму
# флеш: вместо 4 МиБ данных получилось бы 4 МиБ паспорта, с зеркалами.
# Правильно - переставлять окно по карте, как ядро (yenta_set_mem_map):
# на карте N страниц, в форточку видна одна, чтение идёт с одного адреса.
#
# Сколько читать, решает САМА карта: лишние адресные линии не подключены, или
# карта отражает их зеркально, поэтому адрес повторяется с периодом, равным
# её размеру. Пробуем 512 КиБ, 1, 2, 4, 8, 16, 32 МиБ и берём первый период
# со СТАБИЛЬНЫМ совпадением (два чтения подряд), а не «на глаз».
SIZE_STEPS = (512 << 10, 1 << 20, 2 << 20, 4 << 20, 8 << 20, 16 << 20, 32 << 20)


def cis_card_size(info):
    """Ёмкость карты по паспорту: (байт, откуда) или (None, почему нет).

    Два независимых источника, оба уже разобраны выше:
      * CISTPL_DEVICE: размер каждого устройства считается как в ядре;
      * строка VERS_1 вида "32 MEG FLASH" и JEDEC "всего N шт.".
    """
    devs = (info or {}).get("devices") or []
    total = sum(d.get("size") or 0 for d in devs)
    if total and total <= TRUST_CIS_MAX:
        return total, f"по кортежу CISTPL_DEVICE ({len(devs)} устройств)"
    mbits = (info or {}).get("mbits")
    if mbits:
        return mbits * 1024 * 1024 // 8, f'по паспорту "{mbits} Мбит"'
    return None, "в паспорте нет ёмкости"


def _read_card(mem, exca, win, off, length, dev_id=None, tries=3):
    """Читает участок карты и убеждается, что прочитанное не «плывёт».

    Зачем: в живом логе два прохода двойного чтения шли с разной скоростью -
    0.40 МиБ/с и 0.19 МиБ/с. Это признак нестабильного ответа карты, и
    одиночное чтение такой случай не ловит: ошибка видна только на повторе.
    Здесь участок читается до трёх раз, при расхождении - понятная ошибка.
    """
    first = _read_page(mem, exca, win, off, length, dev_id=dev_id)
    second = _read_page(mem, exca, win, off, length, dev_id=dev_id) if tries > 1 else first
    if second == first:
        return first
    # два чтения разошлись - третье решает, кто прав (правило двух из трёх)
    if tries > 2:
        third = _read_page(mem, exca, win, off, length, dev_id=dev_id)
        if third == first:
            return first
        if third == second:
            return second
    raise DirectError(
        f"карта отвечает НЕСТАБИЛЬНО: участок со смещения {off} "
        f"({length} байт) читается по-разному за {tries} попытки. "
        "Обычные причины: контакты карты или слота, питание. "
        "Образ не сохранён, на карту ничего не записано - повторите "
        "чтение после чистки контактов или с меньшим заходом "
        "(--span 4096).")


def card_kind(info, tuples, at_off=None):
    """Что это за карта: (вид, что с ней можно делать).

    Виды: linear-flash, ata, sram, serial, network, unknown. От вида зависит,
    имеет ли смысл снимать образ: у ATA-диска это дамп всего диска, а у
    serial/network - набор регистров, а не данные.
    """
    func = (info or {}).get("func")
    jedec = (info or {}).get("jedec")
    types = {(d or {}).get("type_name", "").upper() for d in (info or {}).get("devices") or []}
    if at_off is not None:
        return "ata", ("паспорт лежит в common memory (смещение "
                       f"{at_off:#x}) - так устроены ATA-карты: "
                       "CompactFlash в переходнике, Microdrive, диски; "
                       "образ с такой карты - это дамп всего диска")
    if func == 1 and ("SRAM" in types or "DRAM" in types):
        return "sram", ("SRAM/DRAM с батареей: образ снимается, но данные "
                        "держатся только пока жива батарея; стирания "
                        "секторами нет, запись - обычная, без команд")
    if func == 1 and ("ROM" in types or "EPROM" in types or "OTPROM" in types):
        return "rom", ("ROM/EPROM/OTPROM: только чтение, записывать нечего")
    if func == 1 and ("FLASH" in types or jedec):
        return "linear-flash", "линейная флеш: образ снимается, запись - по Vpp"
    if func == 1:
        return "memory", "карта памяти: образ снимается как есть"
    if func in (2,):
        return "serial", ("последовательный порт: это НЕ носитель, образ не "
                          "имеет смысла (только регистры)")
    if func in (3,):
        return "parallel", "параллельный порт: не носитель, образ не нужен"
    if func in (6,):
        return "network", ("сетевая карта: не носитель, образ не нужен "
                           "(можно читать CIS и регистры)")
    return "unknown", "вид карты по паспорту не определён"


def window_access_diag(dev, exca, win):
    """Почему attribute memory читается нулями: разбор ДО выводов о карте.

    Живой лог показал важное: при вынутой карте attribute memory читалась
    нулями, а не 0xFF. Для пустого слота это ненормально - шина PC Card без
    карты обычно отдаёт 0xFF (ничем не притянута). Нули означают, что чтение
    не доходит до карты: чаще всего ядро заняло окно своим драйвером
    (yenta_socket) и переставило регистры ExCA, либо /dev/mem отдал не ту
    страницу. Поэтому сначала - факты, потом вывод «карта не отвечает».

    Возвращает (список строк, подозрение: bool).
    """
    out = []
    suspect = False
    try:
        aw_reg = mem_for_addr(dev, exca, writable=True, dev_id=dev)
    except DirectError as e:
        return [f"регистры ExCA недоступны: {e}"], True
    try:
        aw = aw_reg.read(exca + I365_ADDRWIN, 1)[0]
        page = aw_reg.read(exca + CB_MEM_PAGE0, 1)[0]
        start = int.from_bytes(aw_reg.read(exca + I365_MEM0_START, 2), "little")
        stop = int.from_bytes(aw_reg.read(exca + I365_MEM0_STOP, 2), "little")
        off = int.from_bytes(aw_reg.read(exca + I365_MEM0_OFF, 2), "little")
    except DirectError as e:
        return [f"регистры ExCA не читаются: {e}"], True
    out.append(f"  ExCA: ADDRWIN={aw:#04x} (окно 0 {'включено' if aw & 1 else 'ВЫКЛЮЧЕНО'}), "
               f"PAGE0={page:#04x}, START={start:#06x}, STOP={stop:#06x}, OFF={off:#06x}")
    if not (aw & 1):
        out.append("  -> до чтения окно 0 было ВЫКЛЮЧЕНО: значит его переставил")
        out.append("     драйвер моста (yenta_socket) или оно не настраивалось.")
        suspect = True
    # кто ещё держит память моста
    try:
        import glob
        for p in glob.glob("/sys/bus/pci/devices/*/driver"):
            name = os.path.basename(os.path.realpath(p))
            if name in ("yenta_socket", "pcmcia_socket", "pcmcia"):
                out.append(f"  драйвер моста загружен: {name} - он мог занять окно;")
                out.append("     для прямого чтения его выгружают: modprobe -r yenta_socket")
                suspect = True
                break
    except OSError:
        pass
    return out, suspect


def card_window_check(mem, exca, win, dev_id=None):
    """Здоровье окна: (ok, текст). Ловит «пустой слот» и «занятое окно».

    Смысл отдельной проверки: нельзя снимать 4 МиБ, если окно не читается
    вообще или читается мусор - иначе пустой слот выглядит как карта,
    забитая нулями.
    """
    try:
        a = _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)
        b = _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)
    except DirectError as e:
        return False, f"окно не читается: {e}"
    if a != b:
        return False, ("окно читается НЕСТАБИЛЬНО: два чтения одного и того "
                       "же места различаются - контакты или питание")
    if a == b"\x00" * len(a):
        return True, ("в окне сплошные нули: шина прижата, карта не отвечает "
                      "(пустой слот или карта не контачит)")
    if a == b"\xff" * len(a):
        return True, "в окне сплошные 0xFF: карта не отвечает (пустой слот?)"
    return True, "окно читается, содержимое неоднородное"


def _page_signature(mem, exca, win, dev_id=None):
    """Содержимое первой страницы карты как «подпись» для поиска повторов."""
    return _read_page(mem, exca, win, 0, PROBE_LEN, dev_id=dev_id)


def find_card_size(mem, exca, win, steps=SIZE_STEPS, probe=PROBE_LEN,
                   dev_id=None, verbose=False, expect=None):
    """Размер карты: (размер, как определено).

    Порядок, от самого надёжного к самому грубому:
      1) повторение адресов (работает, если карта зеркалит лишние линии);
      2) если повторения нет, а паспорт говорит ёмкость - берём ПАСПОРТ;
      3) если в начале карты непустые данные, размер не больше первого
         появления нулей/0xFF на страничной границе (конец карты);
      4) иначе - окно моста (и об этом громко предупреждаем).
    Паспорт важнее «на глаз»: 64 МиБ окна - это не карта.
    """
    head = _page_signature(mem, exca, win, dev_id=dev_id)
    blank_head = looks_blank(head, 0xFF) or looks_blank(head, 0x00)
    for period in steps:
        if period < probe:
            continue
        if period >= win["size"]:
            break
        try:
            a = _read_page(mem, exca, win, period, probe, dev_id=dev_id)
            # Если в начале всё 0xFF/0x00, совпадение ничего не доказывает:
            # у стёртой карты и у «пустоты за концом карты» одинаковый вид.
            if blank_head and period != (expect or -1):
                continue
            if a != head:
                continue
            b = _read_page(mem, exca, win, period, probe, dev_id=dev_id)
            if b == head:
                return period, f"адрес повторяется через {period / 1024 / 1024:.1f} МиБ"
        except DirectError:
            continue

    if expect:
        # 2) паспорт: читаем последнюю страницу карты, чтобы убедиться, что
        #    это действительно карта, а не дыра за её концом (0x00/0xFF)
        try:
            tail = _read_page(mem, exca, win, expect - PROBE_LEN, PROBE_LEN,
                              dev_id=dev_id)
        except DirectError:
            tail = None
        if tail is not None and tail == head and not blank_head:
            return expect, ("по паспорту; внутри карты содержимое повторяется, "
                            "то есть карта СВОРАЧИВАЕТ адреса")
        note = ""
        if tail is not None:
            if looks_blank(tail, 0x00):
                note = ("; за концом карты читаются нули - линии адреса не "
                        "подключены, повторения не будет")
            elif looks_blank(tail, 0xFF):
                note = "; за концом карты 0xFF (пустая шина)"
            else:
                note = "; за концом карты другие данные"
        return expect, f"по паспорту ({expect / 1024 / 1024:.0f} МиБ)" + note

    # 3) конец карты по первому «пустому» участку, если начало непустое
    if not blank_head:
        prev = head
        for period in steps:
            if period <= probe or period >= win["size"]:
                continue
            try:
                pg = _read_page(mem, exca, win, period, PROBE_LEN, dev_id=dev_id)
            except DirectError:
                break
            if (looks_blank(pg, 0x00) or looks_blank(pg, 0xFF)) and \
                    looks_blank(prev, 0x00) is False:
                return period, (f"по первому пустому участку ({period / 1024 / 1024:.0f} МиБ): "
                                "дальше за картой ничего нет")
            prev = pg

    # 4) последний случай - окно моста, с честным предупреждением
    return win["size"], (f"повторения не нашли и ёмкости в паспорте нет, "
                         f"беру окно моста {win['size']:#x} - это МОЖЕТ БЫТЬ "
                         f"больше карты")


def read_card_image(mem, exca, win, path, size, double=False, progress=None,
                    dev_id=None, verbose=False, span=None):
    """Снимает образ карты: окно переставляется по карте, читается common memory.

    Возвращает dict: сколько байт, md5, путь. При double=True образ читается
    дважды и сравнивается — нестабильное чтение не выдаётся за готовый образ.
    """
    import hashlib
    tmp = path + ".part"
    step = window_span(win, span)

    last = [0]

    def one_pass(out_path, pass_no=1):
        h = hashlib.md5()
        done = 0
        last[0] = 0
        last_move = time.time()
        with open(out_path, "wb") as f:
            while done < size:
                n = min(step, size - done)
                # первый блок каждого захода читаем со сверкой повтором:
                # если карта поплыла, узнаем это в первые секунды, а не в конце
                if done == 0:
                    blk = _read_card(mem, exca, win, done, n, dev_id=dev_id)
                else:
                    blk = _read_page(mem, exca, win, done, n, dev_id=dev_id)
                f.write(blk)
                h.update(blk)
                done += n
                # стоп по застою: если карта залипла (или мост перестал
                # отвечать), процесс иначе висит навсегда. 60 с без единого
                # нового байта - это уже не «медленно», а поломка.
                if time.time() - last_move > STALL_TIMEOUT:
                    raise DirectError(
                        f"чтение встало: {STALL_TIMEOUT} с без нового байта "
                        f"(остановились на {done} из {size}). Причины: карту "
                        "вынули, контакты, зависший мост. Образ не сохранён.")
                last_move = time.time()
                # проценты - не чаще PROGRESS_EVERY байт: иначе на быстром
                # чтении они печатаются тысячи раз и сами тормозят вывод
                if progress and (done >= size or done - last[0] >= PROGRESS_EVERY):
                    last[0] = done
                    progress(done, size, pass_no)
            f.flush()
            os.fsync(f.fileno())
        return h.hexdigest()

    # защита от второго экземпляра по тому же файлу: два чтения сразу
    # переставляют одно и то же окно моста, и оба получают мусор
    lock_path = path + ".lock"
    lock_fh = None
    try:
        import fcntl
        lock_fh = open(lock_path, "w")
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if lock_fh:
            lock_fh.close()
        raise DirectError(
            f"образ {path} уже снимается другим запуском ({lock_path} занят). "
            "Дождитесь его конца или возьмите другое имя файла.") from e
    except ImportError:
        lock_fh = None
    try:
        md5 = one_pass(tmp, 1)
        if double:
            chk = path + ".chk"
            try:
                md5b = one_pass(chk, 2)
                same = filecmp_equal(tmp, chk)
            finally:
                try:
                    os.remove(chk)
                except OSError:
                    pass
            if not same or md5 != md5b:
                raise DirectError(
                    "два чтения РАЗЛИЧАЮТСЯ — карта или контакты нестабильны, "
                    f"образ не сохранён (md5 {md5} и {md5b})")
        os.replace(tmp, path)
    except BaseException:
        for junk in (tmp, path + ".chk"):
            try:
                os.remove(junk)
            except OSError:
                pass
        raise
    finally:
        if lock_fh:
            try:
                import fcntl
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_fh.close()
            try:
                os.remove(lock_path)
            except OSError:
                pass
    return {"path": path, "size": size, "md5": md5,
            "sha256": file_sha256(path)}


def image_regions(path, page=4096):
    """Карта занятости: одинаковые соседние страницы объединяются в участки.

    Зачем: по процентам «0x00 78.8%, данные 18.4%» нельзя понять, ГДЕ данные.
    Из живого лога карта Centennial дала сплошные нули в начале при данных в
    середине - по одним процентам этого не увидеть. Здесь возвращается список
    участков (начало, длина, вид), а вид считается по содержимому страницы.
    """
    out = []
    prev = None
    size = os.path.getsize(path)
    off = 0

    def kind_of(blk):
        if not blk:
            return "пусто"
        ff = blk.count(0xFF)
        z = blk.count(0x00)
        if ff == len(blk):
            return "0xFF (стёрто)"
        if z == len(blk):
            return "0x00 (нули)"
        if z > len(blk) * 0.9:
            return "почти нули"
        if ff > len(blk) * 0.9:
            return "почти 0xFF"
        return "данные"

    with open(path, "rb") as f:
        while off < size:
            blk = f.read(page)
            if not blk:
                break
            k = kind_of(blk)
            if prev and prev[0] == k:
                prev[1] += len(blk)
            else:
                prev = [k, len(blk), off]
                out.append(prev)
            off += len(blk)
    return out


def image_stats(path):
    """Короткая сводка по образу: сколько 0xFF, 0x00, прочего."""
    ff = z = n = 0
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            n += len(blk)
            ff += blk.count(b"\xff")
            z += blk.count(b"\x00")
    n = n or 1
    return {"bytes": n, "ff": ff, "zero": z, "other": n - ff - z,
            "ff_pct": 100.0 * ff / n, "zero_pct": 100.0 * z / n,
            "other_pct": 100.0 * (n - ff - z) / n}


def file_sha256(path, chunk=1 << 20):
    """sha256 файла. Нужен для настоящих данных, где md5 слабоват."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def trim_image(path, size_wanted, keep_original=True):
    """Обрезает образ до size_wanted байт (остальное - пустая шина за картой).

    Возвращает (путь результата, сколько байт). Исходник не удаляется, если
    keep_original: пересъёмка на новом наборе дешевле, чем потеря старого файла.
    """
    size = os.path.getsize(path)
    if size <= size_wanted:
        return path, size
    if path.lower().endswith(".bin"):
        out = path[:-4] + f"-{size_wanted}.bin"
    else:
        out = path + ".trim"
    with open(path, "rb") as src, open(out, "wb") as dst:
        left = size_wanted
        while left > 0:
            blk = src.read(min(1 << 20, left))
            if not blk:
                break
            dst.write(blk)
            left -= len(blk)
    if not keep_original:
        try:
            os.remove(path)
        except OSError:
            pass
    return out, os.path.getsize(out)


def trim_hint(path, size_wanted):
    """Что делать, если образ больше карты (ошибка 2.8.9 с окном моста)."""
    size = os.path.getsize(path)
    if size <= size_wanted:
        return []
    return [f"  ВНИМАНИЕ: образ {size} байт, а карта по паспорту {size_wanted} байт.",
            f"  Первые {size_wanted} байт - это карта, остальное - пустая шина.",
            f"  Обрезать: head -c {size_wanted} {os.path.basename(path)} > card-карта.bin",
            "  Чище переснять: в наборе 2.9.0+ размер берётся из паспорта."]


def image_report(res, stats, win, how):
    """Строки отчёта о снятом образе (без карты их нечем восстановить)."""
    out = ["", "Образ карты:", _window_text(win)]
    out.append(f"  файл   : {res['path']}")
    out.append(f"  размер : {res['size']} байт "
               f"({res['size'] / 1024 / 1024:.1f} МиБ) — {how}")
    out.append(f"  md5    : {res['md5']}")
    if res.get("sha256"):
        out.append(f"  sha256 : {res['sha256']}")
    out.append(f"  состав : 0xFF {stats['ff_pct']:.1f}%, "
               f"0x00 {stats['zero_pct']:.1f}%, "
               f"данные {stats['other_pct']:.1f}%")
    if stats["other_pct"] < 0.1:
        out.append("  ВНИМАНИЕ: в образе почти нет данных (всё 0xFF/0x00).")
        out.append("    Так выглядит стёртая карта ИЛИ чтение не туда: проверьте,")
        out.append("    что в разделе 7 паспорт читается (карта отвечает).")
    try:
        parts = image_regions(res["path"])
    except OSError:
        parts = []
    if parts:
        out.append("  где что лежит (участки одинаковых страниц склеены):")
        for k, n, off in parts[:12]:
            if n >= 4096:
                out.append(f"    {off:#09x}  {k:<16} {n / 1024:8.1f} КиБ")
        if len(parts) > 12:
            out.append(f"    ... всего участков: {len(parts)}")
    return out


def normalize_bankwidth(data):
    """8-битная карта на 16-битной шине даёт каждый байт дважды.

    Возвращает (данные, ширина): 2 — как есть, 1 — если байты явно дублируются.
    Признак дублирования: чётные байты равны следующим нечётным на всём куске.
    """
    if len(data) < 8:
        return data, 2
    even = data[0::2]
    odd = data[1::2]
    if even[:4096] == odd[:4096] and len(set(even[:1024])) > 1:
        return even, 1
    return data, 2


def strings(data, minlen=4, limit=16):
    """Печатаемые строки — по ним видно PRODID/MANFID карты."""
    res, cur = [], bytearray()
    for b in data:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                res.append(cur.decode("ascii"))
                if len(res) >= limit:
                    return res
            cur = bytearray()
    if len(cur) >= minlen:
        res.append(cur.decode("ascii"))
    return res


def summarize(data):
    """Короткая сводка содержимого окна: пусто / данные / что похоже."""
    n = len(data)
    kinds = set(data)
    res = {"bytes": n,
           "unique_values": len(kinds),
           "empty_ff": looks_blank(data, 0xFF),
           "empty_00": looks_blank(data, 0x00),
           "cis_marks": find_cis(data),
           "strings": strings(data)}
    if res["empty_ff"]:
        res["verdict"] = "всё 0xFF — карта стёрта (или это не карта, а пустое окно)"
    elif res["empty_00"]:
        res["verdict"] = "всё 0x00 — типично для неотвечающей шины или SRAM без питания"
    elif len(kinds) < 8:
        res["verdict"] = f"подозрительно однородное содержимое ({len(kinds)} разных байт)"
    else:
        res["verdict"] = "похоже на настоящие данные"
    return res


def filecmp_equal(a, b):
    import filecmp
    return filecmp.cmp(a, b, shallow=False)


# ---------------------------------------------------------------- паспорт ---

def card_geometry(chips_hint=0):
    """(сколько чипов, сколько байт всего) по паспорту карты.

    Читается без окна: read_cis сам его настраивает. Если паспорт пуст -
    остаётся только догадка «два чипа» (так у 16-битных карт в паре).
    """
    chips, size = (chips_hint or 2), 0
    try:
        _raw, _tuples, info = read_cis(mem=None, verbose=False)
    except DirectError:
        return chips, size
    devs = (info or {}).get("devices") or []
    total = sum((d.get("size") or 0) for d in devs)
    if devs:
        chips = max(len(devs), 1)
    if total:
        size = total
    else:
        size, _why = cis_card_size(info or {})
    return chips, (size or 0)
