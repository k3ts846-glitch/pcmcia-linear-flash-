#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вход по ключу: настроить один раз, дальше работать без пароля.

Заменяет add_key.ps1. Пароль спрашивается здесь ОДИН раз — его спрашивает сам
ssh; никуда не сохраняется, в файлы и в командную строку не попадает.

    python add_key.py                          адрес по умолчанию
    python add_key.py --host 192.168.0.71
    python add_key.py --host 192.168.0.71 --user root --port 2222

Что делает:
  1) если ключа ~/.ssh/pcmcia_key нет — создаёт его без пароля;
  2) отправляет открытую часть на ноутбук и дописывает в authorized_keys;
  3) проверяет вход по ключу и печатает готовую команду запуска.

Ничего не удаляет и не перезаписывает: если годный ключ уже есть, берётся он.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from kitlib import console                                   # noqa: E402
from kitlib.transport import Target                           # noqa: E402

DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "pcmcia_key")

# Скрипт, который дописывает ключ на ноутбуке. Отдельным файлом, а не одной
# длинной командой: так не нужно экранировать кавычки и спецсимволы в двух
# оболочках сразу (на этом ломались прежние попытки в PowerShell).
ADD_KEY_SCRIPT = """#!/bin/bash
# Дописывает открытый ключ в ~/.ssh/authorized_keys без дублей.
set -u
PUB="$HOME/pcmcia_pub_key.txt"
AK="$HOME/.ssh/authorized_keys"
KEY=$(head -n1 "$PUB" 2>/dev/null | tr -d '\\r')
if [ -z "$KEY" ]; then echo "KEY_EMPTY"; rm -f "$PUB" "$0"; exit 1; fi
mkdir -p "$HOME/.ssh" 2>/dev/null && chmod 700 "$HOME/.ssh" 2>/dev/null
touch "$AK" 2>/dev/null && chmod 600 "$AK" 2>/dev/null
if grep -qxF "$KEY" "$AK" 2>/dev/null; then
  echo "KEY_PRESENT"
else
  printf '%s\\n' "$KEY" >> "$AK" && echo "KEY_ADDED"
fi
rm -f "$PUB" "$0"
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="add_key.py",
        description="Настроить вход по ключу на ноутбук (пароль спрашивается один раз).")
    parser.add_argument("--host", "-H", default="192.168.0.94", help="адрес ноутбука")
    parser.add_argument("--user", "-u", default="root", help="пользователь ssh")
    parser.add_argument("--port", "-p", type=int, default=22, help="порт ssh")
    parser.add_argument("--key", "-k", default=DEFAULT_KEY, help="файл ключа")
    return parser


def have_programs():
    """Проверить наличие ssh, scp и ssh-keygen."""
    missing = [name for name in ("ssh", "scp", "ssh-keygen") if not shutil.which(name)]
    if missing:
        console.bad("нужны ssh, scp и ssh-keygen (входят в клиент OpenSSH).")
        console.say("  В PowerShell от администратора:")
        console.say("    Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0")
        console.say("  Нет: " + ", ".join(missing))
        return False
    return True


def computer_name():
    """Имя этой машины для комментария в ключе (Windows или Linux)."""
    name = os.environ.get("COMPUTERNAME")
    if not name:
        try:
            name = os.uname().nodename        # только на Unix
        except AttributeError:
            name = os.environ.get("HOSTNAME", "windows")
    return name or "windows"


def key_is_usable(kg, key_file):
    """Проверить, что ключ существует и не защищён паролем.

    `ssh-keygen -y -P ""` печатает открытую часть только у ключа без пароля.
    Ключ с паролем нам не годится: он спрашивал бы пароль на каждом шаге.
    """
    result = subprocess.run([kg, "-y", "-P", "", "-f", key_file],
                            capture_output=True, text=True)
    return result.returncode == 0


def create_key(kg, key_file, comment):
    """Создать ключ без пароля. Сначала ed25519, затем rsa 2048 как запасной.

    В Python пустой аргумент доезжает до программы нормально, поэтому
    «кавычки-символы» и запуск через cmd, которые требовались в PowerShell,
    здесь не нужны.
    """
    for extra in (["-t", "ed25519"], ["-t", "rsa", "-b", "2048"]):
        argv = [kg, "-q"] + extra + ["-f", key_file, "-N", "", "-C", comment]
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode == 0 and os.path.isfile(key_file):
            return True, extra[1]
    return False, ""


def public_key_text(kg, key_file):
    """Открытая часть ключа (одна строка)."""
    pub = key_file + ".pub"
    if not os.path.isfile(pub):
        result = subprocess.run([kg, "-y", "-f", key_file], capture_output=True,
                                text=True)
        if result.returncode != 0:
            return ""
        with open(pub, "w", encoding="ascii") as handle:
            handle.write(result.stdout.strip() + "\n")
    with open(pub, encoding="ascii") as handle:
        return handle.read().strip()


def install_key(target, key_text):
    """Положить открытую часть в authorized_keys на ноутбуке.

    Возвращает строку-результат ноутбука: KEY_ADDED, KEY_PRESENT или пусто.
    """
    work = tempfile.mkdtemp(prefix="pcmcia-addkey-")
    try:
        pub_local = os.path.join(work, "pcmcia_pub_key.txt")
        sh_local = os.path.join(work, "pcmcia_addkey.sh")
        # Переводы строк обязательно LF: bash на ноутбуке не примет CRLF.
        with open(pub_local, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(key_text + "\n")
        with open(sh_local, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(ADD_KEY_SCRIPT)
        code, text = target.copy_to([pub_local, sh_local], target.address + ":")
        if code != 0:
            return "", text
        code, text = target.run("bash pcmcia_addkey.sh")
        for marker in ("KEY_ADDED", "KEY_PRESENT", "KEY_EMPTY"):
            if marker in text:
                return marker, text
        return "", text
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    console.enable_utf8()
    console.enable_color()
    args = build_parser().parse_args(argv)
    if not have_programs():
        return 2

    key_file = os.path.abspath(os.path.expanduser(args.key))
    key_dir = os.path.dirname(key_file)
    os.makedirs(key_dir, exist_ok=True)
    target = Target(args.host, args.user, args.port, key_file)

    console.say("=== ключ доступа к ноутбуку ===")
    console.say(f"  ноутбук : {target.address} (порт {target.port})")
    console.say(f"  ключ    : {key_file}")

    kg = shutil.which("ssh-keygen")
    comment = "pcmcia-kit@" + computer_name()

    if os.path.isfile(key_file):
        if key_is_usable(kg, key_file):
            console.say("  ключ уже есть, без пароля — новый не создаю (использую этот)")
        else:
            console.say("  ключ есть, но с паролем — пересоздаю без пароля")
            backup = key_file + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
            os.replace(key_file, backup)
            if os.path.isfile(key_file + ".pub"):
                os.replace(key_file + ".pub", backup + ".pub")
            console.say(f"  старый ключ отложен: {backup}")
    if not os.path.isfile(key_file):
        console.say("  создаю ключ без пароля (он нужен только для этих запусков)")
        created, kind = create_key(kg, key_file, comment)
        if not created:
            console.bad("ssh-keygen не смог создать ключ")
            console.say("  Сделайте это одной строкой и запустите меня снова:")
            console.say(f'    ssh-keygen -q -t ed25519 -f "{key_file}" -N "" -C "{comment}"')
            return 2
        console.say(f"  создан ключ {kind}")

    key_text = public_key_text(kg, key_file)
    if not key_text:
        console.bad("не удалось прочитать открытую часть ключа")
        return 2

    console.say("")
    console.say("Сейчас ssh спросит пароль ОДИН раз — чтобы добавить ключ на ноутбук.")
    console.say("Пароль никуда не сохраняется.")
    console.say("")
    result, text = install_key(target, key_text)
    if result == "KEY_ADDED":
        console.ok("ключ добавлен в ~/.ssh/authorized_keys")
    elif result == "KEY_PRESENT":
        console.ok("такой ключ там уже был — второй раз не добавляю")
    elif result == "KEY_EMPTY":
        console.bad("файл ключа приехал пустым — повторите запуск")
        return 2
    else:
        console.bad("не получилось добавить ключ")
        for line in text.splitlines():
            if line.strip():
                console.say("  ноутбук: " + line.strip())
        if "Permission denied (publickey" in text:
            console.say("  * ноутбук не принимает вход ПО ПАРОЛЮ (PasswordAuthentication no).")
            console.say("    На время добавления ключа разрешите пароль:")
            console.say("      printf 'PasswordAuthentication yes\\n' | sudo tee /etc/ssh/sshd_config.d/99-temp.conf")
            console.say("      sudo systemctl restart ssh")
        else:
            console.say("  * пароль не подошёл (раскладка, Caps Lock) — запустите снова;")
            console.say("  * вход root запрещён — укажите своего пользователя: --user ЛОГИН;")
            console.say("  * ноутбук не отвечает — проверьте адрес и что sshd запущен.")
        console.say("  Ничего не изменено: на карту не писали.")
        return 2

    console.say("--- проверяю вход по ключу (пароля быть не должно) ---")
    key_target = Target(args.host, args.user, args.port, key_file)
    probe = [key_target.ssh] + key_target.ssh_options() + [
        "-o", "BatchMode=yes", key_target.address, "echo KEY_OK; id -un"]
    result = subprocess.run(probe, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    if "KEY_OK" in output:
        console.ok("вход по ключу работает")
    else:
        for line in output.splitlines():
            if line.strip():
                console.say("  " + line.strip())
        console.say("  вход по ключу не подтвердился — работайте с паролем (он спросится по шагам)")
        return 3

    console.say("")
    console.say("=== ГОТОВО ===")
    console.say("  Дальше запускайте так (пароль больше не нужен):")
    console.say(f"    python pcmcia_win.py menu --host {args.host}")
    if args.port != 22:
        console.say(f"    (порт: добавьте --port {args.port})")
    console.say("")
    console.say(f"  Ключ лежит здесь: {key_file}")
    console.say("  Убрать доступ: на ноутбуке удалите из ~/.ssh/authorized_keys строку,")
    console.say(f"  в которой есть {comment} — больше эта машина входить не сможет")
    return 0


if __name__ == "__main__":
    sys.exit(main())
