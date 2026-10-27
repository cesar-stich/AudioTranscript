"""Partes comunes de AudioTranscript (ventana y agente de la tecla Ins): carpetas, grabación y transcripción."""
import datetime
import os
import time
import wave

CARPETA = os.path.join(os.path.expanduser("~"), "Documents", "Transcripciones")
AUDIOS = os.path.join(CARPETA, "audios")


def hora(segundos):
    return str(datetime.timedelta(seconds=int(segundos)))


class _Pista:
    """Una fuente de audio grabándose a un WAV, alineada en el tiempo con las demás.

    WASAPI no entrega nada mientras el PC está en silencio, y una fuente apagada no debe sonar:
    en ambos casos se escribe silencio para que todas las pistas duren lo mismo y se puedan mezclar.
    """

    def __init__(self, pa, pyaudio, dispositivo, ruta, inicio, activa):
        self.ruta, self.inicio, self.activa = ruta, inicio, activa
        self.canales = dispositivo["maxInputChannels"]
        self.tasa = int(dispositivo["defaultSampleRate"])
        self.escritos = 0
        self.usada = False
        self.archivo = wave.open(ruta, "wb")
        self.archivo.setnchannels(self.canales)
        self.archivo.setsampwidth(2)
        self.archivo.setframerate(self.tasa)
        self.stream = pa.open(
            format=pyaudio.paInt16, channels=self.canales, rate=self.tasa, input=True,
            input_device_index=dispositivo["index"], frames_per_buffer=1024,
            stream_callback=lambda datos, n, *_: self.al_recibir(datos, n, pyaudio.paContinue),
        )

    def _silencio_hasta(self, cuadros):
        if cuadros > self.escritos:
            self.archivo.writeframes(b"\0" * (cuadros - self.escritos) * self.canales * 2)
            self.escritos = cuadros

    def al_recibir(self, datos, n, seguir):
        esperado = int((time.monotonic() - self.inicio) * self.tasa) - n
        if esperado - self.escritos > self.tasa // 10:
            self._silencio_hasta(esperado)
        if self.activa():
            self.archivo.writeframes(datos)
            self.escritos += n
            self.usada = True
        else:
            self._silencio_hasta(self.escritos + n)
        return (None, seguir)

    def cerrar(self, fin):
        self.stream.stop_stream()
        self.stream.close()
        self._silencio_hasta(int((fin - self.inicio) * self.tasa))
        self.archivo.close()


class Grabadora:
    """Graba el audio del PC ("pc", lo que suena) y/o el micrófono ("mic"); cada uno se puede prender o apagar a mitad."""

    def __init__(self, pc=True, mic=False):
        self.activo = {"pc": pc, "mic": mic}
        self.pistas = {}
        self.nombres = {}

    def iniciar(self, base):
        import pyaudiowpatch as pyaudio

        self.pyaudio = pyaudio
        self.pa = pyaudio.PyAudio()
        self.base = base
        self.inicio = time.monotonic()
        wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        salida = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        self.dispositivos = {
            "pc": next((d for d in self.pa.get_loopback_device_info_generator() if salida["name"] in d["name"]), None),
            "mic": self.pa.get_device_info_by_index(wasapi["defaultInputDevice"]) if wasapi["defaultInputDevice"] >= 0 else None,
        }
        self.nombres = {k: d["name"].replace(" [Loopback]", "") for k, d in self.dispositivos.items() if d}
        try:
            for clave, prendida in self.activo.items():
                if prendida:
                    self._abrir(clave)
        except Exception:
            self.pa.terminate()
            raise

    def _abrir(self, clave):
        # El micrófono se abre recién cuando se prende, para que Windows no lo marque en uso si no hace falta
        if clave in self.pistas:
            return
        if self.dispositivos[clave] is None:
            raise RuntimeError("No encontré micrófono." if clave == "mic" else "No encontré la salida de audio del PC.")
        ruta = self.base + (".wav" if clave == "pc" else "_mic.wav")
        self.pistas[clave] = _Pista(self.pa, self.pyaudio, self.dispositivos[clave], ruta, self.inicio,
                                    lambda: self.activo[clave])

    def cambiar(self, clave, prendida):
        if prendida:
            self._abrir(clave)
        self.activo[clave] = prendida

    def detener(self):
        """Cierra todo y devuelve los WAV que llegaron a tener sonido de una fuente prendida."""
        fin = time.monotonic()
        rutas = []
        for pista in self.pistas.values():
            pista.cerrar(fin)
            if pista.usada:
                rutas.append(pista.ruta)
            else:
                os.remove(pista.ruta)
        self.pa.terminate()
        return rutas


def mezclar(rutas):
    """Une las pistas (PC + micrófono) en un solo audio de 16 kHz listo para Whisper."""
    import numpy as np
    from faster_whisper import decode_audio

    pistas = [decode_audio(r) for r in rutas]
    mezcla = np.zeros(max(len(p) for p in pistas), dtype=np.float32)
    for p in pistas:
        mezcla[: len(p)] += p
    pico = float(np.abs(mezcla).max()) if len(mezcla) else 0
    return mezcla / pico * 0.95 if pico > 1 else mezcla


def transcribir(lotes, audio, idioma, al_segmento, al_reintento):
    """Transcribe el audio (16 kHz) avisando cada frase; devuelve la duración en segundos."""
    duracion = len(audio) / 16000
    palabras = 0
    segmentos, _ = lotes.transcribe(audio, language=idioma, batch_size=16, beam_size=5)
    for s in segmentos:
        palabras += len(s.text.split())
        al_segmento(s.start, s.text.strip(), duracion)

    # El filtro de voz descarta el canto sobre música: si casi no quedó texto, repetir sin filtro en tramos de 30 s
    if palabras < duracion / 60 * 15:
        al_reintento()
        tramos = [{"start": t, "end": min(t + 30, duracion)} for t in range(0, int(duracion) + 1, 30) if t < duracion]
        segmentos, _ = lotes.transcribe(audio, language=idioma, batch_size=16, beam_size=5, clip_timestamps=tramos)
        for s in segmentos:
            al_segmento(s.start, s.text.strip(), duracion)
    return duracion


def guardar(nombre, segmentos):
    """Guarda <nombre>.txt y <nombre>_tiempos.txt en el historial y devuelve el texto limpio."""
    if os.path.splitext(nombre)[1].lower() in (".mp4", ".m4a", ".mov", ".mkv", ".webm", ".mp3", ".wav"):
        nombre = os.path.splitext(nombre)[0]  # títulos de Drive/OneDrive traen la extensión: "reunion.mp4.mp4"
    base = os.path.join(CARPETA, nombre)
    texto = " ".join(x for _, x in segmentos)
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(texto)
    with open(base + "_tiempos.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(f"[{hora(t)}] {x}" for t, x in segmentos))
    return texto
