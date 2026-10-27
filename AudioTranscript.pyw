"""AudioTranscript: graba el audio del PC, abre un archivo o baja un video de YouTube y lo transcribe con Whisper en tu GPU."""
import ctypes
import datetime
import glob
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback

# Con pythonw no hay consola: lo que las librerías escriban va a un registro junto a la app
if sys.stdout is None or sys.stderr is None:
    registro = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "registro.log"), "w", encoding="utf-8")
    sys.stdout = sys.stdout or registro
    sys.stderr = sys.stderr or registro
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import nucleo
from nucleo import AUDIOS, CARPETA, hora
from transcribir import cargar_modelo

IDIOMAS = {"Detectar solo": None, "Español": "es", "Inglés": "en", "Portugués": "pt"}
FORMATOS = [("Audio o video", "*.mp4 *.mp3 *.m4a *.wav *.webm *.mkv *.mov *.ogg *.flac *.aac"), ("Todos", "*.*")]


def lanzar_agente():
    # El agente de la tecla Ins vive aparte (arranca con Windows); si no está corriendo, se lanza
    if ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\AudioTranscriptAgente"):
        return
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    agente = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agente.pyw")
    subprocess.Popen([pythonw if os.path.exists(pythonw) else "pythonw", agente], cwd=os.path.dirname(agente))


class NecesitaClave(Exception):
    pass


def es_onedrive(url):
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return host == "1drv.ms" or "onedrive" in host or host.endswith(".sharepoint.com") or "microsoftpersonalcontent" in host


def bajar_onedrive(url, avisar):
    # yt-dlp no conoce OneDrive: se prueban las formas de convertir un link público en descarga directa
    import base64
    from urllib.parse import parse_qs, quote, unquote, urlparse

    import requests

    sesion = requests.Session()
    sesion.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    con_descarga = lambda u: u + ("&" if "?" in u else "?") + "download=1"
    clave = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    candidatos = [con_descarga(url), f"https://api.onedrive.com/v1.0/shares/u!{clave}/root/content"]
    try:
        final = sesion.get(url, timeout=30).url
        q = parse_qs(urlparse(final).query)
        if "resid" in q:
            auth = f"&authkey={quote(q['authkey'][0])}" if "authkey" in q else ""
            candidatos.append(f"https://onedrive.live.com/download?resid={quote(q['resid'][0])}{auth}")
        if final != url:
            candidatos.append(con_descarga(final))
    except requests.RequestException:
        pass

    for candidato in candidatos:
        try:
            r = sesion.get(candidato, stream=True, timeout=30)
        except requests.RequestException:
            continue
        tipo = r.headers.get("Content-Type", "")
        if not r.ok or tipo.startswith("text/") or "json" in tipo:
            r.close()
            continue
        nombre = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", r.headers.get("Content-Disposition", ""))
        nombre = unquote(nombre.group(1) or nombre.group(2)) if nombre else datetime.datetime.now().strftime("onedrive_%Y-%m-%d_%H-%M.mp4")
        ruta = os.path.join(AUDIOS, re.sub(r'[\\/:*?"<>|]', "_", nombre))
        total, bajado = int(r.headers.get("Content-Length", 0)), 0
        with open(ruta, "wb") as f:
            for trozo in r.iter_content(1 << 20):
                f.write(trozo)
                bajado += len(trozo)
                if total:
                    avisar(bajado / total * 100)
        return ruta
    raise RuntimeError("No pude bajar ese link de OneDrive/SharePoint directamente.\n\n"
                       "Lo más rápido: abre el link en tu navegador, dale a 'Descargar' y luego usa 📂 Abrir archivo.\n\n"
                       "Si no aparece 'Descargar' (grabaciones de Teams de la universidad o empresa), "
                       "ponle play y usa ● Grabar audio del PC.")


