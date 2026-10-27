"""Agente de AudioTranscript: corre en segundo plano y con la tecla Ins graba y transcribe sin abrir la ventana.

Ins → menú: "Reunión en vivo" (PC + micrófono) o "Video" (solo PC). Ins otra vez elige la opción marcada.
Mientras graba solo se ve un puntito gris en la esquina; al pasar el mouse muestra tiempo y botones.
Ins otra vez o "Detener" → transcribe, copia el texto y lo guarda en el historial.
Nada de esto aparece al compartir pantalla.
"""
import ctypes
import ctypes.wintypes
import datetime
import json
import os
import queue
import sys
import threading
import time
import traceback

CARPETA_APP = os.path.dirname(os.path.abspath(__file__))
if sys.stdout is None or sys.stderr is None:
    registro = open(os.path.join(CARPETA_APP, "agente.log"), "w", encoding="utf-8")
    sys.stdout = sys.stdout or registro
    sys.stderr = sys.stderr or registro
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# Una sola copia del agente a la vez (la ventana también lo lanza si no está corriendo)
_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AudioTranscriptAgente")
if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    sys.exit(0)

import tkinter as tk

import nucleo

ATAJO = os.environ.get("AUDIOTRANSCRIPT_ATAJO", "insert")  # Ins, arriba a la derecha junto a F12
PREFERENCIAS = os.path.join(CARPETA_APP, "preferencias.json")
LIBERAR_MODELO_TRAS = 10 * 60  # segundos sin usar antes de soltar la memoria de la GPU
MODOS = {
    "reunion": ("👥  1 · Reunión en vivo", "audio del PC + tu micrófono", {"pc": True, "mic": True}),
    "video": ("🎬  2 · Video", "solo audio del PC, sin micrófono", {"pc": True, "mic": False}),
}

TRANSPARENTE = "#010203"  # color que Windows vuelve invisible: deja el puntito redondo
FONDO, BORDE, TEXTO, APAGADO = "#ffffff", "#c7c7cc", "#1f1f1f", "#8e8e93"
AZUL, AZUL_SUAVE = "#0f6cbd", "#e5f0fb"
PUNTO = {"grabando": "#9aa0a6", "transcribiendo": "#7aa7d9", "listo": "#5fb37a", "error": "#e0a03a"}
FUENTE = ("Segoe UI", 10)


def leer_preferencias():
    try:
        with open(PREFERENCIAS, encoding="utf-8") as f:
            modo = json.load(f).get("modo")
            return {"modo": modo if modo in MODOS else "video"}
    except (OSError, ValueError):
        return {"modo": "video"}


def area_de_trabajo():
    # Pantalla sin la barra de tareas, para pegar el puntito a la esquina de abajo a la derecha
    r = ctypes.wintypes.RECT()
    ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.right, r.bottom


