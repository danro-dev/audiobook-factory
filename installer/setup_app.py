"""Installer for EPUB a Audiobook.

Built into a single Setup.exe with PyInstaller. The exe carries only the light parts (embedded
CPython, this project, uv, the launcher) and downloads everything heavy while it installs:

  * Python dependencies  (torch, kokoro, Supertonic, wxPython, …) via uv from PyPI
  * ffmpeg / ffprobe     (gyan.dev essentials build)
  * the TTS models       (Kokoro voices + Supertonic 3, ~700 MB)

Usage:
  EPUB-Audiobook-Setup.exe                 GUI installer
  EPUB-Audiobook-Setup.exe --silent DIR    install to DIR with no UI (used for testing)
  EPUB-Audiobook-Setup.exe --uninstall DIR remove an installation
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "EPUB a Audiobook"
APP_VERSION = "0.1.0"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Epub2Audiobook"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
# What the app really needs. pyopenjtalk (Japanese) is left out on purpose: it has no Windows
# wheel and would need a C compiler on the user's machine.
REQUIREMENTS = [
    "torch", "torchaudio", "kokoro>=0.9.4", "misaki[en,zh]>=0.9.4", "espeakng-loader",
    "supertonic>=1.3", "wxPython>=4.2", "ebooklib", "beautifulsoup4", "pdfminer.six", "lxml",
    "soundfile", "numpy", "langdetect", "sounddevice", "mutagen",
]


def resource(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def default_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Programs" / APP_NAME


# --------------------------------------------------------------------------- install steps

class Installer:
    def __init__(self, dest: Path, log, progress, finished, shortcuts: bool = True):
        # absolute, or the shortcuts and the uninstall string would be relative to wherever
        # the installer happened to be launched from
        self.dest = Path(dest).expanduser().resolve()
        self.log = log
        self.progress = progress        # progress(fraction, text)
        self.finished = finished        # finished(ok, message)
        self.cancelled = False
        self.shortcuts = shortcuts
        self.log_path = Path(tempfile.gettempdir()) / "epub2audiobook-install.log"

    # -- helpers ---------------------------------------------------------------------
    def _app(self, *parts: str) -> Path:
        return self.dest.joinpath("app", *parts)

    def _env(self) -> dict:
        env = os.environ.copy()
        env["EPUB2AUDIOBOOK_HOME"] = str(self.dest)
        env["HF_HOME"] = str(self.dest / "models" / "hf")
        env["SUPERTONIC_CACHE_DIR"] = str(self.dest / "models" / "supertonic")
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def _stream(self, cmd: list[str], cwd: Path | None = None) -> None:
        self.log("$ " + " ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""))
        proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=self._env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.log(line)
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"falló: {' '.join(cmd[:3])} (código {code})")

    # -- steps -----------------------------------------------------------------------
    def copy_payload(self) -> None:
        payload = resource("payload")
        self.dest.mkdir(parents=True, exist_ok=True)
        self.log(f"Instalando en {self.dest}")
        shutil.copytree(payload / "python", self._app(), dirs_exist_ok=True)
        shutil.copy2(payload / "uv.exe", self._app("uv.exe"))
        shutil.copy2(payload / "launcher.exe", self.dest / f"{APP_NAME}.exe")
        self._app("bin").mkdir(parents=True, exist_ok=True)
        (self.dest / "models").mkdir(exist_ok=True)
        # the uninstaller is this same exe, with a switch
        shutil.copy2(Path(sys.executable if getattr(sys, "frozen", False) else __file__),
                     self.dest / "Uninstall.exe")

    def install_deps(self) -> None:
        self.log("Descargando dependencias de Python (torch, kokoro, Supertonic, wxPython…),"
                 " puede tardar varios minutos.")
        self._stream([str(self._app("uv.exe")), "pip", "install",
                      "--python", str(self._app("python.exe")),
                      "--index-url", "https://pypi.org/simple",
                      *REQUIREMENTS])

    def download_ffmpeg(self) -> None:
        self.log("Descargando ffmpeg…")
        tmp = Path(tempfile.mkdtemp(prefix="ffmpeg-"))
        try:
            archive = tmp / "ffmpeg.zip"
            with urllib.request.urlopen(FFMPEG_URL, timeout=60) as resp, open(archive, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    if self.cancelled:
                        raise RuntimeError("cancelado")
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        self.progress(0.10 + 0.25 * done / total,
                                      f"Descargando ffmpeg… {done // 1048576} de {total // 1048576} MB")
            self.log(f"ffmpeg: {done / 1048576:.0f} MB descargados, descomprimiendo")
            with zipfile.ZipFile(archive) as zf:
                for member in zf.namelist():
                    name = Path(member).name
                    if name in ("ffmpeg.exe", "ffprobe.exe"):
                        with zf.open(member) as src, open(self._app("bin", name), "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        self.log(f"  instalado {name}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def download_models(self) -> None:
        self.log("Descargando los modelos de voz (unos 700 MB)…")
        try:
            self._stream([str(self._app("python.exe")), "-m", "epub2audiobook.warmup"], cwd=self._app())
        except Exception as exc:  # noqa: BLE001 - the app can still fetch them on first use
            self.log(f"AVISO: no se pudieron descargar los modelos ahora ({exc}).")
            self.log("La aplicación los bajará la primera vez que conviertas un libro.")

    def finish(self) -> None:
        self.log("Creando accesos directos…")
        exe = self.dest / f"{APP_NAME}.exe"
        ps = (
            "$s=(New-Object -ComObject WScript.Shell);"
            f"$d=[Environment]::GetFolderPath('Desktop');"
            f"$l=$s.CreateShortcut(\"$d\\{APP_NAME}.lnk\");"
            f"$l.TargetPath='{exe}';$l.WorkingDirectory='{self.dest}';"
            f"$l.IconLocation='{exe},0';$l.Save();"
            "$p=[Environment]::GetFolderPath('Programs');"
            f"$l2=$s.CreateShortcut(\"$p\\{APP_NAME}.lnk\");"
            f"$l2.TargetPath='{exe}';$l2.WorkingDirectory='{self.dest}';"
            f"$l2.IconLocation='{exe},0';$l2.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=False,
                       creationflags=0x08000000)
        self.log("Registrando la desinstalación…")
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
            winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe))
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(self.dest))
            winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                              f'"{self.dest / "Uninstall.exe"}" --uninstall "{self.dest}"')
            winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)

    def run(self) -> None:
        steps = [
            ("Copiando los archivos base", self.copy_payload),
            ("Instalando dependencias de Python", self.install_deps),
            ("Instalando ffmpeg", self.download_ffmpeg),
            ("Descargando los modelos de voz", self.download_models),
            ("Creando accesos directos", self.finish),
        ]
        # every line goes to the on-screen log and to a file, so a failed install can be
        # diagnosed afterwards (the exe itself is windowed: nothing is left on a console)
        screen_log = self.log
        try:
            self.log_path.write_text("", encoding="utf-8")
        except OSError:
            pass

        def both(msg: str) -> None:
            screen_log(msg)
            try:
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
            except OSError:
                pass

        self.log = both
        if not self.shortcuts:
            steps = [s for s in steps if s[1] != self.finish]
        try:
            for text, step in steps:
                if self.cancelled:
                    raise RuntimeError("cancelado")
                self.progress(_PROGRESS_BEFORE[step.__name__], text)
                step()
            self.finished(True, "Listo.")
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.finished(False, str(exc))


_PROGRESS_BEFORE = {
    "copy_payload": 0.02,
    "install_deps": 0.10,
    "download_ffmpeg": 0.45,
    "download_models": 0.75,
    "finish": 0.97,
}


# --------------------------------------------------------------------------- uninstall

def uninstall(dest: Path) -> int:
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    try:
        ps = ("$s=(New-Object -ComObject WScript.Shell);"
              "$d=[Environment]::GetFolderPath('Desktop');"
              "$p=[Environment]::GetFolderPath('Programs');"
              f"Remove-Item -LiteralPath \"$d\\{APP_NAME}.lnk\" -ErrorAction SilentlyContinue;"
              f"Remove-Item -LiteralPath \"$p\\{APP_NAME}.lnk\" -ErrorAction SilentlyContinue")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       creationflags=0x08000000)
    except Exception:
        pass
    # the folder contains this exe: let a detached shell remove it once we exit
    script = Path(tempfile.gettempdir()) / f"uninstall-{int(time.time())}.cmd"
    script.write_text(f'@echo off\r\ntimeout /t 3 >nul\r\nrmdir /s /q "{dest}"\r\ndel "%~f0"\r\n',
                      encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=0x00000008 | 0x00000200)
    return 0


# --------------------------------------------------------------------------- CLI

def run_silent(dest: Path) -> int:
    def log(msg: str) -> None:
        print(msg, flush=True)

    result: dict = {"ok": False, "msg": "sin terminar"}
    inst = Installer(dest, log, lambda frac, text: print(f"[{frac * 100:3.0f}%] {text}", flush=True),
                     lambda ok, msg: result.update(ok=ok, msg=msg), shortcuts=False)
    inst.run()
    print("RESULT:", "OK" if result["ok"] else "FAIL", result["msg"], flush=True)
    return 0 if result["ok"] else 1


# --------------------------------------------------------------------------- GUI

def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(f"{APP_NAME} {APP_VERSION} — Instalador")
    root.geometry("760x520")
    root.minsize(640, 440)

    dest_var = tk.StringVar(value=str(default_dir()))
    frame = tk.Frame(root, padx=14, pady=12)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text=f"Instalar {APP_NAME}", font=("Segoe UI", 15, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w")
    tk.Label(frame, text="Se descargan las dependencias, ffmpeg y los modelos de voz "
                         "(unos 2 GB en total). Necesita conexión a internet.",
             wraplength=700, justify="left").grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))

    tk.Label(frame, text="Carpeta:").grid(row=2, column=0, sticky="w")
    entry = tk.Entry(frame, textvariable=dest_var)
    entry.grid(row=2, column=1, sticky="ew", padx=(6, 6))
    frame.columnconfigure(1, weight=1)

    def choose() -> None:
        path = filedialog.askdirectory(initialdir=dest_var.get(), title="Elegir carpeta")
        if path:
            dest_var.set(str(Path(path) / APP_NAME) if Path(path).name.lower() != APP_NAME.lower() else path)

    tk.Button(frame, text="Cambiar…", command=choose).grid(row=2, column=2, sticky="ew")

    bar = ttk.Progressbar(frame, maximum=100)
    bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 4))
    status = tk.Label(frame, text="Listo para instalar.", anchor="w")
    status.grid(row=4, column=0, columnspan=3, sticky="ew")

    log_box = tk.Text(frame, height=16, wrap="none", font=("Consolas", 9))
    log_box.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(8, 8))
    scroll = ttk.Scrollbar(frame, orient="vertical", command=log_box.yview)
    scroll.grid(row=5, column=3, sticky="ns")
    log_box.configure(yscrollcommand=scroll.set)
    frame.rowconfigure(5, weight=1)

    buttons = tk.Frame(frame)
    buttons.grid(row=6, column=0, columnspan=3, sticky="e")
    install_btn = tk.Button(buttons, text="Instalar", width=14)
    install_btn.pack(side="right")
    close_btn = tk.Button(buttons, text="Cerrar", width=10, command=root.destroy)
    close_btn.pack(side="right", padx=(0, 8))

    def log(msg: str) -> None:
        log_box.insert("end", msg + "\n")
        log_box.see("end")

    def progress(frac: float, text: str) -> None:
        bar["value"] = max(0.0, min(1.0, frac)) * 100
        status.config(text=text)

    def finished(ok: bool, msg: str) -> None:
        install_btn.config(state="normal")
        bar["value"] = 100 if ok else bar["value"]
        status.config(text="Instalación terminada." if ok else f"Error: {msg}")
        if ok:
            if messagebox.askyesno(APP_NAME, "Instalación terminada.\n\n¿Abrir la aplicación ahora?"):
                exe = Path(dest_var.get()) / f"{APP_NAME}.exe"
                if exe.exists():
                    subprocess.Popen([str(exe)], cwd=str(exe.parent))
        else:
            messagebox.showerror(APP_NAME, f"La instalación falló:\n\n{msg}\n\n"
                                           "Revisá la conexión a internet y volvé a intentarlo.")

    def start() -> None:
        install_btn.config(state="disabled")
        log_box.delete("1.0", "end")
        inst = Installer(Path(dest_var.get()), log, progress, finished)
        threading.Thread(target=inst.run, daemon=True).start()

    install_btn.config(command=start)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--uninstall":
        return uninstall(Path(args[1]) if len(args) > 1 else default_dir())
    if args and args[0] == "--silent":
        return run_silent(Path(args[1]) if len(args) > 1 else default_dir())
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