def bajar_video(url, avisar, clave=None):
    if es_onedrive(url):
        return bajar_onedrive(url, avisar)

    import yt_dlp

    def gancho(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d["status"] == "downloading" and total:
            avisar(d["downloaded_bytes"] / total * 100)

    opciones = {
        # Solo audio si existe; si no, el video más liviano con sonido conocido, la cámara en Zoom ("view") o el normal
        "format": "bestaudio[ext=m4a]/bestaudio/worst[acodec!=none]/view/best",
        "outtmpl": os.path.join(AUDIOS, "%(title).80s.%(ext)s"),
        "windowsfilenames": True, "noplaylist": True,
        "quiet": True, "no_warnings": True, "noprogress": True,
        "progress_hooks": [gancho],
    }
    if clave:
        opciones["videopassword"] = clave
    try:
        import deno  # YouTube exige un motor JavaScript; el paquete pip "deno" trae uno
        opciones["js_runtimes"] = {"deno": {"path": deno.find_deno_bin()}}
    except Exception:
        pass
    try:
        with yt_dlp.YoutubeDL(opciones) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)
    except yt_dlp.utils.DownloadError as e:
        texto = str(e)
        if re.search(r"video-password|wrong pass", texto, re.I):
            raise NecesitaClave("Código incorrecto." if clave else "")
        if re.search(r"logged-in|log in|login|sign in|cookies|private|HTTP Error 40[13]", texto, re.I):
            raise RuntimeError("Ese video no es público: la página pide iniciar sesión.\n\n"
                               "Ponle play y usa ● Grabar audio del PC.")
        if "Unsupported URL" in texto:
            raise RuntimeError("No sé bajar videos de esa página.\n\nPonle play y usa ● Grabar audio del PC.")
        raise RuntimeError(f"{texto}\n\nSi el link es público y aun así falla, la página cambió algo: "
                           "abre una terminal y corre  python -m pip install -U yt-dlp")


