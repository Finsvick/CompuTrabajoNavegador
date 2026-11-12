"""Interfaz de escritorio de compuapply (CustomTkinter). La lógica está en ui_logic; las acciones largas corren la CLI."""
import argparse
import os
import queue
import sqlite3
import threading
import tkinter
import traceback
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
import openai

from compuapply import batch, browser, cv, db, pending, profile, review, schedule, score, setup, ui_logic
from compuapply.ui_logic import ENV, LABELS, PROFILE

PROJECT = Path(__file__).resolve().parent.parent  # la carpeta donde vive Iniciar.bat
GREEN = ("#1a7f37", "#3fb950")
RED = ("#c62828", "#ff6b6b")
GREY = ("gray35", "gray65")
POLL_MS = 200
FORCE_AFTER_MS = 30_000  # si 30 s después de pedir el freno el proceso sigue vivo, se ofrece cerrarlo a la fuerza
CALIBRATE_LIMIT = 100  # ofertas que se cargan por tanda en Calibrar; al terminarla se carga la siguiente
NO_CANDIDATES = "No hay ofertas puntuadas sin etiquetar. Corré una corrida o puntuá pendientes"
BIAS_NOTICE = "Si todavía no calibraste, etiquetá primero: ver los puntajes antes sesga tus respuestas"
TITLES = {"run": "Corrida completa (modo seco)", "score": "Puntuar pendientes", "apply": "Plan de postulación",
          "apply_live": "Postular (en vivo)", "login": "Iniciar sesión", "session_check": "Verificar sesión",
          "batch": "Postular la tanda", "batch_dry": "Plan de la tanda"}
ANSWER_LABELS = {"salary": "Sueldo pretendido", "availability": "Disponibilidad", "modality": "Modalidad (una o más: presencial, híbrido, remoto)",
                 "start": "Cuándo podés empezar", "location": "Barrio y localidad donde vivís", "mobility": "Movilidad o transporte",
                 "salary_part_time": "Pretensión de media jornada", "salary_daily": "Pretensión por día"}
HINTS = {"threshold": "0 a 100", "daily_cap": "1 o más", "company_daily_cap": "1 o más; por defecto 1", "pages": "1 o más", "max_details": "0 o más",
         "score_limit": "0 o más", "max_cost": "mayor que 0", "max_cost_month": "mayor que 0"}
PROBE_BUTTON = "Probar el puntaje (menos de US$ 0,001)"
SCHEDULE_NOTE = ("Busca, puntúa y arma el plan en modo seco: no postula. La PC tiene que estar prendida y con tu sesión "
                 "de Windows abierta; si estaba apagada, corre apenas se prende.")
VARIANT_FIELDS = ("id", "resume", "keywords", "pdf")


@contextmanager
def _connection():
    """Conexión corta a la base: una por operación (con una corrida en curso, SQLite espera hasta 5 s)."""
    conn = db.connect(ui_logic.DB)
    try:
        yield conn
    finally:
        conn.close()


def _percent(value: float | None) -> str:
    """Porcentaje truncado (así un 80% mostrado siempre cumple >= 80), o `-` si no hay divisor."""
    return "-" if value is None else f"{int(value)}%"


def _local(iso: str) -> str:
    """Fecha y hora locales (`AAAA-MM-DD HH:MM`) de un instante ISO guardado en UTC."""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso or ""


