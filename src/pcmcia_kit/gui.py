#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Графический пульт: окно с кнопками и журналом работы.

Зачем окно, если есть меню в консоли: на Windows текст в консоли читается
плохо (кодировка, шрифт, окно закрывается), а нажать кнопку проще, чем
вспоминать ключ. Окно ничего не делает само — оно собирает команду и вызывает
те же функции, что и текстовый режим, поэтому поведение у них одно.

Главное правило: работа с картой идёт в отдельном потоке, а окно только
показывает, что происходит. Иначе на время чтения образа окно «зависает» и
Windows рисует поверх него «Программа не отвечает».

tkinter входит в стандартную поставку Python; если его нет (урезанная сборка
Linux), набор честно переходит на текстовое меню.
"""
import queue
import sys
import threading
import traceback

from . import __version__, console
from .config import (ACTIONS, DEFAULT_HOST, DEFAULT_PORT, DEFAULT_USER,
                     MENU_ITEMS, default_key, has_key, kit_dir as resolve_kit_dir)

# Пункты, которые меняют карту: подтверждаются отдельным вопросом.
DANGEROUS = ("write", "erase", "fwwrite")


def _tk():
    """Импортировать tkinter. Возвращает модуль или None."""
    try:
        import tkinter
        return tkinter
    except ImportError:
        return None


class PumpGUI:
    """Окно пульта: поля подключения, кнопки действий, журнал."""

    def __init__(self, argv=None):
        self.tk = _tk()
        self.argv = list(argv or [])
        self.journal = queue.Queue()
        self.busy = False
        self.kit_dir = resolve_kit_dir("")
        self._build()

    # --- сборка окна ------------------------------------------------------
    def _build(self):
        tk = self.tk
        self.root = tk.Tk()
        self.root.title(f"PCMCIA FLASH — пульт {__version__}")
        self.root.geometry("900x640")
        self.root.minsize(700, 480)

        top = tk.LabelFrame(self.root, text=" Подключение ", padx=8, pady=6)
        top.pack(fill="x", padx=10, pady=(10, 4))

        self.var_host = tk.StringVar(value=self._arg("host") or DEFAULT_HOST)
        self.var_user = tk.StringVar(value=self._arg("user") or DEFAULT_USER)
        self.var_port = tk.StringVar(value=self._arg("port") or str(DEFAULT_PORT))
        self.var_image = tk.StringVar(value=self._arg("image") or "")
        self.var_yes = tk.BooleanVar(value=False)

        tk.Label(top, text="Ноутбук:").grid(row=0, column=0, sticky="w")
        tk.Entry(top, textvariable=self.var_host, width=18).grid(row=0, column=1, sticky="w")
        tk.Label(top, text="Пользователь:").grid(row=0, column=2, sticky="w", padx=(10, 0))
        tk.Entry(top, textvariable=self.var_user, width=10).grid(row=0, column=3, sticky="w")
        tk.Label(top, text="Порт:").grid(row=0, column=4, sticky="w", padx=(10, 0))
        tk.Entry(top, textvariable=self.var_port, width=6).grid(row=0, column=5, sticky="w")

        tk.Label(top, text="Файл образа:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        tk.Entry(top, textvariable=self.var_image, width=52).grid(
            row=1, column=1, columnspan=4, sticky="we", pady=(6, 0))
        tk.Button(top, text="Выбрать...", command=self._choose_image).grid(
            row=1, column=5, sticky="w", pady=(6, 0))
        tk.Checkbutton(top, text="не спрашивать подтверждение (--yes)",
                       variable=self.var_yes).grid(row=2, column=0, columnspan=4,
                                                   sticky="w", pady=(6, 0))
        self.lbl_key = tk.Label(top, text="", anchor="w")
        self.lbl_key.grid(row=2, column=4, columnspan=2, sticky="we", pady=(6, 0))
        self._refresh_key_label()

        mid = tk.LabelFrame(self.root, text=" Действия ", padx=8, pady=6)
        mid.pack(fill="x", padx=10, pady=4)
        for index, (_number, action, title) in enumerate(MENU_ITEMS):
            row, column = divmod(index, 3)
            tk.Button(mid, text=self._short(title), width=32, anchor="w",
                      command=lambda a=action, t=title: self.start(a, t)).grid(
                row=row, column=column, padx=3, pady=3, sticky="we")
        tk.Button(mid, text="Выход", width=32, anchor="w",
                  command=self.root.destroy).grid(
            row=(len(MENU_ITEMS) // 3) + 1, column=2, padx=3, pady=3, sticky="we")

        bottom = tk.LabelFrame(self.root, text=" Журнал ", padx=6, pady=6)
        bottom.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.text = tk.Text(bottom, wrap="word", height=18, state="disabled")
        scroll = tk.Scrollbar(bottom, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.status = tk.Label(self.root, text="готов", anchor="w", padx=10)
        self.status.pack(fill="x")

        self.text.tag_configure("bad", foreground="#a00000")
        self.text.tag_configure("ok", foreground="#0a7a0a")
        self.root.after(120, self._drain)

    def _arg(self, name):
        """Значение --имя из аргументов запуска (если пульт открыт из строки)."""
        for index, item in enumerate(self.argv):
            if item == f"--{name}" and index + 1 < len(self.argv):
                return self.argv[index + 1]
        return ""

    def _short(self, title):
        """Короткая подпись кнопки: то, что до первой скобки."""
        return title.split("(")[0].strip()

    def _refresh_key_label(self):
        key = default_key()
        if has_key(key):
            self.lbl_key.configure(text="вход по ключу: есть", fg="#0a7a0a")
        else:
            self.lbl_key.configure(text="вход по паролю (ключ: пункт «Настроить вход»)")

    def _choose_image(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Файл образа карты",
            filetypes=[("Образ карты", "*.bin *.img"), ("Все файлы", "*.*")])
        if path:
            self.var_image.set(path)

    # --- журнал -----------------------------------------------------------
    def log(self, line, tag=None):
        """Дописать строку в журнал (можно из другого потока)."""
        self.journal.put((line, tag))

    def _drain(self):
        """Показать в окне всё, что накопилось в очереди сообщений."""
        try:
            while True:
                line, tag = self.journal.get_nowait()
                self.text.configure(state="normal")
                self.text.insert("end", line + "\n", tag or ())
                self.text.see("end")
                self.text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    # --- запуск команды ---------------------------------------------------
    def start(self, action, title):
        """Запустить действие в отдельном потоке."""
        if self.busy:
            self.log("Дождитесь окончания текущей работы.", "bad")
            return
        if action in DANGEROUS and not self.var_yes.get():
            if not self._confirm(action, title):
                self.log("Отменено: карта не тронута.")
                return
        if action in ("write", "verify", "fwwrite") and not self.var_image.get():
            self._choose_image()
            if not self.var_image.get():
                self.log("Файл образа не выбран — действие отменено.", "bad")
                return
        self.busy = True
        self.status.configure(text=f"выполняется: {self._short(title)}")
        thread = threading.Thread(target=self._worker, args=(action, title), daemon=True)
        thread.start()

    def _confirm(self, action, title):
        from tkinter import messagebox
        words = ("Стереть ВСЁ содержимое карты?\n\nПаспорт карты останется на месте."
                 if action == "erase" else
                 "Записать образ на карту? Старое содержимое будет перезаписано.")
        return messagebox.askyesno("Подтверждение", words + f"\n\nПункт: {title}")

    def _worker(self, action, title):
        """Работа с картой: выполняется вне потока окна."""
        code = 0
        try:
            code = self._call(action)
        except Exception as error:                  # окно не должно падать
            self.log(f"ОШИБКА: {error}", "bad")
            self.log(traceback.format_exc(limit=3))
            code = 1
        finally:
            self.log("", None)
            if code == 0:
                self.log(f"Готово: {self._short(title)}", "ok")
            else:
                self.log(f"Завершено с кодом {code}: {self._short(title)}", "bad")
            self.busy = False
            self.status.configure(text="готов")

    def _call(self, action):
        """Вызвать нужную команду с теми же аргументами, что у текстового режима."""
        from .cli import main as cli_main
        argv = [action,
                "--host", self.var_host.get().strip(),
                "--user", self.var_user.get().strip(),
                "--port", self.var_port.get().strip() or str(DEFAULT_PORT)]
        if self.var_image.get().strip():
            argv += ["--image", self.var_image.get().strip()]
        if self.var_yes.get():
            argv.append("--yes")
        return cli_main(argv)

    def run(self):
        """Показать окно и работать до закрытия."""
        self.log(f"Набор: {self.kit_dir}")
        self.log(f"Версия: {__version__}; доступные действия: {', '.join(ACTIONS)}")
        self.log("Начните с «Паспорт и вердикт»: он ничего не меняет на карте.")
        self.root.mainloop()
        return 0


def main(argv=None):
    """Точка входа графического пульта. None — если графики нет."""
    if _tk() is None:
        console.warn("tkinter недоступен — графический пульт не запускается")
        return None
    console.enable_utf8()
    PumpGUI(argv).run()
    return 0


if __name__ == "__main__":              # pragma: no cover - ручной запуск
    sys.exit(main() or 0)