class App:
    def __init__(self, raiz, archivo_inicial=None):
        self.raiz = raiz
        self.modelo = None
        self.grabadora = None
        self.ocupado = False
        self.eventos = queue.Queue()
        self.archivo_inicial = archivo_inicial
        self.segmentos = []
        self.historial_rutas = []

        raiz.title("AudioTranscript")
        raiz.geometry("1080x660")
        raiz.minsize(760, 460)
        icono = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icono.ico")
        if os.path.exists(icono):
            raiz.iconbitmap(icono)

        estilo = ttk.Style()
        estilo.theme_use("vista" if "vista" in estilo.theme_names() else "clam")
        estilo.configure("Grande.TButton", font=("Segoe UI", 11), padding=(14, 8))
        estilo.configure("Titulo.TLabel", font=("Segoe UI", 11, "bold"))

        cuerpo = ttk.PanedWindow(raiz, orient="horizontal")
        cuerpo.pack(fill="both", expand=True)
        lado = ttk.Frame(cuerpo, padding=(12, 12, 6, 12))
        principal = ttk.Frame(cuerpo)
        cuerpo.add(lado, weight=1)
        cuerpo.add(principal, weight=3)

        # --- historial (izquierda) ---
        ttk.Label(lado, text="Historial", style="Titulo.TLabel").pack(anchor="w")
        self.buscar = tk.StringVar()
        self.buscar.trace_add("write", lambda *_: self.cargar_historial())
        fila_buscar = ttk.Frame(lado)
        fila_buscar.pack(fill="x", pady=(6, 6))
        ttk.Label(fila_buscar, text="🔍").pack(side="left")
        ttk.Entry(fila_buscar, textvariable=self.buscar).pack(side="left", fill="x", expand=True, padx=(4, 0))
        marco_lista = ttk.Frame(lado)
        marco_lista.pack(fill="both", expand=True)
        self.lista = tk.Listbox(marco_lista, font=("Segoe UI", 10), activestyle="none", relief="solid", borderwidth=1, height=8, width=34)
        desliz_lista = ttk.Scrollbar(marco_lista, command=self.lista.yview)
        self.lista.configure(yscrollcommand=desliz_lista.set)
        desliz_lista.pack(side="right", fill="y")
        self.lista.pack(side="left", fill="both", expand=True)
        self.lista.bind("<<ListboxSelect>>", self.abrir_historial)

        # --- acciones (derecha) ---
        barra = ttk.Frame(principal, padding=(6, 12, 12, 6))
        barra.pack(fill="x")
        self.btn_grabar = ttk.Button(barra, text="●  Grabar audio del PC", style="Grande.TButton", command=self.alternar_grabacion)
        self.btn_grabar.pack(side="left")
        self.btn_abrir = ttk.Button(barra, text="📂  Abrir archivo", style="Grande.TButton", command=self.abrir_archivo)
        self.btn_abrir.pack(side="left", padx=8)
        ttk.Label(barra, text="Idioma:").pack(side="left", padx=(16, 4))
        self.idioma = ttk.Combobox(barra, values=list(IDIOMAS), state="readonly", width=13)
        self.idioma.current(0)
        self.idioma.pack(side="left")

        fila_yt = ttk.Frame(principal, padding=(6, 0, 12, 8))
        fila_yt.pack(fill="x")
        ttk.Label(fila_yt, text="Link del video:").pack(side="left")
        self.url = tk.StringVar()
        self.entrada_url = ttk.Entry(fila_yt, textvariable=self.url, font=("Segoe UI", 10))
        self.entrada_url.pack(side="left", fill="x", expand=True, padx=8)
        self.entrada_url.bind("<Return>", lambda _: self.transcribir_youtube())
        self.btn_youtube = ttk.Button(fila_yt, text="▶  Transcribir link", command=self.transcribir_youtube)
        self.btn_youtube.pack(side="left")
        ttk.Label(principal, text="Sirve con YouTube, Google Drive, OneDrive/SharePoint, grabaciones de Zoom, Loom, Facebook, TikTok… (links públicos)",
                  foreground="#777", font=("Segoe UI", 9), padding=(8, 0, 0, 6)).pack(anchor="w")

        self.estado = tk.StringVar(value="Cargando modelo...")
        ttk.Label(principal, textvariable=self.estado, font=("Segoe UI", 10), padding=(8, 0)).pack(anchor="w")
        self.progreso = ttk.Progressbar(principal, mode="determinate", maximum=100)
        self.progreso.pack(fill="x", padx=(6, 12), pady=(4, 6))

        marco = ttk.Frame(principal, padding=(6, 0, 12, 0))
        marco.pack(fill="both", expand=True)
        self.texto = tk.Text(marco, wrap="word", font=("Segoe UI", 11), relief="solid", borderwidth=1, padx=10, pady=8, height=8)
        desliz = ttk.Scrollbar(marco, command=self.texto.yview)
        self.texto.configure(yscrollcommand=desliz.set)
        desliz.pack(side="right", fill="y")
        self.texto.pack(side="left", fill="both", expand=True)

        pie = ttk.Frame(principal, padding=(6, 12, 12, 12))
        pie.pack(fill="x")
        self.con_tiempos = tk.BooleanVar(value=False)
        ttk.Checkbutton(pie, text="Mostrar marcas de tiempo", variable=self.con_tiempos, command=self.refrescar_texto).pack(side="left")
        ttk.Button(pie, text="Abrir carpeta", command=lambda: os.startfile(CARPETA)).pack(side="right")
        ttk.Button(pie, text="📋  Copiar texto", command=self.copiar).pack(side="right", padx=8)

        os.makedirs(AUDIOS, exist_ok=True)
        self.cargar_historial()
        self.habilitar(False)
        threading.Thread(target=self.cargar, daemon=True).start()
        try:
            lanzar_agente()
        except Exception:
            traceback.print_exc()
        # Lo que se graba con la tecla Ins aparece en el historial al volver a esta ventana
        raiz.bind("<FocusIn>", lambda e: e.widget is raiz and not self.ocupado and self.cargar_historial())
        raiz.after(100, self.procesar_eventos)
        raiz.protocol("WM_DELETE_WINDOW", self.cerrar)

    # --- hilos de trabajo -------------------------------------------------
    def cargar(self):
        try:
            from faster_whisper import BatchedInferencePipeline

            self.modelo, dispositivo = cargar_modelo("large-v3-turbo", usar_cpu=False)
            self.lotes = BatchedInferencePipeline(model=self.modelo)  # ~2x más rápido en GPU
            self.eventos.put(("listo", dispositivo))
        except Exception:
            self.eventos.put(("error", traceback.format_exc()))

    def descargar(self, url, clave=None):
        try:
            ruta = bajar_video(url, lambda pct: self.eventos.put(("descarga", pct)), clave)
            self.eventos.put(("descargado", ruta))
        except NecesitaClave as e:
            self.eventos.put(("pide_clave", (url, str(e))))
        except Exception as e:
            self.eventos.put(("error", f"No pude bajar el video.\n\n{e}"))

    def transcribir(self, ruta, idioma):
        try:
            from faster_whisper import decode_audio

            inicio = time.time()
            duracion = nucleo.transcribir(
                self.lotes, decode_audio(ruta), idioma,
                lambda t, x, d: self.eventos.put(("segmento", (t, x, d))),
                lambda: self.eventos.put(("reintento", None)),
            )
            self.eventos.put(("terminado", (ruta, time.time() - inicio, duracion)))
        except Exception:
            self.eventos.put(("error", traceback.format_exc()))

    # --- acciones ---------------------------------------------------------
    def alternar_grabacion(self):
        if self.grabadora is None:
            base = os.path.join(AUDIOS, datetime.datetime.now().strftime("grabacion_%Y-%m-%d_%H-%M-%S"))
            self.grabadora = nucleo.Grabadora(pc=True, mic=False)
            try:
                self.grabadora.iniciar(base)
            except Exception as e:
                self.grabadora = None
                messagebox.showerror("AudioTranscript", str(e))
                return
            self.inicio_grabacion = time.time()
            self.habilitar(False)
            self.btn_grabar.state(["!disabled"])
            self.btn_grabar.configure(text="■  Detener y transcribir")
            self.progreso.configure(mode="indeterminate")
            self.progreso.start(15)
            self.dispositivo = self.grabadora.nombres.get("pc", "")
            self.reloj()
        else:
            rutas = self.grabadora.detener()
            self.grabadora = None
            self.progreso.stop()
            self.progreso.configure(mode="determinate")
            self.btn_grabar.configure(text="●  Grabar audio del PC")
            if rutas:
                self.empezar(rutas[0])
            else:
                self.estado.set("No se grabó ningún sonido: ¿estaba sonando algo en el PC?")

    def reloj(self):
        if self.grabadora is None:
            return
        seg = int(time.time() - self.inicio_grabacion)
        self.estado.set(f"● Grabando {seg // 60:02d}:{seg % 60:02d}  —  lo que suena en: {self.dispositivo}. Dale play a tu reunión o video.")
        self.raiz.after(500, self.reloj)

    def abrir_archivo(self):
        ruta = filedialog.askopenfilename(title="Elige un audio o video", filetypes=FORMATOS)
        if ruta:
            self.empezar(ruta)

    def transcribir_youtube(self):
        url = self.url.get().strip()
        if not url:
            try:
                url = self.raiz.clipboard_get().strip()
            except tk.TclError:
                url = ""
        if not re.match(r"https?://", url):
            messagebox.showinfo("AudioTranscript", "Copia el link del video y pégalo en la casilla.")
            return
        self.url.set(url)
        self.ocupado = True
        self.habilitar(False)
        self.progreso["value"] = 0
        self.estado.set("Bajando el audio del video...")
        threading.Thread(target=self.descargar, args=(url,), daemon=True).start()

    def empezar(self, ruta):
        self.ocupado = True
        self.habilitar(False)
        self.segmentos = []
        self.lista.selection_clear(0, "end")
        self.texto.delete("1.0", "end")
        self.progreso["value"] = 0
        self.estado.set(f"Transcribiendo {os.path.basename(ruta)}...")
        idioma = IDIOMAS[self.idioma.get()]
        threading.Thread(target=self.transcribir, args=(ruta, idioma), daemon=True).start()

    def copiar(self):
        contenido = self.texto.get("1.0", "end").strip()
        if contenido:
            self.raiz.clipboard_clear()
            self.raiz.clipboard_append(contenido)
            self.estado.set("Texto copiado. Pégalo donde quieras con Ctrl+V.")

    def cerrar(self):
        if self.grabadora is not None:
            self.grabadora.detener()
        self.raiz.destroy()

    # --- historial --------------------------------------------------------
    def cargar_historial(self):
        filtro = self.buscar.get().strip().lower()
        archivos = [f for f in glob.glob(os.path.join(CARPETA, "*.txt")) if not f.endswith("_tiempos.txt")]
        archivos.sort(key=os.path.getmtime, reverse=True)
        self.lista.delete(0, "end")
        self.historial_rutas = []
        for f in archivos:
            nombre = os.path.splitext(os.path.basename(f))[0]
            if filtro and filtro not in nombre.lower():
                with open(f, encoding="utf-8") as h:
                    if filtro not in h.read().lower():
                        continue
            fecha = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime("%d/%m %H:%M")
            self.lista.insert("end", f"{fecha}   {nombre}")
            self.historial_rutas.append(f)

    def abrir_historial(self, _evento):
        seleccion = self.lista.curselection()
        if not seleccion or self.ocupado or self.grabadora is not None:
            return
        ruta = self.historial_rutas[seleccion[0]]
        tiempos = ruta[:-4] + "_tiempos.txt"
        self.segmentos = []
        if os.path.exists(tiempos):
            with open(tiempos, encoding="utf-8") as f:
                for linea in f:
                    m = re.match(r"\[(\d+):(\d+):(\d+)\] (.*)", linea.strip())
                    if m:
                        h, mi, s, frase = m.groups()
                        self.segmentos.append((int(h) * 3600 + int(mi) * 60 + int(s), frase))
        if not self.segmentos:
            with open(ruta, encoding="utf-8") as f:
                self.segmentos = [(0, f.read())]
        self.refrescar_texto()
        self.estado.set(f"Del historial: {os.path.splitext(os.path.basename(ruta))[0]}")

    # --- interfaz ---------------------------------------------------------
    def habilitar(self, si):
        for w in (self.btn_grabar, self.btn_abrir, self.idioma, self.btn_youtube, self.entrada_url):
            w.state(["!disabled"] if si else ["disabled"])
        if si:
            self.idioma.state(["readonly"])

    def texto_actual(self):
        if self.con_tiempos.get():
            return "\n".join(f"[{hora(t)}] {x}" for t, x in self.segmentos)
        return " ".join(x for _, x in self.segmentos)

    def refrescar_texto(self):
        if self.segmentos:
            self.texto.delete("1.0", "end")
            self.texto.insert("1.0", self.texto_actual())

    def procesar_eventos(self):
        while not self.eventos.empty():
            tipo, dato = self.eventos.get()
            if tipo == "listo":
                self.estado.set(f"Listo ({dato}). Tip: la tecla Ins graba desde cualquier programa, aunque esta ventana esté cerrada.")
                self.habilitar(True)
                if self.archivo_inicial and re.match(r"https?://", self.archivo_inicial):
                    self.url.set(self.archivo_inicial)
                    self.transcribir_youtube()
                elif self.archivo_inicial:
                    self.empezar(self.archivo_inicial)
                    self.archivo_inicial = None
            elif tipo == "descarga":
                self.progreso["value"] = dato
                self.estado.set(f"Bajando el audio del video... {dato:.0f}%")
            elif tipo == "pide_clave":
                url, aviso = dato
                clave = simpledialog.askstring(
                    "Código de acceso", f"{aviso}\nEsta grabación pide un código de acceso (passcode).\nEscríbelo:".lstrip(),
                    parent=self.raiz)
                if clave:
                    self.estado.set("Bajando el audio del video...")
                    threading.Thread(target=self.descargar, args=(url, clave.strip()), daemon=True).start()
                else:
                    self.ocupado = False
                    self.habilitar(True)
                    self.estado.set("Cancelado: la grabación pide un código de acceso.")
            elif tipo == "descargado":
                self.url.set("")
                self.empezar(dato)
            elif tipo == "segmento":
                inicio, frase, duracion = dato
                self.segmentos.append((inicio, frase))
                self.texto.insert("end", f"[{hora(inicio)}] {frase}\n" if self.con_tiempos.get() else frase + " ")
                self.texto.see("end")
                self.progreso["value"] = min(100, inicio / max(duracion, 1) * 100)
                self.estado.set(f"Transcribiendo... {hora(inicio)} / {hora(duracion)}")
            elif tipo == "reintento":
                self.segmentos = []
                self.texto.delete("1.0", "end")
                self.progreso["value"] = 0
                self.estado.set("Casi no detecté voz hablada (¿música?). Transcribiendo todo el audio sin filtro...")
            elif tipo == "terminado":
                ruta, tardo, duracion = dato
                if not self.segmentos:
                    self.progreso["value"] = 0
                    self.ocupado = False
                    self.habilitar(True)
                    self.estado.set("No encontré voz en este audio. No se guardó nada.")
                    continue
                nucleo.guardar(os.path.splitext(os.path.basename(ruta))[0], self.segmentos)
                self.progreso["value"] = 100
                self.ocupado = False
                self.habilitar(True)
                self.copiar()
                self.cargar_historial()
                self.estado.set(f"✓ Listo en {tardo:.0f} s ({duracion / 60:.1f} min de audio). Ya está copiado: pégalo con Ctrl+V.")
            elif tipo == "error":
                print(dato, file=sys.stderr, flush=True)
                self.ocupado = False
                self.habilitar(self.modelo is not None)
                self.estado.set("Ocurrió un error.")
                messagebox.showerror("AudioTranscript", dato[-1500:])
        self.raiz.after(100, self.procesar_eventos)


if __name__ == "__main__":
    raiz = tk.Tk()
    App(raiz, sys.argv[1] if len(sys.argv) > 1 else None)
    raiz.mainloop()