class App(ctk.CTk):
    """Ventana con las pestañas Estado, Parámetros, Correr, Calibrar, Resultados, Banco y Preguntas. Una sola acción larga a la vez."""

    def __init__(self) -> None:
        super().__init__()
        self.title("compuapply")
        self.geometry("940x720")
        self.minsize(800, 620)
        self.proc = None  # subproceso en curso
        self.job = None  # {action, on_done, output, lines, eof, idle} de la acción en curso
        self.thread_busy = False  # hilo en curso (verificar la clave, crear el acceso directo)
        self.key_ok = False
        self.profile_ok = False
        self.probe_result = None  # (anduvo, texto) de la última prueba de puntaje; la escribe el hilo, la dibuja refresh_status
        self.report = None
        self.login_dialog = None
        self.login_chrome = False  # el diálogo de inicio de sesión espera a Chrome (True) o al Enter de Chromium (False)
        self.variant_rows = []
        self.cal_items = []  # ofertas de la tanda de Calibrar y cuál se está viendo
        self.cal_index = 0
        self.cal_suggested = None
        self.cal_model = None
        self.results_forced = False  # «Mostrar igual» ya tocado
        self.result_rows = {}  # id16 -> fila de la tabla de Resultados
        self.rep_paths = {}  # nombre -> ruta de cada informe de la lista
        self.bank_proposals = []  # las propuestas que muestra la tabla de Banco, en el orden de sus filas
        self.bank_items = []  # los hechos aprobados que muestra la tabla de Banco
        self.pending_rows = {}  # clave normalizada -> pregunta que muestra la tabla de Pendientes
        self.question_rows = {}  # clave normalizada -> pregunta pendiente que muestra la tabla de Preguntas
        self.events = queue.Queue()  # lo que mandan los hilos (nunca se toca un widget desde un hilo)
        self.shown = queue.Queue()  # lo mismo, para lo que solo se muestra y no ocupa a la ventana (el estado de la tarea)
        self._style_tables()
        self.tabs = ctk.CTkTabview(self, command=self._on_tab)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=12)
        for name in ("Estado", "Parámetros", "Correr", "Calibrar", "Resultados", "Banco", "Preguntas", "Pendientes"):
            self.tabs.add(name)
        self._build_status(self.tabs.tab("Estado"))
        self._build_params(self.tabs.tab("Parámetros"))
        self._build_run(self.tabs.tab("Correr"))
        self._build_calibrate(self.tabs.tab("Calibrar"))
        self._build_results(self.tabs.tab("Resultados"))
        self._build_bank(self.tabs.tab("Banco"))
        self._build_questions(self.tabs.tab("Preguntas"))
        self._build_pending(self.tabs.tab("Pendientes"))
        self.bind("<Key>", self._on_key)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.load_form()
        self.refresh_status()
        self.after(POLL_MS, self._poll)

    def report_callback_exception(self, exc, val, tb) -> None:
        """Sin consola (pythonw) los errores de un botón no se verían: van al registro."""
        self._log("Error interno de la interfaz: " + "".join(traceback.format_exception_only(exc, val)).strip())

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _say(label: ctk.CTkLabel, text: str, ok: bool | None = True) -> None:
        label.configure(text=text, text_color=GREY if ok is None else GREEN if ok else RED)

    @staticmethod
    def _mark(label: ctk.CTkLabel, ok: bool) -> None:
        label.configure(text="✔" if ok else "✘", text_color=GREEN if ok else RED)

    def _dialog(self, title: str) -> ctk.CTkToplevel:
        dialog = ctk.CTkToplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.after(150, dialog.lift)  # en Windows un CTkToplevel puede abrirse detrás de la ventana
        return dialog

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _apply_states(self) -> None:
        """Mientras corre una acción, el resto de los botones de acción quedan deshabilitados."""
        state = "disabled" if self.proc is not None or self.thread_busy else "normal"
        for button in (self.verify_button, self.login_button, self.run_button, self.score_button, self.apply_button,
                       self.live_button, self.key_button, self.shortcut_button, self.sched_set, self.sched_off,
                       self.batch_dry_button, self.batch_live_button):
            button.configure(state=state)
        # Probar el puntaje pide clave y perfil en ✔ (gasta menos de una milésima de dólar).
        self.probe_button.configure(state="normal" if state == "normal" and self.key_ok and self.profile_ok else "disabled")
        self.model_menu.configure(state=state)
        self._cv_buttons()
        self._bank_buttons()
        self.key_button.configure(text="Reemplazar clave" if self.key_ok else "Cargar clave")
        self.stop_button.configure(state="normal" if self.proc is not None else "disabled")

    # ── pestaña Estado ───────────────────────────────────────────────────────

    def _row(self, tab, row: int, button: str | None = None, command=None):
        mark = ctk.CTkLabel(tab, text="", width=28)
        mark.grid(row=row, column=0, padx=(4, 8), pady=8)
        text = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=520)
        text.grid(row=row, column=1, sticky="ew", pady=8)
        widget = None
        if button:
            widget = ctk.CTkButton(tab, text=button, width=140, command=command)
            widget.grid(row=row, column=2, padx=4, pady=8)
        return mark, text, widget

    def _build_status(self, tab) -> None:
        tab.grid_columnconfigure(1, weight=1)
        self.key_mark, self.key_text, self.key_button = self._row(tab, 0, "Cargar clave", self._ask_key)
        _, model_text, _ = self._row(tab, 1)
        model_text.configure(text="Modelo")
        self.model_menu = ctk.CTkOptionMenu(tab, values=list(score.PRICES), width=140, command=self._on_model)
        self.model_menu.grid(row=1, column=2, padx=4, pady=8)
        self.profile_mark, self.profile_text, _ = self._row(tab, 2, "Editar", lambda: self.tabs.set("Parámetros"))
        self.pdf_box = ctk.CTkFrame(tab, fg_color="transparent")
        self.pdf_box.grid(row=3, column=0, columnspan=4, sticky="ew")
        self.spend_mark, self.spend_text, _ = self._row(tab, 4)
        self.threshold_mark, self.threshold_text, _ = self._row(tab, 5)
        self.probe_mark, self.probe_text, self.probe_button = self._row(tab, 6, PROBE_BUTTON, self._probe)
        self.session_mark, self.session_text, self.verify_button = self._row(tab, 7, "Verificar", self._verify_session)
        self.login_button = ctk.CTkButton(tab, text="Iniciar sesión", width=140, command=self._login)
        self.login_button.grid(row=7, column=3, padx=4, pady=8)
        self.session_text.configure(text="Sesión de Computrabajo: sin verificar")
        self.status_msg = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=780)
        self.status_msg.grid(row=8, column=0, columnspan=4, sticky="ew", padx=8, pady=(16, 0))
        ctk.CTkButton(tab, text="Actualizar", width=140, command=self.refresh_status).grid(
            row=9, column=0, columnspan=2, sticky="w", padx=4, pady=12)
        self.shortcut_button = ctk.CTkButton(tab, text="Crear acceso directo en el escritorio", width=140,
                                             command=self._create_shortcut)
        self.shortcut_button.grid(row=9, column=2, columnspan=2, sticky="e", padx=4, pady=12)

    def refresh_status(self) -> None:
        """Vuelve a leer `.env` y `profile.yaml` y redibuja la pestaña Estado."""
        st = ui_logic.status(ENV, PROFILE)
        self.key_ok, self.profile_ok = st["key_ok"], st["profile_ok"]
        self._mark(self.key_mark, st["key_ok"])
        self.key_text.configure(text="Clave de OpenAI: " + ("cargada" if st["key_ok"] else "falta"))
        self.model_menu.set(st["model"])
        self._mark(self.profile_mark, st["profile_ok"])
        self.profile_text.configure(text=f"Perfil: {st['profile_msg']}")
        for child in self.pdf_box.winfo_children():
            child.destroy()
        for row, variant in enumerate(st["variants"]):
            mark = ctk.CTkLabel(self.pdf_box, text="", width=28)
            mark.grid(row=row, column=0, padx=(4, 8), pady=4)
            self._mark(mark, variant["pdf_ok"])
            ctk.CTkLabel(self.pdf_box, anchor="w", text=f"PDF de «{variant['id']}»: " + (
                "cargado" if variant["pdf_ok"] else "falta")).grid(row=row, column=1, sticky="w")
        if st["threshold"] is None:  # sin perfil que cargue no hay umbral que mostrar
            self.threshold_mark.configure(text="")
            self.threshold_text.configure(text="Umbral: sin perfil")
        else:
            self._mark(self.threshold_mark, st["threshold_note"] is None)
            self.threshold_text.configure(text=f"Umbral: {st['threshold']} " + (
                f"(calibrado con {st['calibrated_with']})" if st["threshold_note"] is None else f"· {st['threshold_note']}"))
        if self.probe_result is None:
            self.probe_mark.configure(text="")
            self.probe_text.configure(text="Prueba de puntaje: sin hacer")
        else:
            self._mark(self.probe_mark, self.probe_result[0])
            self.probe_text.configure(text="Prueba de puntaje: " + self.probe_result[1])
        spent = 0.0
        if ui_logic.DB.is_file():  # sin base todavía no hay gasto: no se crea una solo por mirar
            try:
                with _connection() as conn:
                    spent = ui_logic.spend(conn, datetime.now(timezone.utc).date())["month"]
            except sqlite3.Error:
                spent = None
        if spent is None:
            self.spend_mark.configure(text="")
            self.spend_text.configure(text="Gasto del mes: no se pudo leer la base")
        else:
            self._mark(self.spend_mark, spent < st["month_cap"])  # ✘ cuando ya se llegó al tope mensual
            self.spend_text.configure(text=f"Gasto del mes: {score.amounts(spent, st['month_cap'])}")
        self._apply_states()

    def _on_tab(self) -> None:
        tab = self.tabs.get()
        self.focus_set()  # que un campo de otra pestaña no se quede con las teclas (los atajos de Calibrar)
        if tab == "Estado":
            self.refresh_status()
        elif tab == "Correr":
            self._schedule_refresh()
            self._batch_load()
        elif tab == "Calibrar":
            self._calibrate_load()
        elif tab == "Resultados":
            self._results_load()
        elif tab == "Banco":
            self._bank_load()
        elif tab == "Preguntas":
            self._questions_load()
        elif tab == "Pendientes":
            self._pending_load()

    def _ask_key(self) -> None:
        replace = self.key_ok  # con la clave ✔ el botón dice «Reemplazar clave»
        dialog = self._dialog("Reemplazar la clave de OpenAI" if replace else "Clave de OpenAI")
        ctk.CTkLabel(dialog, wraplength=360, text="Pegá tu clave de OpenAI. No se muestra en pantalla ni en el registro.").pack(
            padx=20, pady=(20, 8))
        if replace:
            ctk.CTkLabel(dialog, wraplength=360, text="La clave actual se reemplaza solo si la nueva verifica bien.").pack(
                padx=20, pady=(0, 8))
        entry = ctk.CTkEntry(dialog, show="•", width=360)
        entry.pack(padx=20, pady=8)
        dialog.after(250, entry.focus_set)

        def accept(_event=None) -> None:
            value = entry.get()
            dialog.destroy()
            self._load_key(value, replace)

        entry.bind("<Return>", accept)
        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(pady=(8, 20))
        ctk.CTkButton(buttons, text="Guardar", width=120, command=accept).pack(side="left", padx=6)
        ctk.CTkButton(buttons, text="Cancelar", width=120, fg_color="gray40", command=dialog.destroy).pack(side="left", padx=6)

    def _load_key(self, value: str, replace: bool = False) -> None:
        """Verifica y guarda la clave en un hilo (la verificación llama a OpenAI); el resultado vuelve por `events`."""
        self.probe_result = None  # con otra clave la prueba anterior ya no vale
        self._in_thread("Verificando la clave con OpenAI…", lambda: setup.setup_env(ENV, ask=lambda _: value, replace=replace))

    def _in_thread(self, waiting: str, work, label: ctk.CTkLabel | None = None, then=None) -> None:
        """Corre `work()` en un hilo mientras el resto de las acciones esperan.

        Su mensaje o su error van a `label` (por defecto, el de Estado); al terminar se llama a `then`, si hay.
        """
        label = label or self.status_msg
        self.thread_busy = True
        self._say(label, waiting, None)
        self._apply_states()

        def target() -> None:
            try:
                message = work()
            except (RuntimeError, OSError) as e:
                self.events.put((label, False, str(e), then))
            except Exception as e:  # solo la clase: el texto de un error raro podría traer parte de la clave
                self.events.put((label, False, f"Falló con un error inesperado ({type(e).__name__}).", then))
            else:
                self.events.put((label, True, message, then))

        threading.Thread(target=target, daemon=True).start()

    def _probe(self) -> None:
        """Puntúa una oferta de ejemplo en un hilo (llama a OpenAI, por menos de US$ 0,001) y deja el resultado en Estado."""
        def work() -> str:
            try:
                info = ui_logic.run_probe(ENV, PROFILE, openai.OpenAI)
            except RuntimeError as e:
                self.probe_result = (False, str(e))
                raise
            self.probe_result = (True, ui_logic.probe_summary(info))
            return "Prueba de puntaje: " + self.probe_result[1]

        self._in_thread("Probando el puntaje con OpenAI…", work)

    def _create_shortcut(self) -> None:
        self._in_thread("Creando el acceso directo…", lambda: ui_logic.create_shortcut(
            ui_logic.desktop_dir(), PROJECT, ui_logic.pythonw_exe()))

    def _on_model(self, value: str) -> None:
        self.probe_result = None  # con otro modelo la prueba anterior ya no vale
        try:
            setup.set_model(ENV, value)
        except (RuntimeError, OSError) as e:
            self._say(self.status_msg, str(e), False)
        else:
            self._say(self.status_msg, f"Modelo guardado: {value}", True)
        self.refresh_status()

    def _session_result(self, ok: bool, text: str) -> None:
        self._mark(self.session_mark, ok)
        self.session_text.configure(text=f"Sesión de Computrabajo: {text}")

    def _verify_session(self) -> None:
        self._launch("session_check", on_done=lambda code: self._session_result(
            code == 0, "activa" if code == 0 else "vencida o con error (mirá el registro)"))

    def _login(self) -> None:
        if not self._launch("login", interactive=True, on_done=lambda code: self._session_result(
                code == 0, "iniciada" if code == 0 else "no se pudo iniciar (mirá el registro)")):
            return
        dialog = self._dialog("Iniciar sesión")
        dialog.protocol("WM_DELETE_WINDOW", self._login_cancel)
        self.login_chrome = browser.find_chrome() is not None
        ctk.CTkLabel(dialog, wraplength=360, text=browser.CHROME_PROMPT if self.login_chrome else
                     "Iniciá sesión en la ventana de Chromium y después tocá Ya inicié sesión.").pack(padx=20, pady=(20, 8))
        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(pady=(8, 20))
        ctk.CTkButton(buttons, text="Ya inicié sesión", width=150, command=self._login_continue).pack(side="left", padx=6)
        ctk.CTkButton(buttons, text="Cancelar", width=120, fg_color="gray40", command=self._login_cancel).pack(side="left", padx=6)
        self.login_dialog = dialog

    def _close_login_dialog(self) -> None:
        if self.login_dialog is not None:
            self.login_dialog.destroy()
            self.login_dialog = None

    def _login_continue(self) -> None:
        """Con Chrome, `login` espera a que se cierre la ventana: si el diálogo sigue abierto, Chrome también, y se le pide que la
        cierre. Con Chromium es el Enter que espera `login`: se lo escribimos en el stdin del subproceso."""
        if self.login_chrome:
            self._log("Chrome sigue abierto: cerrá esa ventana de Chrome. El programa verifica la sesión apenas se cierre.")
            return
        try:
            self.proc.stdin.write("\n")
            self.proc.stdin.flush()
            self.proc.stdin.close()
        except (OSError, ValueError):
            self._log("El inicio de sesión ya había terminado.")
        self._close_login_dialog()

    def _login_cancel(self) -> None:
        if self.proc is not None:
            ui_logic.kill_tree(self.proc.pid)
            self._log("Inicio de sesión cancelado.")
        self._close_login_dialog()

    # ── pestaña Parámetros ───────────────────────────────────────────────────

    def _build_params(self, tab) -> None:
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", pady=(8, 0))
        ctk.CTkButton(bottom, text="Guardar", width=120, command=self._save).grid(row=0, column=0, padx=4)
        ctk.CTkButton(bottom, text="Recargar", width=120, fg_color="gray40", command=self.load_form).grid(row=0, column=1, padx=4)
        self.create_button = ctk.CTkButton(bottom, text="Crear desde una carpeta de CV", command=self._create_from_folder)
        self.create_button.grid(row=0, column=2, padx=4)
        self.params_msg = ctk.CTkLabel(bottom, text="", anchor="w", justify="left", wraplength=780)
        self.params_msg.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=(6, 0))
        form = ctk.CTkScrollableFrame(tab)
        form.pack(fill="both", expand=True)
        bold = ctk.CTkFont(weight="bold")
        ctk.CTkLabel(form, text="Variantes de CV (de 1 a 3)", font=bold).pack(anchor="w", pady=(4, 0))
        self.variants_box = ctk.CTkFrame(form, fg_color="transparent")
        self.variants_box.pack(fill="x")
        self.add_button = ctk.CTkButton(form, text="Agregar variante", width=160, command=self._add_variant)
        self.add_button.pack(anchor="w", pady=6)
        self.exclude_companies = self._textbox(form, "Empresas excluidas (una por línea)")
        self.exclude_words = self._textbox(form, "Palabras del título que excluyen la oferta (una por línea)")
        self.zones_box = self._textbox(form, "Zonas (provincias como las muestra Computrabajo, una por línea; vacío = todas)")
        self.fields = {}
        for title, keys in (("Postulación", ("threshold", "daily_cap", "company_daily_cap")),
                            ("Corrida (run)", ("pages", "max_details", "score_limit", "max_cost", "max_cost_month"))):
            ctk.CTkLabel(form, text=title, font=bold).pack(anchor="w", pady=(12, 0))
            grid = ctk.CTkFrame(form, fg_color="transparent")
            grid.pack(fill="x")
            for row, key in enumerate(keys):
                ctk.CTkLabel(grid, text=f"{LABELS[key]} ({HINTS[key]})", anchor="w").grid(row=row, column=0, sticky="w", pady=3)
                self.fields[key] = ctk.CTkEntry(grid, width=90)
                self.fields[key].grid(row=row, column=1, padx=12, pady=3)
        self.review_check = ctk.CTkCheckBox(form, text="Revisar cada CV antes de subirlo")
        self.review_check.pack(anchor="w", pady=(10, 0))
        ctk.CTkLabel(form, anchor="w", justify="left", wraplength=780, text_color=GREY,
                     text="Mientras esté marcada, «Postular (en vivo)» no sube ningún CV a medida: revisá los de data/cvs/ (cada uno "
                          "tiene su revisión) y desmarcala cuando quieras que el programa los suba solo.").pack(anchor="w")
        ctk.CTkLabel(form, text="Respuestas", font=bold).pack(anchor="w", pady=(12, 0))
        ctk.CTkLabel(form, anchor="w", justify="left", wraplength=780, text_color=GREY,
                     text="Con estos datos el programa contesta las preguntas de selección al postular. Lo que dejes vacío, "
                          "el programa no lo inventa: la oferta queda esperando en la pestaña Preguntas.").pack(anchor="w")
        grid = ctk.CTkFrame(form, fg_color="transparent")
        grid.pack(fill="x")
        self.answer_fields = {}
        for row, key in enumerate(profile.ANSWER_KEYS):
            ctk.CTkLabel(grid, text=ANSWER_LABELS[key], anchor="w").grid(row=row, column=0, sticky="w", pady=3)
            self.answer_fields[key] = ctk.CTkEntry(grid, width=360)
            self.answer_fields[key].grid(row=row, column=1, padx=12, pady=3)

    @staticmethod
    def _textbox(parent, title: str) -> ctk.CTkTextbox:
        ctk.CTkLabel(parent, text=title).pack(anchor="w", pady=(12, 0))
        box = ctk.CTkTextbox(parent, height=70)
        box.pack(fill="x")
        return box

    def _add_variant(self, values: dict | None = None) -> None:
        values = values or dict.fromkeys(VARIANT_FIELDS, "")
        box = ctk.CTkFrame(self.variants_box)
        box.pack(fill="x", pady=6)
        box.grid_columnconfigure(1, weight=1)
        row = {"box": box}
        remove = ctk.CTkButton(box, text="Quitar", width=90, fg_color="gray40", command=lambda: self._remove_variant(row))
        remove.grid(row=0, column=2, padx=8, pady=(8, 0))
        row["remove"] = remove
        for n, (key, title) in enumerate((("id", "Nombre corto"), ("resume", "CV (.yaml)"), ("keywords", "Keywords (con coma)"),
                                          ("pdf", "PDF (opcional)")), start=1):
            ctk.CTkLabel(box, text=title, anchor="w").grid(row=n, column=0, sticky="w", padx=8, pady=3)
            row[key] = ctk.CTkEntry(box)
            row[key].grid(row=n, column=1, sticky="ew", pady=3)
            row[key].insert(0, values[key])
        for key, title, kinds in (("resume", "Elegir CV", [("YAML", "*.yaml *.yml"), ("Todos", "*.*")]),
                                  ("pdf", "Elegir PDF", [("PDF", "*.pdf")])):
            ctk.CTkButton(box, text="Elegir…", width=90, command=lambda e=row[key], t=title, k=kinds: self._choose(e, t, k)).grid(
                row=2 if key == "resume" else 4, column=2, padx=8)
        self.variant_rows.append(row)
        self._update_variant_buttons()

    def _remove_variant(self, row: dict) -> None:
        row["box"].destroy()
        self.variant_rows.remove(row)
        self._update_variant_buttons()

    def _update_variant_buttons(self) -> None:
        self.add_button.configure(state="normal" if len(self.variant_rows) < profile.MAX_VARIANTS else "disabled")
        for row in self.variant_rows:
            row["remove"].configure(state="normal" if len(self.variant_rows) > 1 else "disabled")

    @staticmethod
    def _choose(entry: ctk.CTkEntry, title: str, kinds: list) -> None:
        chosen = filedialog.askopenfilename(title=title, filetypes=kinds)
        if chosen:
            entry.delete(0, "end")
            entry.insert(0, os.path.normpath(chosen))

    def _read_form(self) -> dict:
        return {
            "variants": [{key: row[key].get() for key in VARIANT_FIELDS} for row in self.variant_rows],
            "exclude_companies": self.exclude_companies.get("1.0", "end"),
            "exclude_title_words": self.exclude_words.get("1.0", "end"),
            "zones": self.zones_box.get("1.0", "end"),
            "apply": {key: self.fields[key].get() for key in ("threshold", "daily_cap", "company_daily_cap")},
            "run": {key: self.fields[key].get() for key in ("pages", "max_details", "score_limit", "max_cost", "max_cost_month")},
            "answers": {key: self.answer_fields[key].get() for key in profile.ANSWER_KEYS},
            "review_cv": "true" if self.review_check.get() else "false",
        }

    def _fill_form(self, form: dict) -> None:
        for row in self.variant_rows:
            row["box"].destroy()
        self.variant_rows.clear()
        for values in form["variants"]:
            self._add_variant(values)
        for box, text in ((self.exclude_companies, form["exclude_companies"]), (self.exclude_words, form["exclude_title_words"]),
                          (self.zones_box, form["zones"])):
            box.delete("1.0", "end")
            box.insert("1.0", text)
        for section in ("apply", "run"):
            for key, value in form[section].items():
                self.fields[key].delete(0, "end")
                self.fields[key].insert(0, value)
        for key, value in form["answers"].items():
            self.answer_fields[key].delete(0, "end")
            self.answer_fields[key].insert(0, value)
        (self.review_check.deselect if form["review_cv"] == "false" else self.review_check.select)()  # sin declarar vale «revisar»

    def load_form(self) -> None:
        """Carga el formulario desde `profile.yaml` (o vacío si no existe)."""
        try:
            form, error = ui_logic.profile_to_form(PROFILE), ""
        except RuntimeError as e:
            form, error = ui_logic.empty_form(), str(e)
        self._fill_form(form)
        if PROFILE.is_file():
            self.create_button.grid_remove()
        else:
            self.create_button.grid()
        self._say(self.params_msg, error, False)

    def _save(self) -> None:
        try:
            ui_logic.save_profile(ui_logic.form_to_profile(self._read_form()), PROFILE)
        except (RuntimeError, OSError) as e:
            self._say(self.params_msg, f"No se guardó: {e}", False)
            return
        self._say(self.params_msg, "Perfil guardado.", True)
        self.create_button.grid_remove()
        self.refresh_status()

    def _create_from_folder(self) -> None:
        folder = filedialog.askdirectory(title="Carpeta con una subcarpeta por CV")
        if not folder:
            return
        try:
            names = setup.list_cv_dirs(Path(folder))
        except RuntimeError as e:
            self._say(self.params_msg, str(e), False)
            return
        if not names:
            self._say(self.params_msg, f"En esa carpeta no hay subcarpetas con {setup.RESUME_NAME}.", False)
            return
        dialog = self._dialog("Elegí hasta 3 CV")
        ctk.CTkLabel(dialog, text="Una variante por CV (hasta 3):").pack(padx=20, pady=(20, 8), anchor="w")
        checks = {}
        message = ctk.CTkLabel(dialog, text="", wraplength=320)

        def toggle() -> None:
            full = sum(var.get() for var in checks.values()) >= profile.MAX_VARIANTS
            for name, box in boxes.items():
                box.configure(state="disabled" if full and not checks[name].get() else "normal")

        boxes = {}
        for name in names:
            checks[name] = ctk.BooleanVar()
            boxes[name] = ctk.CTkCheckBox(dialog, text=name, variable=checks[name], command=toggle)
            boxes[name].pack(padx=28, pady=3, anchor="w")
        message.pack(padx=20, pady=(8, 0))

        def create() -> None:
            try:
                setup.write_profile(Path(folder), [name for name, var in checks.items() if var.get()], PROFILE)
            except (RuntimeError, OSError) as e:
                self._say(message, str(e), False)
                return
            dialog.destroy()
            self.load_form()
            self.refresh_status()
            self._say(self.params_msg, "Perfil creado. Completá las keywords y guardá.", True)

        buttons = ctk.CTkFrame(dialog, fg_color="transparent")
        buttons.pack(pady=(8, 20))
        ctk.CTkButton(buttons, text="Crear", width=120, command=create).pack(side="left", padx=6)
        ctk.CTkButton(buttons, text="Cancelar", width=120, fg_color="gray40", command=dialog.destroy).pack(side="left", padx=6)

    # ── pestaña Correr ───────────────────────────────────────────────────────

    def _build_run(self, tab) -> None:
        actions = ctk.CTkFrame(tab, fg_color="transparent")
        actions.pack(fill="x")
        self.run_button = ctk.CTkButton(actions, text=TITLES["run"], command=lambda: self._launch("run"))
        self.score_button = ctk.CTkButton(actions, text=TITLES["score"], command=lambda: self._launch("score"))
        self.apply_button = ctk.CTkButton(actions, text=TITLES["apply"], command=lambda: self._launch("apply"))
        self.live_button = ctk.CTkButton(actions, text=TITLES["apply_live"], fg_color="#b45309", hover_color="#92400e", command=self._live)
        for button in (self.run_button, self.score_button, self.apply_button, self.live_button):
            button.pack(side="left", padx=4)
        self._build_batch(tab)
        controls = ctk.CTkFrame(tab, fg_color="transparent")
        controls.pack(fill="x", pady=8)
        self.stop_button = ctk.CTkButton(controls, text="Detener", width=130, state="disabled", fg_color="gray40", command=self._stop)
        self.stop_button.pack(side="left", padx=4)
        self.report_button = ctk.CTkButton(controls, text="Abrir informe", width=130, state="disabled", command=self._open_report)
        self.report_button.pack(side="left", padx=4)
        self.result_label = ctk.CTkLabel(controls, text="", anchor="w")
        self.result_label.pack(side="left", padx=12)
        box = ctk.CTkFrame(tab)  # va abajo del todo; se arma antes que el registro para que este ocupe lo que sobra
        box.pack(side="bottom", fill="x", pady=(8, 0))
        box.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(box, text="Corrida programada", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 0))
        self.sched_status = ctk.CTkLabel(box, text="", anchor="w", justify="left", wraplength=860)
        self.sched_status.grid(row=1, column=0, columnspan=4, sticky="ew", padx=10, pady=2)
        self.sched_time = ctk.CTkEntry(box, width=80, placeholder_text="HH:MM")
        self.sched_time.grid(row=2, column=0, padx=(10, 4), pady=4)
        self.sched_set = ctk.CTkButton(box, text="Programar", width=120, command=self._schedule_set)
        self.sched_set.grid(row=2, column=1, padx=4)
        self.sched_off = ctk.CTkButton(box, text="Quitar", width=100, fg_color="gray40", command=self._schedule_off)
        self.sched_off.grid(row=2, column=2, padx=4)
        self.sched_msg = ctk.CTkLabel(box, text="", anchor="w", justify="left", wraplength=540)
        self.sched_msg.grid(row=2, column=3, sticky="w", padx=8)
        ctk.CTkLabel(box, text=SCHEDULE_NOTE, anchor="w", justify="left", wraplength=860, text_color=GREY).grid(
            row=3, column=0, columnspan=4, sticky="ew", padx=10, pady=(2, 8))
        self.log = ctk.CTkTextbox(tab, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)

    def _build_batch(self, tab) -> None:
        """El bloque «Tanda»: CV (variante), búsquedas, minutos y tope, con «Plan de la tanda» (--dry) y «Postular la tanda»."""
        box = ctk.CTkFrame(tab)
        box.pack(fill="x", pady=(8, 0))
        box.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(box, text="Tanda", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, columnspan=6, sticky="w", padx=10, pady=(8, 0))
        ctk.CTkLabel(box, text="CV").grid(row=1, column=0, padx=(10, 4), pady=4, sticky="e")
        self.batch_variant = ctk.CTkOptionMenu(box, values=["(sin perfil)"], width=140)
        self.batch_variant.grid(row=1, column=1, padx=4)
        ctk.CTkLabel(box, text="Búsquedas (separadas por comas)").grid(row=1, column=2, padx=(12, 4), sticky="e")
        self.batch_keywords = ctk.CTkEntry(box)
        self.batch_keywords.grid(row=1, column=3, columnspan=3, padx=(4, 10), sticky="ew")
        ctk.CTkLabel(box, text="Minutos").grid(row=2, column=0, padx=(10, 4), pady=4, sticky="e")
        self.batch_minutes = ctk.CTkEntry(box, width=70)
        self.batch_minutes.grid(row=2, column=1, padx=4, sticky="w")
        ctk.CTkLabel(box, text="Tope (vacío: sin tope)").grid(row=2, column=2, padx=(12, 4), sticky="e")
        self.batch_max = ctk.CTkEntry(box, width=70)
        self.batch_max.grid(row=2, column=3, padx=4, sticky="w")
        self.batch_afin = ctk.CTkCheckBox(box, text="Afín por puntaje")
        self.batch_afin.grid(row=3, column=0, columnspan=2, padx=(10, 4), pady=(0, 6), sticky="w")
        self.batch_company = ctk.CTkCheckBox(box, text="Tope por empresa")
        self.batch_company.grid(row=3, column=2, columnspan=2, padx=(12, 4), pady=(0, 6), sticky="w")
        self.batch_dry_button = ctk.CTkButton(box, text=TITLES["batch_dry"], width=150, command=lambda: self._batch(True))
        self.batch_dry_button.grid(row=2, column=4, padx=4, pady=6)
        self.batch_live_button = ctk.CTkButton(box, text=TITLES["batch"], width=150, fg_color="#b45309", hover_color="#92400e",
                                               command=lambda: self._batch(False))
        self.batch_live_button.grid(row=2, column=5, padx=(4, 10), pady=6)
        self._batch_load()

    def _batch_load(self) -> None:
        """Pone en el bloque la última tanda (o la variante de siempre, 60 minutos y el tope diario) si el campo está vacío."""
        defaults = ui_logic.batch_defaults(PROFILE)
        self.batch_variant.configure(values=defaults["variants"] or ["(sin perfil)"])
        if not self.batch_keywords.get():
            self.batch_variant.set(defaults["variant"] or "(sin perfil)")
            self.batch_keywords.insert(0, defaults["keywords"])
            self.batch_minutes.insert(0, str(defaults["minutes"]))
            self.batch_max.insert(0, "" if defaults["max"] is None else str(defaults["max"]))
            for box, key in ((self.batch_afin, "afin"), (self.batch_company, "per_company")):
                box.select() if defaults[key] else box.deselect()

    def _batch_settings(self) -> dict:
        """Los datos de la tanda, validados; RuntimeError con el motivo si algo falta."""
        keywords = ", ".join(batch.parse_keywords(self.batch_keywords.get()))
        if not keywords:
            raise RuntimeError("Escribí al menos una búsqueda para la tanda.")
        try:
            minutes = int(self.batch_minutes.get())
            cap = int(self.batch_max.get()) if self.batch_max.get().strip() else None  # vacío: sin tope, manda solo el tiempo
        except ValueError:
            raise RuntimeError("Los minutos y el tope de la tanda tienen que ser números enteros (el tope puede quedar vacío).") from None
        if minutes < 1 or (cap is not None and cap < 0):
            raise RuntimeError("Los minutos son 1 o más y el tope 0 o más.")
        variant = self.batch_variant.get()
        if variant == "(sin perfil)":
            raise RuntimeError("Falta el perfil: completalo en Parámetros.")
        return {"variant": variant, "keywords": keywords, "minutes": minutes, "max": cap, "afin": bool(self.batch_afin.get()),
                "per_company": bool(self.batch_company.get())}

    def _batch(self, dry: bool) -> None:
        """«Plan de la tanda» (--dry) lanza la tanda sin enviar; «Postular la tanda» pide confirmación como el envío real."""
        try:
            settings = self._batch_settings()
            blocker = None if dry else ui_logic.live_blocker(PROFILE)
        except (RuntimeError, sqlite3.Error, OSError) as e:
            self._log(f"Error: {e}")
            return
        if blocker:
            self._log(blocker)
            return
        if dry or messagebox.askyesno(
                "compuapply", f"Se van a enviar de verdad {'todas las postulaciones que cumplan las reglas' if settings['max'] is None else 'hasta ' + str(settings['max']) + ' postulaciones'} a Computrabajo con el CV «{settings['variant']}», "
                              f"durante {settings['minutes']} minutos como máximo, con el navegador visible. ¿Seguir?"):
            self._launch("batch_dry" if dry else "batch", settings=settings)

    def _schedule_refresh(self) -> None:
        """Consulta la tarea programada en un hilo (PowerShell tarda un poco) y muestra su estado."""
        self._say(self.sched_status, "Consultando la tarea programada…", None)

        def target() -> None:
            try:
                self.shown.put((self.sched_status, None, schedule.describe(schedule.current())))
            except RuntimeError as e:
                self.shown.put((self.sched_status, False, str(e)))

        threading.Thread(target=target, daemon=True).start()

    def _schedule_set(self) -> None:
        """Programa la corrida diaria, si la clave y el perfil están en ✔ y la hora es válida."""
        try:
            at = schedule.valid_time(self.sched_time.get())
            reason = ui_logic.schedule_blocker(ui_logic.status(ENV, PROFILE))
            if reason:
                raise RuntimeError(reason)
            pythonw = ui_logic.pythonw_exe()
        except RuntimeError as e:
            self._say(self.sched_msg, str(e), False)
            return
        self._in_thread("Programando…", lambda: schedule.set_daily(pythonw, PROJECT, at), label=self.sched_msg,
                        then=self._schedule_refresh)

    def _schedule_off(self) -> None:
        self._in_thread("Quitando…", lambda: schedule.removal_message(schedule.remove()), label=self.sched_msg,
                        then=self._schedule_refresh)

    def _live(self) -> None:
        """Pide confirmación (con cuántas ofertas manda) y lanza `apply --live`: postula de verdad, con el navegador visible."""
        try:
            blocker = ui_logic.live_blocker(PROFILE)
            count = ui_logic.live_count(PROFILE, ui_logic.DB, ui_logic.status(ENV, PROFILE)["model"])
        except (RuntimeError, sqlite3.Error, OSError) as e:
            self._log(f"Error: {e}")
            return
        if blocker:
            self._log(blocker)
            return
        if not count:
            self._log("No hay ofertas para enviar: el plan de postulación está vacío.")
            return
        if messagebox.askyesno("compuapply", f"Se {'va a enviar 1 postulación' if count == 1 else f'van a enviar {count} postulaciones'} "
                                             "de verdad a Computrabajo, con el navegador visible. ¿Seguir?"):
            self._launch("apply_live")

    def _launch(self, action: str, on_done=None, interactive: bool = False, settings: dict | None = None) -> bool:
        """Lanza la CLI como subproceso (una acción a la vez); devuelve si arrancó."""
        if self.proc is not None or self.thread_busy:
            return False
        settings = settings or {}
        if action == "score":
            try:
                settings = profile.load_profile(PROFILE)["run"]
            except RuntimeError as e:
                self._log(f"Error: {e}")
                return False
        if ui_logic.clear_stop():
            self._log("Había un archivo STOP de una corrida anterior: se borró para poder correr.")
        self._log(f"— {TITLES[action]} —")
        output = queue.Queue()
        try:
            self.proc = ui_logic.start_job(ui_logic.build_command(action, settings), output, interactive)
        except OSError as e:
            self._log(f"No se pudo lanzar el programa: {e}")
            return False
        self.job = {"on_done": on_done, "output": output, "lines": [], "eof": False, "idle": 0}
        self.report = None
        self.report_button.configure(state="disabled")
        self.stop_button.configure(text="Detener", command=self._stop)
        self._say(self.result_label, "Corriendo…", None)
        self._apply_states()
        return True

    def _stop(self) -> None:
        """Pide el freno con el archivo STOP; si a los 30 s sigue vivo, el botón pasa a «Forzar cierre»."""
        ui_logic.request_stop()
        self._log("Se pidió frenar (archivo STOP): el programa para en el próximo control.")
        self.stop_button.configure(state="disabled")
        job = self.job
        self.after(FORCE_AFTER_MS, lambda: self._offer_force(job))

    def _offer_force(self, job: dict) -> None:
        if self.job is job and self.proc is not None and self.proc.poll() is None:
            self.stop_button.configure(text="Forzar cierre", command=self._force, state="normal")
            self._log("Sigue en marcha. Si no frena, usá «Forzar cierre».")

    def _force(self) -> None:
        ui_logic.kill_tree(self.proc.pid)
        self._log("Cierre forzado.")
        self.stop_button.configure(state="disabled")

    def _open_report(self) -> None:
        try:
            os.startfile(self.report.resolve())
        except OSError as e:
            self._log(f"No se pudo abrir el informe: {e}")

    def _poll(self) -> None:
        try:
            self._drain()
        finally:
            self.after(POLL_MS, self._poll)

    def _drain(self) -> None:
        while not self.shown.empty():
            label, ok, message = self.shown.get_nowait()
            self._say(label, message, ok)
        while not self.events.empty():
            label, ok, message, then = self.events.get_nowait()
            self.thread_busy = False
            self._say(label, message, ok)
            self.refresh_status()
            if then:
                then()
        if self.job is None:
            return
        while not self.job["output"].empty():
            line = self.job["output"].get_nowait()
            if line is None:
                self.job["eof"] = True
            else:
                self.job["lines"].append(line)
                self._log(line)
        if self.proc.poll() is not None:
            # Normalmente se espera el fin de la salida; si algún hijo la mantiene abierta, a los 1,2 s se da por terminado.
            self.job["idle"] += 1
            if self.job["eof"] or self.job["idle"] >= 6:
                self._finish()

    def _finish(self) -> None:
        code, job = self.proc.poll(), self.job
        self.proc = self.job = None
        self._close_login_dialog()
        self._say(self.result_label, "Terminó bien" if code == 0 else "Frenó: mirá el registro", code == 0)
        self.report = ui_logic.report_path(job["lines"])
        self.report_button.configure(state="normal" if self.report else "disabled")
        self.stop_button.configure(text="Detener", command=self._stop)
        if job["on_done"]:
            job["on_done"](code)
        self.refresh_status()

    # ── tablas ───────────────────────────────────────────────────────────────

    def _style_tables(self) -> None:
        """Colores de las tablas (`ttk.Treeview`) según el modo claro u oscuro de la ventana."""
        dark = ctk.get_appearance_mode() == "Dark"
        background, text, heading, selected = (
            ("#2b2b2b", "#dce4ee", "#3a3a3a", "#1f6aa5") if dark else ("#ffffff", "#1a1a1a", "#e3e3e3", "#3b8ed0"))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=background, fieldbackground=background, foreground=text, rowheight=24,
                        borderwidth=0)
        style.configure("Treeview.Heading", background=heading, foreground=text, relief="flat")
        style.map("Treeview", background=[("selected", selected)], foreground=[("selected", "white")])
        style.map("Treeview.Heading", background=[("active", heading)])

    @staticmethod
    def _table(parent, columns: tuple, height: int):
        """Tabla con barra de desplazamiento. `columns` = ((clave, título, ancho, alineación), ...)."""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        tree = ttk.Treeview(frame, columns=[column[0] for column in columns], show="headings", height=height,
                            selectmode="browse")
        for key, title, width, anchor in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor=anchor, stretch=anchor == "w")
        scrollbar = ctk.CTkScrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return frame, tree

    @staticmethod
    def _set_text(box: ctk.CTkTextbox, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    # ── pestaña Calibrar ─────────────────────────────────────────────────────

    def _build_calibrate(self, tab) -> None:
        self.cal_header = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=860)
        self.cal_header.pack(fill="x", padx=4)
        self.cal_title = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=860,
                                      font=ctk.CTkFont(size=16, weight="bold"))
        self.cal_title.pack(fill="x", padx=4, pady=(10, 0))
        self.cal_meta = ctk.CTkLabel(tab, text="", anchor="w")
        self.cal_meta.pack(fill="x", padx=4)
        self.cal_text = ctk.CTkTextbox(tab, height=100, wrap="word", state="disabled")
        self.cal_text.pack(fill="both", expand=True, padx=4, pady=8)
        answers = ctk.CTkFrame(tab, fg_color="transparent")
        answers.pack(fill="x")
        self.cal_open = ctk.CTkButton(answers, text="Abrir la oferta", width=140, fg_color="gray40", command=self._cal_open_offer)
        self.cal_yes = ctk.CTkButton(answers, text="Sí, me postularía", width=170, command=lambda: self._answer(True))
        self.cal_no = ctk.CTkButton(answers, text="No", width=100, command=lambda: self._answer(False))
        self.cal_skip = ctk.CTkButton(answers, text="Saltar", width=100, fg_color="gray40", command=lambda: self._answer(None))
        for button in (self.cal_open, self.cal_yes, self.cal_no, self.cal_skip):
            button.pack(side="left", padx=4)
        bottom = ctk.CTkFrame(tab, fg_color="transparent")
        bottom.pack(fill="x", pady=(10, 0))
        self.cal_threshold_button = ctk.CTkButton(bottom, text="Calcular umbral", width=150, command=self._cal_threshold)
        self.cal_threshold_button.grid(row=0, column=0, padx=4)
        self.cal_use_button = ctk.CTkButton(bottom, text="Usar el umbral sugerido", width=190, command=self._cal_use_suggested)
        self.cal_use_button.grid(row=0, column=1, padx=4)
        self.cal_use_button.grid_remove()  # solo aparece con un umbral sugerido
        self.cal_msg = ctk.CTkLabel(bottom, text="", anchor="w", justify="left", wraplength=520)
        self.cal_msg.grid(row=0, column=2, padx=8, sticky="w")
        self.cal_table_frame, self.cal_table = self._table(tab, (
            ("threshold", "Umbral", 80, "center"), ("send", "Enviaría", 90, "center"),
            ("precision", "Precisión", 100, "center"), ("recall", "Cobertura", 100, "center")), len(review.THRESHOLDS))

    def _calibrate_load(self) -> None:
        """Carga una tanda de ofertas sin etiquetar (en orden aleatorio) y muestra la primera."""
        try:
            zones = profile.load_zones(PROFILE)  # solo ofrece ofertas de las zonas del perfil
            with _connection() as conn:
                self.cal_items = review.label_candidates(conn, CALIBRATE_LIMIT, zones=zones)
        except (sqlite3.Error, RuntimeError) as e:
            self.cal_items = []
            self._say(self.cal_msg, f"No se pudo leer la base o el perfil: {e}", False)
        self.cal_index = 0
        self._calibrate_show()

    def _calibrate_show(self) -> None:
        counts = {"total": 0, "yes": 0, "no": 0}
        try:
            with _connection() as conn:
                counts = review.label_counts(conn)
        except sqlite3.Error as e:
            self._say(self.cal_msg, f"No se pudo leer la base: {e}", False)
        totals = f"etiquetadas en total: {counts['total']} (sí: {counts['yes']} · no: {counts['no']})"
        offer = self.cal_items[self.cal_index] if self.cal_items else None
        if offer:
            self.cal_header.configure(text=f"Oferta {self.cal_index + 1} de {len(self.cal_items)} · {totals}")
            self.cal_title.configure(text=offer["title"] or "(sin título)")
            self.cal_meta.configure(text=" · ".join(
                part for part in (offer["company"], offer["location"], offer["employment_type"]) if part))
            self._set_text(self.cal_text, offer["excerpt"])
        else:
            self.cal_header.configure(text=totals.capitalize())
            self.cal_title.configure(text=NO_CANDIDATES)
            self.cal_meta.configure(text="")
            self._set_text(self.cal_text, "")
        for button in (self.cal_yes, self.cal_no, self.cal_skip):
            button.configure(state="normal" if offer else "disabled")
        self.cal_open.configure(state="normal" if offer and offer["url"] else "disabled")
        self.cal_threshold_button.configure(state="normal" if counts["total"] >= review.MIN_LABELS else "disabled")

    def _answer(self, label: bool | None) -> None:
        """Guarda la respuesta al instante (`None` = saltar) y pasa a la oferta siguiente; al terminar la tanda carga otra."""
        if not self.cal_items:
            return
        if label is not None:
            try:
                with _connection() as conn:
                    review.save_label(conn, self.cal_items[self.cal_index]["id16"], label)
            except sqlite3.Error as e:
                self._say(self.cal_msg, f"No se guardó la respuesta: {e}", False)
                return
            self.cal_table_frame.pack_forget()  # el cálculo anterior ya no cuenta las etiquetas nuevas
            self.cal_use_button.grid_remove()
            self._say(self.cal_msg, "", True)
        self.cal_index += 1
        if self.cal_index >= len(self.cal_items):
            self._calibrate_load()
        else:
            self._calibrate_show()

    def _on_key(self, event) -> None:
        """Atajos de Calibrar: s = sí, n = no, espacio = saltar. Solo en esa pestaña y fuera de los campos de texto."""
        key = event.keysym.lower()
        if key not in ("s", "n", "space") or event.state & 0x20004 or self.tabs.get() != "Calibrar":
            return  # 0x4 = Ctrl, 0x20000 = Alt
        try:
            focus = self.focus_get()
        except (KeyError, tkinter.TclError):
            return
        if isinstance(focus, (tkinter.Entry, tkinter.Text)) and str(focus.cget("state")) == "normal":
            return
        self._answer({"s": True, "n": False, "space": None}[key])

    def _cal_open_offer(self) -> None:
        webbrowser.open(self.cal_items[self.cal_index]["url"])

    def _cal_threshold(self) -> None:
        model = ui_logic.status(ENV, PROFILE)["model"]
        try:
            with _connection() as conn:
                table, suggested = review.threshold_table(review.db_labels(conn, model, profile.load_zones(PROFILE)))
                unscored = review.unscored_note(conn, model)
        except (sqlite3.Error, RuntimeError) as e:
            self._say(self.cal_msg, f"No se pudo leer la base: {e}", False)
            return
        self.cal_table.delete(*self.cal_table.get_children())
        for row in table:
            self.cal_table.insert("", "end", iid=str(row["threshold"]), values=(
                row["threshold"], row["send"], _percent(row["precision"]), _percent(row["recall"])))
        self.cal_table_frame.pack(fill="x", pady=(8, 0))
        self.cal_suggested, self.cal_model = suggested, model
        extra = "\n" + unscored if unscored else ""
        if suggested is None:
            self._say(self.cal_msg, f"Ningún umbral llega a {review.MIN_PRECISION}% de precisión{extra}", None)
            self.cal_use_button.grid_remove()
            return
        best = next(row for row in table if row["threshold"] == suggested)
        self.cal_table.selection_set(str(suggested))
        self._say(self.cal_msg, f"Umbral sugerido: {suggested} (precisión {_percent(best['precision'])}, "
                                f"cobertura {_percent(best['recall'])}){extra}", None)
        self.cal_use_button.grid()

    def _cal_use_suggested(self) -> None:
        try:
            ui_logic.set_threshold(PROFILE, self.cal_suggested, self.cal_model)
        except (RuntimeError, OSError) as e:
            self._say(self.cal_msg, f"No se guardó: {e}", False)
            return
        self._say(self.cal_msg, f"Umbral guardado en el perfil: {self.cal_suggested}", True)
        self.fields["threshold"].delete(0, "end")  # el formulario de Parámetros muestra el valor nuevo
        self.fields["threshold"].insert(0, str(self.cal_suggested))
        self.cal_use_button.grid_remove()
        self.refresh_status()

    # ── pestaña Resultados ───────────────────────────────────────────────────

    def _build_results(self, tab) -> None:
        self.res_notice = ctk.CTkFrame(tab, fg_color="transparent")
        ctk.CTkLabel(self.res_notice, text=BIAS_NOTICE, wraplength=720, justify="center").pack(pady=(60, 14))
        ctk.CTkButton(self.res_notice, text="Mostrar igual", width=160, command=self._results_force).pack()
        self.res_body = ctk.CTkFrame(tab, fg_color="transparent")
        bar = ctk.CTkFrame(self.res_body, fg_color="transparent")
        bar.pack(fill="x")
        self.only_passing = ctk.CTkCheckBox(bar, text="Solo las que pasan el umbral", command=self._results_refresh)
        self.only_passing.pack(side="left", padx=4)
        self.res_count = ctk.CTkLabel(bar, text="", anchor="w", text_color=GREY)
        self.res_count.pack(side="left", padx=12)
        frame, self.res_tree = self._table(self.res_body, (
            ("score", "Puntaje", 70, "center"), ("title", "Título", 300, "w"), ("company", "Empresa", 160, "w"),
            ("variant", "Variante", 90, "w"), ("status", "Estado", 90, "w"), ("date", "Fecha", 120, "center")), 9)
        frame.pack(fill="both", expand=True, pady=6)
        self.res_tree.bind("<<TreeviewSelect>>", self._results_select)
        self.res_tree.bind("<Double-1>", self._results_open)
        self.res_detail = ctk.CTkTextbox(self.res_body, height=70, wrap="word", state="disabled")
        self.res_detail.pack(fill="x")
        cv_bar = ctk.CTkFrame(self.res_body, fg_color="transparent")
        cv_bar.pack(fill="x", pady=(6, 0))
        self.cv_make_button = ctk.CTkButton(cv_bar, text="Armar CV", width=130, command=self._cv_make)
        self.cv_make_button.pack(side="left", padx=(0, 6))
        self.cv_open_button = ctk.CTkButton(cv_bar, text="Abrir CV", width=130, fg_color="gray40", command=self._cv_open)
        self.cv_open_button.pack(side="left", padx=(0, 6))
        self.cv_review_button = ctk.CTkButton(cv_bar, text="Abrir revisión", width=130, fg_color="gray40", command=self._cv_review)
        self.cv_review_button.pack(side="left")
        ctk.CTkLabel(cv_bar, text="Adapta tu CV a la oferta elegida sin agregar hechos nuevos; no se sube nada: lo revisás vos.",
                     text_color=GREY).pack(side="left", padx=10)
        self._cv_buttons()
        ctk.CTkLabel(self.res_body, text="Informes", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(8, 0))
        reports = ctk.CTkFrame(self.res_body, fg_color="transparent")
        reports.pack(fill="x")
        frame, self.rep_tree = self._table(reports, (("name", "Informe", 360, "w"),), 3)
        frame.pack(side="left", fill="x", expand=True)
        side = ctk.CTkFrame(reports, fg_color="transparent")
        side.pack(side="left", padx=8)
        ctk.CTkButton(side, text="Abrir", width=130, command=self._report_open).pack(pady=2)
        ctk.CTkButton(side, text="Abrir carpeta", width=130, fg_color="gray40", command=self._report_folder).pack(pady=2)
        self.res_spend = ctk.CTkLabel(self.res_body, text="", anchor="w")
        self.res_spend.pack(fill="x", pady=(8, 0))

    def _results_load(self) -> None:
        """Con menos de 20 etiquetas primero va el aviso; si no, la tabla."""
        try:
            with _connection() as conn:
                labeled = review.label_counts(conn)["total"]
        except sqlite3.Error as e:
            labeled = review.MIN_LABELS
            self._say(self.res_count, f"No se pudo leer la base o el perfil: {e}", False)
        if labeled < review.MIN_LABELS and not self.results_forced:
            self.res_body.pack_forget()
            self.res_notice.pack(fill="both", expand=True)
        else:
            self.res_notice.pack_forget()
            self.res_body.pack(fill="both", expand=True)
            self._results_refresh()

    def _results_force(self) -> None:
        self.results_forced = True
        self._results_load()

    def _results_refresh(self) -> None:
        threshold, problem = None, ""
        if self.only_passing.get():
            try:
                threshold = profile.load_profile(PROFILE)["apply"]["threshold"]
            except RuntimeError as e:
                self.only_passing.deselect()
                problem = f"No se pudo leer el umbral: {e}"
        try:
            zones = profile.load_zones(PROFILE)
            with _connection() as conn:
                rows, outside = ui_logic.results_split(conn, ui_logic.status(ENV, PROFILE)["model"], threshold, zones)
                money = ui_logic.spend(conn, datetime.now(timezone.utc).date())
        except (sqlite3.Error, RuntimeError) as e:
            self._say(self.res_count, f"No se pudo leer la base o el perfil: {e}", False)
            return
        self.result_rows = {row["id16"]: row for row in rows}
        self.res_tree.delete(*self.res_tree.get_children())
        for row in rows:
            self.res_tree.insert("", "end", iid=row["id16"], values=(
                row["score"], row["title"], row["company"], row["variant"], row["status"], _local(row["scored_at"])))
        self._set_text(self.res_detail, "")
        count = f"{len(rows)} oferta{'' if len(rows) == 1 else 's'}" + (f" con {threshold} puntos o más" if threshold is not None else "")
        count += f" · {outside} fuera de zona (no se muestran)" if outside else ""
        self._say(self.res_count, problem or count, False if problem else None)
        self.rep_paths = {path.name: path for path in ui_logic.reports()}
        self.rep_tree.delete(*self.rep_tree.get_children())
        for name in self.rep_paths:
            self.rep_tree.insert("", "end", iid=name, values=(name,))
        self.res_spend.configure(text=f"Gasto en OpenAI: este mes {score.money(money['month'])} · total {score.money(money['total'])}")
        self._cv_buttons()

    def _results_select(self, _event=None) -> None:
        selected = self.res_tree.selection()
        if selected:
            row = self.result_rows[selected[0]]
            self._set_text(self.res_detail, "Razones: " + (" | ".join(row["reasons"]) or "—") + "\n"
                           "Faltantes: " + (" | ".join(row["missing"]) or "—")
                           + ("\nRequisitos que el CV no muestra: " + " | ".join(row["unmet_required"]) if row["unmet_required"] else ""))
        self._cv_buttons()

    def _cv_info(self, id16: str) -> dict | None:
        """El CV guardado de la oferta, o None (también si todavía no hay base)."""
        if not ui_logic.DB.is_file():
            return None
        try:
            with _connection() as conn:
                return cv.existing(conn, id16)
        except sqlite3.Error:
            return None

    def _cv_buttons(self) -> None:
        """«Armar CV» pide una fila elegida, clave y perfil en ✔ y nada en curso; «Abrir CV», que la oferta ya tenga uno."""
        selected = self.res_tree.selection()
        busy = self.proc is not None or self.thread_busy
        self.cv_make_button.configure(state="normal" if selected and not busy and self.key_ok and self.profile_ok else "disabled")
        info = self._cv_info(selected[0]) if selected else None
        self.cv_open_button.configure(state="normal" if info else "disabled")
        self.cv_review_button.configure(state="normal" if info and cv.revision_path(info["html"]).is_file() else "disabled")

    def _cv_make(self) -> None:
        """Arma el CV de la fila elegida en un hilo (llama a OpenAI, por centavos); los errores van al mensaje de Resultados."""
        selected = self.res_tree.selection()
        if selected:
            self._in_thread("Armando el CV con OpenAI…", lambda: ui_logic.cv_line(
                ui_logic.run_cv(ENV, PROFILE, ui_logic.DB, selected[0], openai.OpenAI)), label=self.res_count, then=self._cv_buttons)

    def _cv_review(self) -> None:
        selected = self.res_tree.selection()
        info = self._cv_info(selected[0]) if selected else None
        if info:
            self._startfile(cv.revision_path(info["html"]))

    def _cv_open(self) -> None:
        selected = self.res_tree.selection()
        info = self._cv_info(selected[0]) if selected else None
        if info:
            pdf = Path(info["pdf"])
            self._startfile(pdf if pdf.is_file() else Path(info["html"]))

    def _results_open(self, event) -> None:
        iid = self.res_tree.identify_row(event.y)
        if iid and self.result_rows[iid]["url"]:
            webbrowser.open(self.result_rows[iid]["url"])

    def _report_open(self) -> None:
        selected = self.rep_tree.selection() or self.rep_tree.get_children()[:1]
        if selected:
            self._startfile(self.rep_paths[selected[0]])

    def _report_folder(self) -> None:
        self._startfile(ui_logic.RUNS)

    def _startfile(self, path: Path) -> None:
        try:
            os.startfile(path.resolve())
        except OSError as e:
            self._say(self.res_count, f"No se pudo abrir: {e}", False)

    # ── pestaña Banco ────────────────────────────────────────────────────────

    def _build_bank(self, tab) -> None:
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x")
        ctk.CTkLabel(bar, text="Variante").pack(side="left", padx=(4, 6))
        self.bank_variant = ctk.CTkOptionMenu(bar, values=[""], width=160, command=lambda _value: self._bank_refresh())
        self.bank_variant.pack(side="left")
        self.bank_propose_button = ctk.CTkButton(bar, text="Proponer", width=120, command=self._bank_propose)
        self.bank_propose_button.pack(side="left", padx=10)
        self.bank_count = ctk.CTkLabel(bar, text="", anchor="w", text_color=GREY)
        self.bank_count.pack(side="left")
        self.bank_msg = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=860)
        self.bank_msg.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(tab, anchor="w", justify="left", wraplength=860, text_color=GREY,
                     text="El programa propone lo que se sigue de lo que hiciste (por ejemplo, usar React implica JavaScript). "
                          "Aprobar una propuesta es declarar que es cierta: solo lo aprobado entra al banco y lo usa el CV a medida.").pack(
            fill="x", pady=(2, 6))
        ctk.CTkLabel(tab, text="Propuestas", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x")
        frame, self.bank_tree = self._table(tab, (("text", "Hecho", 300, "w"), ("reason", "Por qué", 240, "w"),
                                                  ("source", "Línea de tu CV de donde sale", 300, "w")), 6)
        frame.pack(fill="both", expand=True, pady=4)
        self.bank_tree.bind("<<TreeviewSelect>>", lambda _event: self._bank_buttons())
        buttons = ctk.CTkFrame(tab, fg_color="transparent")
        buttons.pack(fill="x")
        self.bank_approve_button = ctk.CTkButton(buttons, text="Aprobar", width=110, command=self._bank_approve)
        self.bank_approve_button.pack(side="left", padx=(0, 6))
        self.bank_reject_button = ctk.CTkButton(buttons, text="Rechazar", width=110, fg_color="gray40", command=self._bank_reject)
        self.bank_reject_button.pack(side="left", padx=(0, 6))
        self.bank_all_button = ctk.CTkButton(buttons, text="Aprobar todas", width=130, command=self._bank_approve_all)
        self.bank_all_button.pack(side="left")
        ctk.CTkLabel(tab, text="Banco aprobado", font=ctk.CTkFont(weight="bold"), anchor="w").pack(fill="x", pady=(10, 0))
        frame, self.bank_approved = self._table(tab, (("text", "Hecho", 840, "w"),), 5)
        frame.pack(fill="both", expand=True, pady=4)
        self.bank_approved.bind("<<TreeviewSelect>>", lambda _event: self._bank_buttons())
        add = ctk.CTkFrame(tab, fg_color="transparent")
        add.pack(fill="x")
        self.bank_remove_button = ctk.CTkButton(add, text="Quitar", width=110, fg_color="gray40", command=self._bank_remove)
        self.bank_remove_button.pack(side="left", padx=(0, 10))
        self.bank_entry = ctk.CTkEntry(add, placeholder_text="Un hecho cierto escrito por vos (primera persona)")
        self.bank_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.bank_add_button = ctk.CTkButton(add, text="Agregar", width=110, command=self._bank_add)
        self.bank_add_button.pack(side="left")
        self._bank_buttons()

    def _bank_buttons(self) -> None:
        """«Proponer» pide clave y perfil en ✔, una variante y nada en curso; el resto, algo elegido (o propuestas, para «Aprobar todas»)."""
        busy = self.proc is not None or self.thread_busy
        ready = not busy and bool(self.bank_variant.get())
        self.bank_propose_button.configure(state="normal" if ready and self.key_ok and self.profile_ok else "disabled")
        chosen = bool(self.bank_tree.selection())
        self.bank_approve_button.configure(state="normal" if ready and chosen else "disabled")
        self.bank_reject_button.configure(state="normal" if ready and chosen else "disabled")
        self.bank_all_button.configure(state="normal" if ready and self.bank_proposals else "disabled")
        self.bank_remove_button.configure(state="normal" if ready and self.bank_approved.selection() else "disabled")
        self.bank_add_button.configure(state="normal" if ready else "disabled")

    def _bank_load(self) -> None:
        """Al entrar a la pestaña: las variantes del perfil en el menú y la vista de la elegida."""
        try:
            ids = [v["id"] for v in ui_logic.profile_to_form(PROFILE)["variants"] if v["id"]]
        except RuntimeError as e:
            ids = []
            self._say(self.bank_msg, str(e), False)
        self.bank_variant.configure(values=ids or [""])
        if self.bank_variant.get() not in ids:
            self.bank_variant.set(ids[0] if ids else "")
        self._bank_refresh()

    def _bank_refresh(self) -> None:
        variant = self.bank_variant.get()
        self.bank_tree.delete(*self.bank_tree.get_children())
        self.bank_approved.delete(*self.bank_approved.get_children())
        self.bank_proposals, self.bank_items = [], []
        if not variant:
            self._say(self.bank_count, "Todavía no hay variantes en el perfil.", None)
            self._bank_buttons()
            return
        try:
            view = ui_logic.bank_view(variant, PROFILE)
        except RuntimeError as e:
            self._say(self.bank_msg, str(e), False)
            self._say(self.bank_count, "", None)
            self._bank_buttons()
            return
        self.bank_proposals, self.bank_items = view["propuestas"], view["banco"]
        for n, proposal in enumerate(self.bank_proposals):
            self.bank_tree.insert("", "end", iid=str(n), values=(proposal["text"], proposal["reason"], " | ".join(proposal["source_lines"])))
        for n, text in enumerate(self.bank_items):
            self.bank_approved.insert("", "end", iid=str(n), values=(text,))
        self._say(self.bank_count, f"{len(self.bank_proposals)} por aprobar · {len(self.bank_items)} aprobadas · "
                                   f"{view['rechazadas']} rechazadas", None)
        self._bank_buttons()

    def _bank_propose(self) -> None:
        """Pide las propuestas en un hilo (llama a OpenAI, por centavos); lo pendiente se suma, no se pisa."""
        variant = self.bank_variant.get()
        if variant:
            self._in_thread("Pidiendo propuestas a OpenAI…", lambda: ui_logic.propose_line(
                ui_logic.run_propose(ENV, PROFILE, ui_logic.DB, variant, openai.OpenAI)), label=self.bank_msg, then=self._bank_refresh)

    def _bank_do(self, work) -> None:
        """Una operación rápida sobre los archivos del banco: su mensaje o su error van a Banco y la vista se actualiza."""
        try:
            self._say(self.bank_msg, work(), True)
        except RuntimeError as e:
            self._say(self.bank_msg, str(e), False)
        self._bank_refresh()

    def _bank_selected(self) -> list[str]:
        return [self.bank_proposals[int(iid)]["text"] for iid in self.bank_tree.selection()]

    def _bank_approve(self) -> None:
        texts, variant = self._bank_selected(), self.bank_variant.get()
        self._bank_do(lambda: f"Aprobadas: {ui_logic.approve(variant, texts, PROFILE)}. Pasaron al banco.")

    def _bank_approve_all(self) -> None:
        texts, variant = [p["text"] for p in self.bank_proposals], self.bank_variant.get()
        self._bank_do(lambda: f"Aprobadas: {ui_logic.approve(variant, texts, PROFILE)}. Pasaron al banco.")

    def _bank_reject(self) -> None:
        texts, variant = self._bank_selected(), self.bank_variant.get()
        self._bank_do(lambda: f"Rechazadas: {ui_logic.reject(variant, texts, PROFILE)}. No se vuelven a proponer.")

    def _bank_add(self) -> None:
        text, variant = self.bank_entry.get(), self.bank_variant.get()

        def work() -> str:
            ui_logic.add_truth(variant, text, PROFILE)
            self.bank_entry.delete(0, "end")
            return "Agregado al banco."

        self._bank_do(work)

    def _bank_remove(self) -> None:
        variant = self.bank_variant.get()
        texts = [self.bank_items[int(iid)] for iid in self.bank_approved.selection()]

        def work() -> str:
            for text in texts:
                ui_logic.remove_truth(variant, text, PROFILE)
            return f"Quitadas: {len(texts)}."

        self._bank_do(work)

    # ── pestaña Preguntas ────────────────────────────────────────────────────

    def _build_questions(self, tab) -> None:
        ctk.CTkLabel(tab, anchor="w", justify="left", wraplength=860, text_color=GREY,
                     text="Preguntas de selección que el programa no pudo contestar con tu CV ni con tus respuestas. Escribí la "
                          "respuesta: queda guardada y la oferta vuelve a la cola para la próxima postulación.").pack(fill="x")
        frame, self.question_tree = self._table(tab, (("question", "Pregunta", 520, "w"), ("options", "Opciones", 240, "w"),
                                                      ("offers", "Ofertas", 70, "center")), 10)
        frame.pack(fill="both", expand=True, pady=6)
        self.question_tree.bind("<<TreeviewSelect>>", lambda _event: self._question_buttons())
        self.question_detail = ctk.CTkTextbox(tab, height=70, wrap="word", state="disabled")
        self.question_detail.pack(fill="x")
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        # desplegable editable: en una cerrada se elige una de sus opciones (se guarda su texto exacto); también acepta texto libre
        self.question_entry = ctk.CTkComboBox(row, values=[""], state="normal")
        self.question_entry.set("")
        self.question_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.question_button = ctk.CTkButton(row, text="Guardar respuesta", width=150, command=self._question_save)
        self.question_button.pack(side="left")
        self.question_msg = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=860)
        self.question_msg.pack(fill="x", pady=(6, 0))
        self._question_buttons()

    def _question_buttons(self) -> None:
        selected = self.question_tree.selection()
        self.question_button.configure(state="normal" if selected else "disabled")
        if selected:
            row = self.question_rows[selected[0]]
            self._set_text(self.question_detail, row["question"] + ("\n\nOpciones: " + " | ".join(row["options"]) if row["options"] else ""))
            self.question_entry.configure(values=list(row["options"]) or [""])
            self.question_entry.set("")
        else:
            self.question_entry.configure(values=[""])

    def _questions_load(self) -> None:
        rows = []
        if ui_logic.DB.is_file():
            try:
                with _connection() as conn:
                    rows = ui_logic.pending_questions(conn)
            except sqlite3.Error as e:
                self._say(self.question_msg, f"No se pudo leer la base: {e}", False)
        self.question_rows = {row["key"]: row for row in rows}
        self.question_tree.delete(*self.question_tree.get_children())
        for row in rows:
            self.question_tree.insert("", "end", iid=row["key"], values=(row["question"], " | ".join(row["options"]), row["offers"]))
        self._set_text(self.question_detail, "")
        if not rows:
            self._say(self.question_msg, "No hay preguntas sin respuesta.", None)
        self._question_buttons()

    def _question_save(self) -> None:
        selected = self.question_tree.selection()
        if not selected:
            return
        try:
            back = ui_logic.answer_question(PROFILE, ui_logic.DB, selected[0], self.question_entry.get())
        except (RuntimeError, sqlite3.Error, OSError) as e:
            self._say(self.question_msg, f"No se guardó: {e}", False)
            return
        self.question_entry.set("")
        self._questions_load()
        self._say(self.question_msg, f"Respuesta guardada. {'Volvió 1 oferta' if back == 1 else f'Volvieron {back} ofertas'} a la cola.", True)

    # ── pestaña Pendientes ───────────────────────────────────────────────────

    def _build_pending(self, tab) -> None:
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x")
        ctk.CTkLabel(bar, text="Variante").pack(side="left", padx=(4, 6))
        self.pending_variant = ctk.CTkOptionMenu(bar, values=[""], width=160)
        self.pending_variant.pack(side="left")
        ctk.CTkLabel(tab, anchor="w", justify="left", wraplength=860, text_color=GREY,
                     text="Preguntas sin repetir que solo vos sabés. Tu respuesta pasa al banco de verdades como un hecho y las ofertas vuelven a la cola; "
                          "«No la contesto» hace que no se pregunte de nuevo. CUIL, DNI y teléfono no se guardan: los contestás vos en la oferta.").pack(
            fill="x", pady=(4, 0))
        frame, self.pending_tree = self._table(tab, (("question", "Pregunta", 430, "w"), ("offers", "Ofertas", 70, "center"),
                                                     ("reason", "Motivo", 340, "w")), 10)
        frame.pack(fill="both", expand=True, pady=6)
        self.pending_tree.bind("<<TreeviewSelect>>", lambda _event: self._pending_buttons())
        row = ctk.CTkFrame(tab, fg_color="transparent")
        row.pack(fill="x", pady=(6, 0))
        self.pending_entry = ctk.CTkEntry(row)
        self.pending_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.pending_button = ctk.CTkButton(row, text="Guardar como hecho", width=160, command=self._pending_save)
        self.pending_button.pack(side="left", padx=(0, 6))
        self.pending_skip_button = ctk.CTkButton(row, text="No la contesto", width=130, fg_color="gray40", command=self._pending_skip)
        self.pending_skip_button.pack(side="left")
        self.pending_msg = ctk.CTkLabel(tab, text="", anchor="w", justify="left", wraplength=860)
        self.pending_msg.pack(fill="x", pady=(6, 0))
        self._pending_buttons()

    def _pending_buttons(self) -> None:
        selected = self.pending_tree.selection()
        row = self.pending_rows.get(selected[0]) if selected else None
        self.pending_button.configure(state="normal" if row and not row["sensitive"] else "disabled")
        self.pending_skip_button.configure(state="normal" if row else "disabled")

    def _pending_load(self) -> None:
        rows = []
        if ui_logic.DB.is_file():
            try:
                with _connection() as conn:
                    rows = pending.groups(conn)
            except sqlite3.Error as e:
                self._say(self.pending_msg, f"No se pudo leer la base: {e}", False)
        try:
            ids = [v["id"] for v in profile.load_profile(PROFILE)["variants"]]
        except (RuntimeError, OSError):
            ids = []
        self.pending_variant.configure(values=ids or [""])
        if self.pending_variant.get() not in ids:
            self.pending_variant.set(ids[0] if ids else "")
        self.pending_rows = {row["key"]: row for row in rows}
        self.pending_tree.delete(*self.pending_tree.get_children())
        for row in rows:
            self.pending_tree.insert("", "end", iid=row["key"], values=(row["question"], row["offers"], row["reason"]))
        if not rows:
            self._say(self.pending_msg, "No hay preguntas pendientes.", None)
        self._pending_buttons()

    def _pending_selected(self):
        selected = self.pending_tree.selection()
        return self.pending_rows.get(selected[0]) if selected else None

    def _pending_save(self) -> None:
        row, text, variant = self._pending_selected(), self.pending_entry.get().strip(), self.pending_variant.get()
        if row is None or not text:
            return
        try:
            with _connection() as conn:
                back = pending.answer(conn, row, text, variant, PROFILE)
        except (RuntimeError, sqlite3.Error, OSError) as e:
            self._say(self.pending_msg, f"No se guardó: {e}", False)
            return
        self.pending_entry.delete(0, "end")
        self._pending_load()
        self._say(self.pending_msg, f"Agregado al banco. {'Volvió 1 oferta' if back == 1 else f'Volvieron {back} ofertas'} a la cola.", True)

    def _pending_skip(self) -> None:
        row = self._pending_selected()
        if row is None:
            return
        try:
            with _connection() as conn:
                pending.skip(conn, row)
        except (sqlite3.Error, OSError) as e:
            self._say(self.pending_msg, f"No se guardó: {e}", False)
            return
        self._pending_load()
        self._say(self.pending_msg, "Marcada como «no la contesto».", True)

    def _on_close(self) -> None:
        if self.proc is not None:
            if not messagebox.askyesno("compuapply", "Hay una acción en curso. ¿Detenerla y cerrar?"):
                return
            ui_logic.kill_tree(self.proc.pid)
        self.destroy()


def main(argv: list[str] | None = None) -> int:
    """Abre la interfaz. Con `--smoke` arma la ventana, la actualiza, la cierra y sale con 0 (no corre ninguna acción)."""
    parser = argparse.ArgumentParser(prog="python -m compuapply.gui")
    parser.add_argument("--smoke", action="store_true", help="arma la ventana y la cierra, para comprobar que abre")
    args = parser.parse_args(argv)
    ctk.set_appearance_mode("system")
    app = App()
    if args.smoke:
        app.update()
        app.destroy()
        return 0
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
