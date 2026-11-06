#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remote_collect.py — запускается на ВАШЕМ компьютере (откуда пишете).

Что делает:
  1) проверяет, что есть ssh/scp и файлы набора;
  2) копирует файлы набора на ноутбук по scp (в /tmp/pcmcia-collect — на
     ноутбуке ничего не остаётся, карта не затрагивается);
  3) запускает на ноутбуке collect_all.py от root (или через sudo) одной
     командой ssh;
  4) забирает отчёт к себе по scp в файл otchet-<хост>-<дата>.txt
     (а с --pull-image — и снятый образ);
  5) временную папку на ноутбуке убирает.

Пароль нигде не сохраняется: его спрашивает сам ssh (и sudo, если нужен),
в командную строку и в файлы он не попадает.

ЗАПУСК:
    python3 remote_collect.py                          # спросит хост
    python3 remote_collect.py 192.168.0.94
    python3 remote_collect.py root@192.168.0.94
    python3 remote_collect.py --user root --host 192.168.0.94
    python3 remote_collect.py 192.168.0.94 --image     # + снять образ карты
    python3 remote_collect.py 192.168.0.94 --kit /media/usb/pcmcia-flash-kit
    python3 remote_collect.py 192.168.0.94 --dry-run   # только показать, что будет

Ключи:
    --host H        адрес ноутбука (по умолчанию 192.168.0.94)
    --user U        пользователь SSH (по умолчанию root)
    --port P        порт SSH (по умолчанию 22)
    --kit DIR       какая папка набора отправляется
    --image         после диагностики снять образ карты в /root/card.bin
    --img PATH      куда писать образ (по умолчанию /root/card.bin)
    --pull-image    вместе с --image забрать образ карты к себе
    --no-slow       без --selftest и --diag (быстрее)
    --dry-run       ничего не подключать, только показать команды
    -h, --help      эта справка
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_HOST = "192.168.0.94"
DEFAULT_USER = "root"
DEFAULT_PORT = "22"
REMOTE_DIR = "/tmp/pcmcia-collect"
REMOTE_REPORT = "otchet.txt"


def die(msg: str) -> None:
    print(f"ОШИБКА: {msg}", file=sys.stderr)
    sys.exit(2)


def say(text: str = "") -> None:
    print(text)


def have(name: str) -> bool:
    from shutil import which
    return which(name) is not None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remote_collect.py",
        description="Диагностика карты PCMCIA по SSH (запуск с рабочей машины).",
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"адрес ноутбука (по умолчанию {DEFAULT_HOST})")
    p.add_argument("--user", "-u", default=DEFAULT_USER,
                   help=f"пользователь SSH (по умолчанию {DEFAULT_USER})")
    p.add_argument("--port", "-p", default=DEFAULT_PORT,
                   help=f"порт SSH (по умолчанию {DEFAULT_PORT})")
    p.add_argument("--kit", default="",
                   help="папка набора (по умолчанию ищется рядом)")
    p.add_argument("--image", action="store_true",
                   help="снять образ карты в /root/card.bin")
    p.add_argument("--img", default="/root/card.bin",
                   help="куда писать образ (по умолчанию /root/card.bin)")
    p.add_argument("--pull-image", action="store_true",
                   help="забрать образ карты к себе")
    p.add_argument("--no-slow", action="store_true",
                   help="без --selftest и --diag (быстрее)")
    p.add_argument("--dry-run", action="store_true",
                   help="ничего не подключать, только показать команды")
    return p


