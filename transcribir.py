"""Grabador + transcriptor local y gratis (Whisper en tu GPU).

Uso:
  python transcribir.py grabar            Graba el audio del PC (lo que suena por los parlantes) y lo transcribe al detenerlo
  python transcribir.py <archivo>         Transcribe un audio/video existente (mp4, mp3, m4a, wav, webm...)

Opciones:
  --idioma es|en|...   Fuerza el idioma (por defecto se detecta solo)
  --modelo NOMBRE      large-v3-turbo (defecto), medium, small...
  --cpu                No usar la GPU

Resultado: <nombre>.txt (texto limpio) y <nombre>_tiempos.txt (con marcas de tiempo),
y el texto queda copiado en el portapapeles listo para pegar.
"""
import argparse
import datetime
import glob
import os
import site
import subprocess
import sys
import threading
import time
import wave

SALIDA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcripciones")


def habilitar_cuda_windows():
    # Las DLL de cuBLAS/cuDNN vienen en los paquetes pip nvidia-*; hay que decirle a Windows dónde están
    for base in site.getsitepackages() + [site.getusersitepackages()]:
        for carpeta in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            os.add_dll_directory(carpeta)
            os.environ["PATH"] = carpeta + os.pathsep + os.environ["PATH"]


def grabar(destino):
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    salida = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    loopback = next(
        (d for d in pa.get_loopback_device_info_generator() if salida["name"] in d["name"]),
        None,
    )
    if loopback is None:
        sys.exit(f"No encontré el dispositivo loopback para '{salida['name']}'.")

    canales = loopback["maxInputChannels"]
    tasa = int(loopback["defaultSampleRate"])
    archivo = wave.open(destino, "wb")
    archivo.setnchannels(canales)
    archivo.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
    archivo.setframerate(tasa)

    def al_recibir(datos, *_):
        archivo.writeframes(datos)
        return (None, pyaudio.paContinue)

    stream = pa.open(
        format=pyaudio.paInt16, channels=canales, rate=tasa, input=True,
        input_device_index=loopback["index"], frames_per_buffer=1024, stream_callback=al_recibir,
    )
    print(f"\n● Grabando el audio de: {salida['name']}")
    print("  Dale play a tu video/reunión. Presiona ENTER para detener.\n")

    inicio = time.time()
    detener = threading.Event()

    def reloj():
        while not detener.is_set():
            seg = int(time.time() - inicio)
            print(f"\r  {seg // 60:02d}:{seg % 60:02d}", end="", flush=True)
            time.sleep(1)

    threading.Thread(target=reloj, daemon=True).start()
    try:
        input()
    except KeyboardInterrupt:
        pass
    detener.set()
    stream.stop_stream()
    stream.close()
    archivo.close()
    pa.terminate()
    print(f"\n  Guardado: {destino}\n")


def cargar_modelo(nombre, usar_cpu):
    from faster_whisper import WhisperModel

    if not usar_cpu:
        try:
            habilitar_cuda_windows()
            return WhisperModel(nombre, device="cuda", compute_type="float16"), "GPU"
        except Exception as e:
            print(f"  (GPU no disponible: {e}; uso CPU)")
    return WhisperModel(nombre, device="cpu", compute_type="int8"), "CPU"


def transcribir(ruta, idioma, modelo_nombre, usar_cpu):
    print(f"Cargando modelo {modelo_nombre}... (la primera vez se descarga, ~1.6 GB)")
    modelo, dispositivo = cargar_modelo(modelo_nombre, usar_cpu)
    print(f"Transcribiendo en {dispositivo}: {os.path.basename(ruta)}")

    inicio = time.time()
    segmentos, info = modelo.transcribe(ruta, language=idioma, vad_filter=True, beam_size=5)
    if not idioma:
        print(f"  Idioma detectado: {info.language} ({info.language_probability:.0%})")

    texto, con_tiempos = [], []
    for s in segmentos:
        marca = str(datetime.timedelta(seconds=int(s.start)))
        linea = s.text.strip()
        texto.append(linea)
        con_tiempos.append(f"[{marca}] {linea}")
        print(f"\r  {marca} / {datetime.timedelta(seconds=int(info.duration))}", end="", flush=True)

    base = os.path.join(SALIDA, os.path.splitext(os.path.basename(ruta))[0])
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(" ".join(texto))
    with open(base + "_tiempos.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(con_tiempos))

    subprocess.run("clip", input=" ".join(texto).encode("utf-16-le"), check=False)
    print(f"\n\n✓ Listo en {time.time() - inicio:.0f} s (audio de {info.duration / 60:.1f} min)")
    print(f"  {base}.txt")
    print(f"  {base}_tiempos.txt")
    print("  El texto ya está copiado en tu portapapeles (Ctrl+V).")


def main():
    p = argparse.ArgumentParser(description="Graba y transcribe gratis con Whisper local.")
    p.add_argument("entrada", help="'grabar' o la ruta de un archivo de audio/video")
    p.add_argument("--idioma", default=None)
    p.add_argument("--modelo", default="large-v3-turbo")
    p.add_argument("--cpu", action="store_true")
    a = p.parse_args()

    os.makedirs(SALIDA, exist_ok=True)
    if a.entrada == "grabar":
        ruta = os.path.join(SALIDA, datetime.datetime.now().strftime("grabacion_%Y-%m-%d_%H-%M") + ".wav")
        grabar(ruta)
    else:
        ruta = os.path.abspath(a.entrada.strip('"'))
        if not os.path.exists(ruta):
            sys.exit(f"No existe: {ruta}")
    transcribir(ruta, a.idioma, a.modelo, a.cpu)


if __name__ == "__main__":
    main()
