#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вход по ключу: настроить один раз, дальше работать без пароля.

Пароль спрашивается здесь ОДИН раз — его спрашивает сам ssh; никуда не
сохраняется, в файлы и в командную строку не попадает.

    python -m pcmcia_kit key
    python -m pcmcia_kit key --host 192.168.0.71 --user root --port 2222

Что делает:
  1) если ключа ~/.ssh/pcmcia_key нет — создаёт его без пароля;
  2) отправляет открытую часть на ноутбук и дописывает в authorized_keys;
  3) проверяет вход по ключу и печатает готовую команду запуска.

Ничего не удаляет и не перезаписывает: если годный ключ уже есть, берётся он.
"""
import os
import shutil
import subprocess
import tempfile
import time

from . import console
from .config import default_key, has_key
from .transport import Target

# Скрипт, который дописывает ключ на ноутбуке. Отдельным файлом, а не одной
# длинной командой: так не нужно экранировать кавычки и спецсимволы в двух
# оболочках сразу (на этом ломались прежние попытки в PowerShell).
PROGRAMS = ("ssh", "scp", "ssh-keygen")


def computer_name():
    """Имя этой машины для комментария в ключе (Windows или Linux)."""
    name = os.environ.get("COMPUTERNAME")
    if not name:
        try:
            name = os.uname().nodename            # только на Unix
        except AttributeError:
            name = os.environ.get("HOSTNAME", "windows")
    return name or "windows"


def have_programs():
    """Проверить наличие ssh, scp и ssh-keygen."""
    missing = [name for name in PROGRAMS if not shutil.which(name)]
    if not missing:
        return True
    console.bad("нужны ssh, scp и ssh-keygen (входят в клиент OpenSSH).")
    console.say("  В Windows 10/11 клиент уже есть; если его нет, от администратора:")
    console.say("    Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0")
    console.say("  Нет: " + ", ".join(missing))
    return False


def key_is_usable(ssh_keygen, key_file):
    """Проверить, что ключ существует и не защищён паролем.

    `ssh-keygen -y -P ""` печатает открытую часть только у ключа без пароля.
    Ключ с паролем не годится: он спрашивал бы пароль на каждом шаге.
    """
    result = subprocess.run([ssh_keygen, "-y", "-P", "", "-f", key_file],
                            capture_output=True, text=True)
    return result.returncode == 0


def create_key(ssh_keygen, key_file, comment):
    """Создать ключ без пароля: сначала ed25519, затем rsa 2048 как запасной.

    В Python пустой аргумент доезжает до программы нормально, поэтому
    «кавычки-символы» и запуск через cmd.exe, которые требовались в
    PowerShell, здесь не нужны.
    """
    for extra in (["-t", "ed25519"], ["-t", "rsa", "-b", "2048"]):
        argv = [ssh_keygen, "-q"] + extra + ["-f", key_file, "-N", "", "-C", comment]
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode == 0 and os.path.isfile(key_file):
            return True, extra[1]
    return False, ""


def public_key_text(ssh_keygen, key_file):
    """Открытая часть ключа одной строкой."""
    pub = key_file + ".pub"
    if not os.path.isfile(pub):
        result = subprocess.run([ssh_keygen, "-y", "-f", key_file],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return ""
        with open(pub, "w", encoding="ascii") as handle:
            handle.write(result.stdout.strip() + "\n")
    with open(pub, encoding="ascii") as handle:
        return handle.read().strip()


def install_key(target, key_text):
    """Положить открытую часть в authorized_keys на ноутбуке.

    Возвращает (результат, текст): результат — KEY_ADDED, KEY_PRESENT,
    KEY_EMPTY или пустая строка при отказе.
    """
    if not key_text or not key_text.strip():
        return "KEY_EMPTY", ""

    work = tempfile.mkdtemp(prefix="pcmcia-addkey-")
    try:
        pub_local = os.path.join(work, "pcmcia_pub_key.txt")
        # Переводы строк обязательно LF.
        with open(pub_local, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(key_text.strip() + "\n")
        code, text = target.copy_to([pub_local], target.address + ":")
        if code != 0:
            return "", text

        # Создать .ssh и authorized_keys, если их нет.
        code, text = target.run(
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
        )
        if code != 0:
            return "", text

        # Ключ уже есть? grepping по точному совпадению whole-line из файла.
        code, text = target.run(
            "grep -qxF -f pcmcia_pub_key.txt ~/.ssh/authorized_keys "
            "&& echo KEY_PRESENT || echo KEY_ABSENT"
        )
        if "KEY_PRESENT" in text:
            return "KEY_PRESENT", text

        # Добавить ключ в конец authorized_keys.
        code, text = target.run(
            "cat pcmcia_pub_key.txt >> ~/.ssh/authorized_keys && echo KEY_ADDED"
        )
        if "KEY_ADDED" in text:
            return "KEY_ADDED", text
        return "", text
    finally:
        shutil.rmtree(work, ignore_errors=True)


def explain_failure(text):
    """Объяснить по-русски, почему ключ не добавился."""
    if "Permission denied (publickey" in text:
        console.say("  * ноутбук не принимает вход ПО ПАРОЛЮ (PasswordAuthentication no).")
        console.say("    На время добавления ключа разрешите пароль:")
        console.say("      printf 'PasswordAuthentication yes\\n' | "
                    "sudo tee /etc/ssh/sshd_config.d/99-temp.conf")
        console.say("      sudo systemctl restart ssh")
        console.say("    (после добавления ключа этот файл можно удалить)")
    else:
        console.say("  * пароль не подошёл (раскладка, Caps Lock) — запустите снова;")
        console.say("  * вход root запрещён — укажите своего пользователя: --user ЛОГИН;")
        console.say("  * ноутбук не отвечает — проверьте адрес и что sshd запущен.")
    console.say("  Ничего не изменено: на карту не писали.")


def setup(host, user, port, key_file=""):
    """Настроить вход по ключу. Возвращает код возврата."""
    if not have_programs():
        return 2

    key_file = os.path.abspath(os.path.expanduser(key_file or default_key()))
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    target = Target(host, user, port, key_file)

    console.say("=== ключ доступа к ноутбуку ===")
    console.say(f"  ноутбук : {target.address} (порт {target.port})")
    console.say(f"  ключ    : {key_file}")

    ssh_keygen = shutil.which("ssh-keygen")
    comment = "pcmcia-kit@" + computer_name()

    if has_key(key_file):
        if key_is_usable(ssh_keygen, key_file):
            console.say("  ключ уже есть, без пароля — новый не создаю (использую этот)")
        else:
            console.say("  ключ есть, но с паролем — пересоздаю без пароля")
            backup = key_file + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
            os.replace(key_file, backup)
            if os.path.isfile(key_file + ".pub"):
                os.replace(key_file + ".pub", backup + ".pub")
            console.say(f"  старый ключ отложен: {backup}")

    if not has_key(key_file):
        console.say("  создаю ключ без пароля (он нужен только для этих запусков)")
        created, kind = create_key(ssh_keygen, key_file, comment)
        if not created:
            console.bad("ssh-keygen не смог создать ключ")
            console.say("  Сделайте это одной строкой и запустите снова:")
            console.say(f'    ssh-keygen -q -t ed25519 -f "{key_file}" -N "" -C "{comment}"')
            return 2
        console.say(f"  создан ключ {kind}")

    key_text = public_key_text(ssh_keygen, key_file)
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
        explain_failure(text)
        return 2

    console.say("--- проверяю вход по ключу (пароля быть не должно) ---")
    probe = [target.ssh] + target.ssh_options() + [
        "-o", "BatchMode=yes", target.address, "echo KEY_OK; id -un"]
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
    console.say(f"    python -m pcmcia_kit menu --host {host}")
    if port != 22:
        console.say(f"    (порт: добавьте --port {port})")
    console.say("")
    console.say(f"  Ключ лежит здесь: {key_file}")
    console.say("  Убрать доступ: на ноутбуке удалите из ~/.ssh/authorized_keys строку,")
    console.say(f"  в которой есть {comment} — больше эта машина входить не сможет")
    return 0
