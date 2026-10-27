"""Instalador de un clic de AudioTranscript (se compila a Instalar-AudioTranscript.exe).

1. Busca Python 3.10+; si no hay, instala Python 3.12 para el usuario (winget o el instalador oficial de python.org).
2. Baja la última versión de AudioTranscript desde GitHub.
3. Ejecuta su instalar.py: librerías, GPU, modelo Whisper, accesos directos y la tecla Ins.
Solo usa la biblioteca estándar, para que el .exe pese poco.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

REPO_ZIP = "https://github.com/cesar-stich/AudioTranscript/archive/refs/heads/main.zip"
PYTHON_WINGET = "Python.Python.3.12"
PYTHON_OFICIAL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"


def titulo(texto):
    print(f"\n» {texto}", flush=True)


def terminar(codigo, texto=""):
    if texto:
        print(f"\n✗ {texto}")
    try:
        input("\nPresiona Enter para cerrar...")
    except EOFError:
        pass
    sys.exit(codigo)


def version_de(python):
    try:
        r = subprocess.run([python, "-c", "import sys; print('%d %d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=30)
        mayor, menor = map(int, r.stdout.split())
        return (mayor, menor)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def buscar_python():
    candidatos = []
    for nombre in ("python", "py"):
        ruta = shutil.which(nombre)
        if ruta:
            candidatos.append(ruta)
    base = os.environ.get("LOCALAPPDATA", "")
    candidatos += sorted(glob.glob(os.path.join(base, "Programs", "Python", "Python3*", "python.exe")), reverse=True)
    candidatos += sorted(glob.glob(r"C:\Program Files\Python3*\python.exe"), reverse=True)
    for c in candidatos:
        v = version_de(c)
        if v and v >= (3, 10):
            if os.path.basename(c).lower() == "py.exe":  # el lanzador "py": pedirle la ruta real de python.exe
                c = subprocess.run([c, "-c", "import sys; print(sys.executable)"], capture_output=True, text=True).stdout.strip()
            return c, v
    return None, None


def instalar_python():
    titulo("No encontré Python: instalando Python 3.12 (solo para tu usuario, unos minutos)...")
    if shutil.which("winget"):
        r = subprocess.run(["winget", "install", "-e", "--id", PYTHON_WINGET, "--scope", "user", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"])
        if r.returncode == 0:
            return
        print("  winget no pudo; uso el instalador oficial de python.org.")
    destino = os.path.join(tempfile.gettempdir(), "python-3.12-instalador.exe")
    urllib.request.urlretrieve(PYTHON_OFICIAL, destino)
    subprocess.run([destino, "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1"], check=True)


def main():
    print("\n  AudioTranscript  ·  instalador de un clic")
    print("  =========================================")
    print("  Graba y transcribe gratis en tu PC. Esto tarda unos minutos y baja ~3 GB (solo esta vez).")

    titulo("Buscando Python...")
    python, version = buscar_python()
    if not python:
        try:
            instalar_python()
        except Exception as e:
            terminar(1, f"No pude instalar Python ({e}).\nInstálalo a mano desde https://www.python.org/downloads/ y vuelve a abrir este instalador.")
        python, version = buscar_python()
        if not python:
            terminar(1, "Python se instaló, pero no lo encuentro. Reinicia la PC y vuelve a abrir este instalador.")
    print(f"  Python {version[0]}.{version[1]}  ({python})")

    titulo("Bajando la última versión de AudioTranscript...")
    carpeta = tempfile.mkdtemp(prefix="audiotranscript_")
    try:
        zip_ruta = os.path.join(carpeta, "app.zip")
        urllib.request.urlretrieve(REPO_ZIP, zip_ruta)
        with zipfile.ZipFile(zip_ruta) as z:
            z.extractall(carpeta)
        origen = next(d for d in glob.glob(os.path.join(carpeta, "AudioTranscript-*")) if os.path.isdir(d))
    except Exception as e:
        terminar(1, f"No pude bajar AudioTranscript ({e}). Revisa tu conexión a internet.")

    entorno = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([python, os.path.join(origen, "instalar.py")], cwd=origen, env=entorno)
    shutil.rmtree(carpeta, ignore_errors=True)
    terminar(r.returncode)


if __name__ == "__main__":
    if not sys.stdout.isatty():  # en una consola normal ya sale bien; redirigido, que no se rompan las tildes
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except KeyboardInterrupt:
        terminar(1, "Instalación cancelada.")
