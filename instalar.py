"""Instalador de AudioTranscript: librerías, aceleración por GPU, modelo Whisper y accesos directos.

Uso normal: doble clic en instalar.bat.  Pruebas: python instalar.py --destino <carpeta> --sin-accesos
"""
import argparse
import os
import shutil
import subprocess
import sys

ORIGEN = os.path.dirname(os.path.abspath(__file__))
ARCHIVOS = ["AudioTranscript.pyw", "agente.pyw", "nucleo.py", "transcribir.py", "icono.ico"]


def paso(texto):
    print(f"\n{texto}", flush=True)


def fallo(texto):
    print(f"\n✗ {texto}")
    sys.exit(1)


def pip(*paquetes):
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "--disable-pip-version-check", *paquetes])
    return r.returncode == 0


def carpeta_especial(nombre):
    # Escritorio, menú Inicio e Inicio de Windows pueden estar movidos (por ejemplo a OneDrive)
    r = subprocess.run(["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{nombre}')"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def acceso_directo(ruta, pythonw, script, destino, descripcion):
    comando = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:AT_RUTA); "
        "$s.TargetPath = $env:AT_PYTHONW; $s.Arguments = '\"' + $env:AT_SCRIPT + '\"'; "
        "$s.WorkingDirectory = $env:AT_DESTINO; $s.IconLocation = $env:AT_ICONO; "
        "$s.Description = $env:AT_DESCRIPCION; $s.Save()"
    )
    entorno = {**os.environ, "AT_RUTA": ruta, "AT_PYTHONW": pythonw, "AT_SCRIPT": os.path.join(destino, script),
               "AT_DESTINO": destino, "AT_ICONO": os.path.join(destino, "icono.ico") + ",0", "AT_DESCRIPCION": descripcion}
    subprocess.run(["powershell", "-NoProfile", "-Command", comando], env=entorno, check=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--destino", default=os.path.join(os.environ["LOCALAPPDATA"], "Programs", "AudioTranscript"))
    p.add_argument("--sin-accesos", action="store_true", help="no crear íconos ni abrir la app (para pruebas)")
    a = p.parse_args()

    print("\n  AudioTranscript  ·  graba y transcribe gratis en tu PC")
    print("  ------------------------------------------------------")

    paso(f"[1/5] Python {sys.version.split()[0]}")
    if sys.version_info < (3, 10):
        fallo("Necesitas Python 3.10 o más nuevo: https://www.python.org/downloads/")

    paso("[2/5] Instalando librerías (unos minutos la primera vez)...")
    if not pip("-r", os.path.join(ORIGEN, "requirements.txt")):
        fallo("No se pudieron instalar las librerías. Revisa tu conexión y vuelve a intentar.")
    if shutil.which("nvidia-smi"):
        print("      Tarjeta NVIDIA detectada: instalando aceleración por GPU...")
        if not pip("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"):
            fallo("No se pudo instalar la aceleración por GPU.")
    else:
        print("      Sin tarjeta NVIDIA: se usará el procesador (más lento, pero funciona).")

    paso(f"[3/5] Copiando la app a {a.destino} ...")
    if not a.sin_accesos:
        # Si ya había un agente de la tecla Ins corriendo, se cierra para reemplazarlo por el nuevo
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Get-CimInstance Win32_Process -Filter \"Name like 'pythonw%'\" | "
                        "Where-Object { $_.CommandLine -match 'agente\\.pyw' } | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }"])
    os.makedirs(a.destino, exist_ok=True)
    for f in ARCHIVOS:
        shutil.copy2(os.path.join(ORIGEN, f), a.destino)

    paso("[4/5] Descargando el modelo Whisper large-v3-turbo (~1.6 GB, solo la primera vez)...")
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    from faster_whisper.utils import download_model

    download_model("large-v3-turbo")

    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = shutil.which("pythonw") or pythonw
    if a.sin_accesos:
        paso("[5/5] (prueba) Sin accesos directos.")
    else:
        paso("[5/5] Creando accesos directos...")
        for carpeta in ("Desktop", "Programs"):
            acceso_directo(os.path.join(carpeta_especial(carpeta), "AudioTranscript.lnk"), pythonw,
                           "AudioTranscript.pyw", a.destino, "Graba y transcribe gratis")
        print("      Ícono en el escritorio y en el menú Inicio.")
        respuesta = input("\n      ¿Quieres que la tecla Ins funcione siempre, desde que prendes la PC? (S/N): ")
        if respuesta.strip().lower().startswith("s"):
            acceso_directo(os.path.join(carpeta_especial("Startup"), "AudioTranscript (tecla Ins).lnk"), pythonw,
                           "agente.pyw", a.destino, "Tecla Ins: grabar y transcribir")
            print("      Listo: la tecla Ins estará activa cada vez que prendas la PC.")
        subprocess.Popen([pythonw, os.path.join(a.destino, "agente.pyw")], cwd=a.destino)

    print("\n  ✓ AudioTranscript quedó instalado.")
    print("    · Abre 'AudioTranscript' desde el escritorio para transcribir archivos o links.")
    print("    · Presiona Ins en cualquier momento para grabar el audio de tu PC.\n")


if __name__ == "__main__":
    main()