class Flotante:
    """Ventanita de la esquina: menú de opciones, puntito discreto o panel con botones."""

    def __init__(self, raiz, agente):
        self.agente = agente
        self.v = tk.Toplevel(raiz)
        self.v.withdraw()
        self.v.overrideredirect(True)
        self.v.attributes("-topmost", True)
        self.v.attributes("-transparentcolor", TRANSPARENTE)
        self.v.configure(bg=TRANSPARENTE)
        self.preparada = False
        self.contraer_id = None

        # Puntito
        self.punto = tk.Canvas(self.v, width=22, height=22, bg=TRANSPARENTE, highlightthickness=0, cursor="hand2")
        self.ovalo = self.punto.create_oval(5, 5, 17, 17, fill=PUNTO["grabando"], outline="")

        # Panel (al pasar el mouse) y menú comparten el mismo marco con borde
        boton = dict(relief="flat", bd=0, font=FUENTE, cursor="hand2", padx=10, pady=4)
        self.panel = tk.Frame(self.v, bg=BORDE)
        cuerpo = tk.Frame(self.panel, bg=FONDO, padx=8, pady=6)
        cuerpo.pack(padx=1, pady=1)
        self.reloj = tk.Label(cuerpo, text="0:00:00", fg=APAGADO, bg=FONDO, font=FUENTE, width=7)
        self.btn_pc = tk.Button(cuerpo, command=lambda: agente.cambiar("pc"), **boton)
        self.btn_mic = tk.Button(cuerpo, command=lambda: agente.cambiar("mic"), **boton)
        self.btn_detener = tk.Button(cuerpo, text="■  Detener", command=agente.detener, bg=AZUL, fg="white",
                                     activebackground="#115ea3", activeforeground="white", **boton)
        self.aviso = tk.Label(cuerpo, fg=TEXTO, bg=FONDO, font=FUENTE, justify="left")
        self.btn_cerrar = tk.Button(cuerpo, text="✕", command=agente.cancelar, bg=FONDO, fg=APAGADO,
                                    activebackground="#f0f0f0", **{**boton, "padx": 6})
        self.controles = [self.reloj, self.btn_pc, self.btn_mic, self.btn_detener]
        for i, w in enumerate(self.controles):
            w.grid(row=0, column=i, padx=3)
        self.aviso.grid(row=0, column=0, columnspan=4, padx=4, sticky="w")
        self.btn_cerrar.grid(row=0, column=4, padx=(6, 0))

        self.menu = tk.Frame(self.v, bg=BORDE)
        cuerpo_menu = tk.Frame(self.menu, bg=FONDO, padx=10, pady=8)
        cuerpo_menu.pack(padx=1, pady=1)
        tk.Label(cuerpo_menu, text="¿Qué vas a grabar?", fg=TEXTO, bg=FONDO, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.opciones = {}
        for clave, (titulo, detalle, _) in MODOS.items():
            b = tk.Button(cuerpo_menu, text=f"{titulo}\n{detalle}", justify="left", anchor="w", width=30,
                          command=lambda c=clave: agente.elegir(c), **{**boton, "pady": 6})
            b.pack(fill="x", pady=(6, 0))
            self.opciones[clave] = b
        pie = tk.Frame(cuerpo_menu, bg=FONDO)
        pie.pack(fill="x", pady=(6, 0))
        tk.Label(pie, text="Ins = opción marcada", fg=APAGADO, bg=FONDO, font=("Segoe UI", 9)).pack(side="left")
        tk.Button(pie, text="Cancelar", command=agente.cancelar, bg=FONDO, fg=APAGADO, **{**boton, "padx": 6}).pack(side="right")

        self.v.bind("<Enter>", lambda e: self.expandir())
        self.v.bind("<Leave>", lambda e: self.programar_contraer())

    def _preparar(self):
        # Invisible al compartir pantalla o grabarla, y sin robarle el foco a la app en la que estás
        if self.preparada:
            return
        self.v.update_idletasks()
        self.hwnd = ctypes.windll.user32.GetParent(self.v.winfo_id())
        if not os.environ.get("AUDIOTRANSCRIPT_VISIBLE_EN_CAPTURAS"):  # solo para revisar el diseño en pruebas
            ctypes.windll.user32.SetWindowDisplayAffinity(self.hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
        estilo = ctypes.windll.user32.GetWindowLongW(self.hwnd, -20)  # GWL_EXSTYLE
        ctypes.windll.user32.SetWindowLongW(self.hwnd, -20, estilo | 0x08000000 | 0x80)  # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        self.preparada = True

    def _mostrar(self, contenido):
        for w in (self.punto, self.panel, self.menu):
            w.pack_forget()
        contenido.pack()
        self.v.attributes("-alpha", 0.75 if contenido is self.punto else 0.97)
        self.v.update_idletasks()
        derecha, abajo = area_de_trabajo()
        self.v.geometry(f"+{derecha - self.v.winfo_reqwidth() - 10}+{abajo - self.v.winfo_reqheight() - 10}")
        self._preparar()
        if not self.v.winfo_viewable():
            # Tk activa la ventana al mostrarla: se le devuelve el foco a la app en la que estabas escribiendo
            previa = ctypes.windll.user32.GetForegroundWindow()
            self.v.deiconify()
            self.v.update()
            if previa and previa != self.hwnd:
                ctypes.windll.user32.SetForegroundWindow(previa)

    def ocultar(self):
        self.v.withdraw()

    def mostrar_menu(self, marcado):
        for clave, b in self.opciones.items():
            b.configure(bg=AZUL_SUAVE if clave == marcado else "#f5f5f5", fg=AZUL if clave == marcado else TEXTO,
                        activebackground=AZUL_SUAVE)
        self._mostrar(self.menu)

    def mostrar_punto(self, estado):
        self.punto.itemconfigure(self.ovalo, fill=PUNTO[estado])
        self._mostrar(self.punto)

    def expandir(self):
        if self.contraer_id:
            self.v.after_cancel(self.contraer_id)
            self.contraer_id = None
        if self.punto.winfo_ismapped():
            grabando = self.agente.estado == "grabando"
            for w in self.controles:
                (w.grid if grabando else w.grid_remove)()
            (self.aviso.grid_remove if grabando else self.aviso.grid)()
            self._mostrar(self.panel)

    def programar_contraer(self):
        if self.panel.winfo_ismapped():
            self.contraer_id = self.v.after(900, self.contraer)

    def contraer(self):
        self.contraer_id = None
        x, y = self.v.winfo_pointerxy()
        dentro = self.v.winfo_rootx() <= x < self.v.winfo_rootx() + self.v.winfo_width() and \
            self.v.winfo_rooty() <= y < self.v.winfo_rooty() + self.v.winfo_height()
        if dentro:
            return
        if self.agente.estado == "grabando" or self.agente.estado == "transcribiendo":
            self.mostrar_punto(self.agente.estado)
        elif self.v.winfo_ismapped():
            self.ocultar()  # avisos finales: al quitar el mouse se van

    def marcar(self, activo, nombres):
        for clave, btn, titulo in (("pc", self.btn_pc, "🔊  Audio del PC"), ("mic", self.btn_mic, "🎙  Micrófono")):
            prendido = activo[clave]
            btn.configure(bg=AZUL_SUAVE if prendido else "#f2f2f2", fg=AZUL if prendido else APAGADO,
                          activebackground=AZUL_SUAVE if prendido else "#e8e8e8")
            if clave == "mic" and "mic" not in nombres:
                btn.configure(state="disabled", text="🎙  Sin micrófono")
            else:
                btn.configure(state="normal", text=titulo + ("  ✓" if prendido else ""))


class Agente:
    def __init__(self, raiz):
        self.raiz = raiz
        self.eventos = queue.Queue()
        self.estado = "libre"  # libre → eligiendo → grabando → transcribiendo → libre
        self.prefs = leer_preferencias()
        self.grabadora = None
        self.lotes = None
        self.carga = threading.Lock()
        self.ultimo_uso = 0
        self.ultimo_atajo = 0
        self.ocultar_en = None
        self.mensaje = ""
        self.flotante = Flotante(raiz, self)
        os.makedirs(nucleo.AUDIOS, exist_ok=True)

        import keyboard

        # suppress: Ins ya no hace su función normal (sobrescribir en Word); ahora es el botón de grabar
        keyboard.add_hotkey(ATAJO, lambda: self.eventos.put(("atajo", None)), suppress=True)
        raiz.after(100, self.procesar_eventos)
        raiz.after(60_000, self.revisar_modelo)

    # --- modelo (se carga solo al usarlo y se suelta si no se usa) ----------
    def preparar_modelo(self):
        with self.carga:
            if self.lotes is None:
                from faster_whisper import BatchedInferencePipeline
                from transcribir import cargar_modelo

                modelo, _ = cargar_modelo("large-v3-turbo", usar_cpu=False)
                self.lotes = BatchedInferencePipeline(model=modelo)
        self.ultimo_uso = time.time()

    def revisar_modelo(self):
        if self.lotes is not None and self.estado == "libre" and time.time() - self.ultimo_uso > LIBERAR_MODELO_TRAS:
            with self.carga:
                self.lotes = None
            import gc

            gc.collect()
        self.raiz.after(60_000, self.revisar_modelo)

    def precargar(self):
        try:
            self.preparar_modelo()
        except Exception:
            traceback.print_exc()

    # --- acciones -----------------------------------------------------------
    def abrir_menu(self):
        self.estado = "eligiendo"
        self.ocultar_en = time.time() + 15  # si no eliges nada, el menú se va solo
        self.flotante.mostrar_menu(self.prefs["modo"])

    def elegir(self, modo):
        if self.estado != "eligiendo":
            return
        self.prefs["modo"] = modo
        try:
            with open(PREFERENCIAS, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f)
        except OSError:
            pass
        self.base = os.path.join(nucleo.AUDIOS, datetime.datetime.now().strftime("grabacion_%Y-%m-%d_%H-%M-%S"))
        self.grabadora = nucleo.Grabadora(**MODOS[modo][2])
        try:
            self.grabadora.iniciar(self.base)
        except Exception as e:
            self.grabadora = None
            self.estado = "libre"
            self.avisar("error", f"No pude empezar a grabar: {e}")
            return
        self.estado = "grabando"
        self.inicio = time.time()
        self.ocultar_en = None
        self.flotante.marcar(self.grabadora.activo, self.grabadora.nombres)
        self.flotante.mostrar_punto("grabando")
        threading.Thread(target=self.precargar, daemon=True).start()  # el modelo carga mientras grabas
        self.tic()

    def tic(self):
        if self.estado == "grabando":
            self.flotante.reloj.configure(text=nucleo.hora(time.time() - self.inicio))
            self.raiz.after(500, self.tic)

    def cambiar(self, clave):
        if self.estado != "grabando":
            return
        try:
            self.grabadora.cambiar(clave, not self.grabadora.activo[clave])
        except Exception as e:
            self.flotante.btn_mic.configure(text=f"🎙  {e}")
            return
        self.flotante.marcar(self.grabadora.activo, self.grabadora.nombres)

    def detener(self):
        if self.estado != "grabando":
            return
        rutas = self.grabadora.detener()
        self.grabadora = None
        if not rutas:
            self.estado = "libre"
            self.avisar("error", "No se grabó ningún sonido.")
            return
        self.estado = "transcribiendo"
        self.mensaje = "Transcribiendo…"
        self.flotante.aviso.configure(text=self.mensaje)
        self.flotante.mostrar_punto("transcribiendo")
        threading.Thread(target=self.transcribir, args=(rutas,), daemon=True).start()

    def cancelar(self):
        if self.estado == "grabando":
            for ruta in self.grabadora.detener():
                os.remove(ruta)
            self.grabadora = None
        if self.estado in ("grabando", "eligiendo"):
            self.estado = "libre"
        self.flotante.ocultar()  # si está transcribiendo, sigue por detrás y se guarda igual

    def transcribir(self, rutas):
        try:
            self.preparar_modelo()
            segmentos = []
            avisar = lambda t, x, d: (segmentos.append((t, x)), self.eventos.put(("avance", (t, d))))
            duracion = nucleo.transcribir(self.lotes, nucleo.mezclar(rutas), None, avisar, segmentos.clear)
            self.ultimo_uso = time.time()
            self.eventos.put(("terminado", (segmentos, duracion)))
        except Exception:
            self.eventos.put(("error", traceback.format_exc()))

    def avisar(self, color, texto):
        # Aviso discreto: puntito de color unos segundos; el texto se ve al pasar el mouse
        self.flotante.aviso.configure(text=texto)
        self.flotante.mostrar_punto(color)
        self.ocultar_en = time.time() + (5 if color == "listo" else 10)

    # --- bucle --------------------------------------------------------------
    def procesar_eventos(self):
        while not self.eventos.empty():
            tipo, dato = self.eventos.get()
            if tipo == "atajo":
                # Con suppress la tecla a veces llega dos veces por pulsación: se ignoran repeticiones en menos de 1 s
                if time.time() - self.ultimo_atajo < 1:
                    continue
                self.ultimo_atajo = time.time()
                if self.estado == "libre":
                    self.abrir_menu()
                elif self.estado == "eligiendo":
                    self.elegir(self.prefs["modo"])
                elif self.estado == "grabando":
                    self.detener()
            elif tipo == "avance":
                t, d = dato
                self.flotante.aviso.configure(text=f"Transcribiendo…  {nucleo.hora(t)} / {nucleo.hora(d)}")
            elif tipo == "terminado":
                segmentos, duracion = dato
                self.estado = "libre"
                if not segmentos:
                    self.avisar("error", "No escuché voz en la grabación. No se guardó nada.")
                    continue
                texto = nucleo.guardar(os.path.basename(self.base), segmentos)
                self.raiz.clipboard_clear()
                self.raiz.clipboard_append(texto)
                self.avisar("listo", f"✓  Copiado (Ctrl+V) y guardado en tu historial  ·  {duracion / 60:.1f} min")
            elif tipo == "error":
                print(dato, file=sys.stderr, flush=True)
                self.estado = "libre"
                self.avisar("error", "Ocurrió un error al transcribir. Revisa agente.log.")
        if self.ocultar_en and time.time() > self.ocultar_en:
            self.ocultar_en = None
            if self.estado == "eligiendo":
                self.cancelar()
            elif self.estado == "libre":
                self.flotante.ocultar()
        self.raiz.after(100, self.procesar_eventos)


if __name__ == "__main__":
    # Tk se queda con el foco al arrancar aunque su ventana esté oculta: se le devuelve a la app de antes
    previa = ctypes.windll.user32.GetForegroundWindow()
    raiz = tk.Tk()
    raiz.withdraw()
    Agente(raiz)
    raiz.update()
    if previa:
        ctypes.windll.user32.SetForegroundWindow(previa)
    raiz.mainloop()