def parse_host_arg(raw: str) -> tuple[str, str]:
    """Разбирает user@host или просто host."""
    if "@" in raw:
        user, host = raw.split("@", 1)
        return user, host
    return DEFAULT_USER, raw


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        print(f"неизвестный ключ: {unknown[0]} (см. --help)", file=sys.stderr)
        return 2

    # host может быть user@host
    user, host = parse_host_arg(args.host)
    port = args.port
    kit = args.kit
    extra = ""
    if args.image:
        extra += " --image"
    if args.img != "/root/card.bin":
        extra += f" --img {args.img}"
    if args.no_slow:
        extra += " --no-slow"
    pull = args.pull_image
    dry = args.dry_run

    target = f"{user}@{host}"

    # ---- ищем набор рядом со скриптом --------------------------------------
    self_dir = Path(__file__).resolve().parent
    if not kit:
        for c in (self_dir, self_dir / "pcmcia-flash-kit",
                  Path.cwd(), Path.cwd() / "pcmcia-flash-kit"):
            if (c / "windows" / "legacy" / "pcmcia_direct.py").is_file():
                kit = str(c)
                break
    if not kit:
        die("не нашёл набор рядом (нужен windows/legacy/pcmcia_direct.py).\n"
            "  Укажите папку: --kit /путь/к/pcmcia-flash-kit")
    kit_p = Path(kit)
    if not (kit_p / "windows" / "legacy" / "pcmcia_direct.py").is_file():
        die(f"в {kit} нет windows/legacy/pcmcia_direct.py")
    if not (kit_p / "collect_all.py").is_file():
        die(f"в {kit} нет collect_all.py — скрипт, который\n"
            "  выполняется на ноутбуке. Он должен лежать рядом с pcmcia_direct.py.")

    if not have("ssh"):
        die("нет команды ssh.\n"
            "  Debian/Ubuntu: sudo apt install openssh-client\n"
            "  Windows: используйте WSL или Git Bash (там ssh есть).")
    if not have("scp"):
        die("нет команды scp (она в том же пакете, что и ssh).")

    # ---- что отправляем ----------------------------------------------------
    stage = Path(tempfile.mkdtemp(prefix="pcmcia-push-"))

    def cleanup() -> None:
        shutil.rmtree(str(stage), ignore_errors=True)

    import atexit, signal
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, lambda *_: (cleanup(), sys.exit(130)))
    signal.signal(signal.SIGTERM, lambda *_: (cleanup(), sys.exit(143)))

    files_to_send = ["collect_all.py"]
    optional_files = ["pcmcia_flash.sh", "pcmcia_gui.py",
                      "SSH_РАБОТА.txt", "README_FIRST.txt"]
    for f in files_to_send:
        src = kit_p / f
        if not src.is_file():
            die(f"не удалось скопировать {f}")
        shutil.copy2(str(src), str(stage / f))
    # pcmcia_direct.py теперь в windows/legacy/ — копируем как pcmcia_direct.py
    direct_src = kit_p / "windows" / "legacy" / "pcmcia_direct.py"
    if not direct_src.is_file():
        die("не удалось скопировать windows/legacy/pcmcia_direct.py")
    shutil.copy2(str(direct_src), str(stage / "pcmcia_direct.py"))
    for f in optional_files:
        src = kit_p / f
        if src.is_file():
            shutil.copy2(str(src), str(stage / f))
    for f in stage.iterdir():
        if f.suffix == ".sh":
            try:
                f.chmod(0o755)
            except OSError:
                pass

    out_local = Path.cwd() / f"otchet-{host}-{time.strftime('%Y%m%d-%H%M%S')}.txt"

    ssh_opts = f"-p {port} -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15"
    scp_opts = f"-P {port} -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15"

    # ШАГ 1 — копирование
    mk_cmd = f"rm -rf '{REMOTE_DIR}'; mkdir -p '{REMOTE_DIR}'"

    # ШАГ 2 — запуск на ноутбуке
    run_cmd = (
        f"cd '{REMOTE_DIR}' || exit 2\n"
        f"rm -f '{REMOTE_DIR}/{REMOTE_REPORT}' 2>/dev/null || true\n"
        f"if [ \"$(id -u)\" = 0 ]; then\n"
        f"  python3 ./collect_all.py --out '{REMOTE_DIR}/{REMOTE_REPORT}'{extra} > '{REMOTE_DIR}/.console.log' 2>&1\n"
        f"else\n"
        f"  sudo python3 ./collect_all.py --out '{REMOTE_DIR}/{REMOTE_REPORT}'{extra} > '{REMOTE_DIR}/.console.log' 2>&1\n"
        f"fi\n"
        f"rc=$?\n"
        f"echo '===== ОТЧЁТ ЦЕЛИКОМ ====='\n"
        f"if [ -f '{REMOTE_DIR}/{REMOTE_REPORT}' ]; then\n"
        f"  cat '{REMOTE_DIR}/{REMOTE_REPORT}'\n"
        f"else\n"
        f"  echo '(отчёт не создан, код $rc) — что напечатала программа:'\n"
        f"  tail -40 '{REMOTE_DIR}/.console.log' 2>/dev/null\n"
        f"fi\n"
        f"exit $rc"
    )

    clean_cmd = f"rm -rf '{REMOTE_DIR}'"

    say("=== ЗАПУСК ДИАГНОСТИКИ ПО SSH ===")
    say(f"  с этой машины : {os.uname().nodename}, {time.strftime('%Y-%m-%d %H:%M')}")
    say(f"  ноутбук       : {target} (порт {port})")
    say(f"  набор         : {kit}")
    say(f"  отчёт придёт в: {out_local}")
    say("")

    stage_files = sorted(p.name for p in stage.iterdir())

    if dry:
        say("--dry-run: реальных подключений не делаю. Что было бы выполнено:")
        say("")
        say("  1) копирование набора на ноутбук (scp, файл за файлом):")
        say(f"       {' '.join(stage_files)}")
        say(f"       ssh {ssh_opts} '{target}' \\\"{mk_cmd}\\\"")
        say(f"       scp {scp_opts} {' '.join(stage_files)} '{target}:{REMOTE_DIR}/'")
        say("  2) запуск проверок (ssh; -tt нужен, если понадобится пароль sudo):")
        for line in run_cmd.splitlines():
            say(f"       {line}")
        say("  3) отчёт забирается обратно (scp):")
        say(f"       scp {scp_opts} '{target}:{REMOTE_DIR}/{REMOTE_REPORT}' '{out_local}'")
        if pull:
            say("  4) образ забирается обратно (scp):")
            say(f"       scp {scp_opts} '{target}:/root/card.bin' '{Path.cwd()}/card-{host}-<дата>.bin'")
        say("  5) уборка на ноутбуке после успешного забора:")
        say(f"       ssh {ssh_opts} '{target}' \\\"{clean_cmd}\\\"")
        say("")
        say("  проверка готовности:")
        say(f"    ssh (клиент)      : {have('ssh') and 'есть' or 'НЕТ'}")
        say(f"    scp (клиент)      : {have('scp') and 'есть' or 'НЕТ'}")
        say(f"    файлов в отправке : {len(stage_files)}")
        return 0

    say("Сейчас ssh попросит пароль пользователя на ноутбуке,")
    say("а если он не root — ещё и пароль sudo на ноутбуке.")
    say("Пароли нигде не сохраняются и в файлы не попадают.")
    say("")

    # Шаг 1: копирование набора по scp
    say("--- шаг 1: копирую набор на ноутбук (scp) ---")
    try:
        res = subprocess.run(
            ["ssh", *ssh_opts.split(), target, mk_cmd],
            capture_output=True, text=True,
        )
        for line in res.stdout.splitlines():
            say(f"    {line}")
        send_rc = res.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        send_rc = 1
        say(f"    (ошибка ssh: {exc})")

    if send_rc == 0:
        try:
            res = subprocess.run(
                ["scp", *scp_opts.split(), *[str(stage / f) for f in stage_files],
                 f"{target}:{REMOTE_DIR}/"],
                capture_output=True, text=True,
            )
            for line in res.stdout.splitlines():
                say(f"    {line}")
            send_rc = res.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            send_rc = 1
            say(f"    (ошибка scp: {exc})")

    if send_rc != 0:
        say("")
        say(f"=== НЕ УДАЛОСЬ СКОПИРОВАТЬ НАБОР (код {send_rc}) ===")
        say("  Частые причины:")
        say(f"   * ноутбук недоступен:   ping -c2 {host}")
        say("   * нет ssh-сервера:      на ноутбуке sudo apt install openssh-server")
        say("   * другой пользователь:  python3 remote_collect.py --user ВАШ_ЛОГИН")
        say("   * другой порт:          --port 2222")
        return send_rc
    say(f"    набор на ноутбуке: OK ({len(stage_files)} файлов)")

    # Шаг 2: запуск проверок
    say("")
    say("--- шаг 2: запускаю проверки на ноутбуке ---")
    try:
        res = subprocess.run(
            ["ssh", "-tt", *ssh_opts.split(), target, run_cmd],
            capture_output=True, text=True,
        )
        for line in res.stdout.splitlines():
            say(f"    {line}")
        rc = res.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"    (ошибка ssh: {exc})")
        rc = 1

    # Шаг 3: забрать отчёт по scp
    say("")
    say("--- шаг 3: забираю отчёт (scp) ---")
    got = False
    try:
        res = subprocess.run(
            ["scp", scp_opts.split(), f"{target}:{REMOTE_DIR}/{REMOTE_REPORT}", str(out_local)],
            capture_output=True, text=True,
        )
        for line in res.stdout.splitlines():
            say(f"    {line}")
        if out_local.is_file() and out_local.stat().st_size > 0:
            got = True
            say(f"    отчёт получен: {out_local}")
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"    (ошибка scp: {exc})")

    if not got:
        say("    отчёт забрать не удалось (ssh уже напечатал его выше — он сохранён)")
        try:
            out_local.touch()
        except OSError:
            pass

    # Шаг 4: уборка на ноутбуке
    if got:
        say("--- шаг 4: убираю набор с ноутбука ---")
        try:
            res = subprocess.run(
                ["ssh", *ssh_opts.split(), target, clean_cmd],
                capture_output=True, text=True,
            )
            if res.returncode == 0:
                say(f"    убрано: {REMOTE_DIR}")
            else:
                say("    убрать не удалось (не страшно: папка в /tmp)")
        except (OSError, subprocess.SubprocessError):
            say("    убрать не удалось (не страшно: папка в /tmp)")
    else:
        say("--- шаг 4: набор на ноутбуке оставлен (отчёт ещё не забран) ---")

    say("")
    if rc != 0:
        say(f"=== НЕ УДАЛОСЬ (код {rc}) ===")
        say("  Частые причины:")
        say(f"   * ноутбук недоступен:  ping -c2 {host}")
        say("   * не включён ssh-сервер на ноутбуке: sudo apt install openssh-server")
        say(f"   * другой пользователь: python3 remote_collect.py --user ВАШ_ЛОГИН --host {host}")
        say("   * другой порт:         --port 2222")
        say(f"  Отчёт (если что-то успело прийти) сохранён: {out_local}")
        return rc

    # Шаг 5 (опционально): забрать образ карты
    if pull:
        say("")
        say("--- шаг 5: забираю образ карты с ноутбука (scp) ---")
        img_remote = args.img
        img_local = Path.cwd() / f"card-{host}-{time.strftime('%Y%m%d-%H%M%S')}.bin"
        try:
            res = subprocess.run(
                ["scp", *scp_opts.split(), f"{target}:{img_remote}", str(img_local)],
                capture_output=True, text=True,
            )
            if res.returncode == 0:
                size = img_local.stat().st_size if img_local.is_file() else 0
                say(f"    образ: {img_local} ({size} байт)")
                try:
                    md5 = subprocess.run(["md5sum", str(img_local)],
                                         capture_output=True, text=True).stdout.split()[0]
                    say(f"    {md5}")
                except (OSError, IndexError):
                    pass
            else:
                say(f"    не удалось забрать {img_remote} — он остался на ноутбуке,")
                say(f"    можно скопировать вручную: scp -P {port} {target}:{img_remote} .")
        except (OSError, subprocess.SubprocessError) as exc:
            say(f"    (ошибка scp: {exc})")

    say("=== ГОТОВО ===")
    try:
        lines = len(out_local.read_text(encoding="utf-8", errors="replace").splitlines())
        say(f"  отчёт: {out_local}   ({lines} строк)")
    except OSError:
        say(f"  отчёт: {out_local}")
    say("  на ноутбуке после уборки ничего не осталось")
    say("")
    say("  Пришлите этот файл в переписку. Главное место — раздел")
    say("  «7. ПАСПОРТ КАРТЫ»: есть кортежи/производитель = карта жива,")
    say("  сплошные 0xFF/0x00 = карта или контакт слота не отвечают.")
    return 0


if __name__ == "__main__":  # pragma: no cover - ручной запуск
    sys.exit(main())
