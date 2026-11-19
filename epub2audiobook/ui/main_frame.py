"""Main wxPython application window: Studio workspace, section editor/visualizer, and multi-book queue."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Optional

import wx
import wx.adv

# pythonw.exe (used by run_gui.bat / launcher.exe) has no console, so sys.stdout/sys.stderr are None.
if sys.stdout is None or sys.stderr is None:
    _null = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or _null
    sys.stderr = sys.stderr or _null

from .. import __version__
from ..audio import AUDIO_FORMATS, find_ffmpeg, format_extension, open_in_explorer, write_wav
from ..converter import ConvertOptions, preview_audio
from ..epub_parser import Block, Book, Chapter, detect_language_from_text, load_book
from ..pdf2epub import ScannedPdfError
from ..player import PlayerFrame
from ..queue_manager import QueueManager
from ..section_editor import (
    ChapterSnapshot,
    WORDS_PER_MIN,
    is_chapter_edited,
    load_project_edits,
    save_project_edits,
    snapshot_book,
)
from ..tts import device_description, make_engine
from ..voices import ENGINE_IDS, ENGINE_LABELS, LANGUAGES, default_voice_for, iso_to_kokoro, voices_for
from .chapter_editor import ChapterInspectorPanel
from .dialogs import AdvancedOptionsDialog, MetadataDialog
from .queue_panel import QueuePanel
from .theme import Palette, get_palette, make_bold

SETTINGS_FILE = Path(os.environ.get("EPUB2AUDIOBOOK_HOME") or Path.home()) / ".epub2audiobook.json"
LEFT_COL_DIP = 315

STATUS_TEXT = {
    "synth": "sintetizando…",
    "encode": "codificando…",
    "done": "✓ listo",
    "cached": "✓ caché",
    "paused": "⏸ pausado",
}


def _error_log_path() -> Path:
    return SETTINGS_FILE.with_name("epub2audiobook-errors.log")


def _log_line(msg: str) -> None:
    try:
        import datetime
        with open(_error_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass


def _record_error(where: str, exc: BaseException) -> None:
    try:
        import datetime
        with open(_error_log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {where} - {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ===\n")
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


class App(wx.App):
    """wx.App that survives unhandled exceptions inside event handlers."""

    def OnExceptionInMainLoop(self) -> bool:  # noqa: N802
        exc = sys.exc_info()[1]
        if exc is None:
            return True
        _record_error("event handler", exc)
        try:
            wx.MessageBox(
                f"Ocurrió un error inesperado, pero la aplicación sigue funcionando.\n\n"
                f"{type(exc).__name__}: {exc}\n\nDetalles: {_error_log_path()}",
                "Error inesperado",
                wx.ICON_ERROR,
            )
        except Exception:
            pass
        return True


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_settings(d: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(d, indent=2), "utf-8")
    except Exception:
        pass


def _wrap_text(win: wx.Window, text: str, width: int) -> str:
    dc = wx.ClientDC(win)
    dc.SetFont(win.GetFont())
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            cand = (cur + " " + word) if cur else word
            if dc.GetTextExtent(cand).width <= width or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return "\n".join(lines)


def fmt_minutes(m: float) -> str:
    if m < 1:
        return f"{int(m * 60)}s"
    h, mm = divmod(int(round(m)), 60)
    return f"{h}h {mm:02d}m" if h else f"{mm}m"


class BookDropTarget(wx.FileDropTarget):
    """Allow dragging .epub or .pdf files straight onto the window."""

    def __init__(self, on_files):
        super().__init__()
        self._on_files = on_files

    def OnDropFiles(self, _x, _y, filenames):  # noqa: N802
        valid = [f for f in filenames if Path(f).suffix.lower() in {".epub", ".pdf"}]
        if valid:
            wx.CallAfter(self._on_files, valid)
            return True
        return False


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=f"Audiobook Factory v{__version__}")
        self._pal: Palette = get_palette()
        self.SetBackgroundColour(self._pal.bg)
        self.SetSize(self.FromDIP(wx.Size(1360, 900)))
        self.SetMinSize(self.FromDIP(wx.Size(1080, 720)))

        self.book: Optional[Book] = None
        self._snapshots: dict[int, ChapterSnapshot] = {}
        self._checked_indices: set[int] = set()
        self._visible_chapters: list[Chapter] = []
        self._chapter_statuses: dict[int, str] = {}
        self._current_book_job_id: Optional[str] = None
        self._setting_picker = False
        self._suppress_check_event = False
        self._loading_book = False

        self.settings = load_settings()
        self._adv_options = {
            "max_chars": int(self.settings.get("max_chars", 350)),
            "pause_chunk": float(self.settings.get("pause_chunk", 0.15)),
            "pause_para": float(self.settings.get("pause_para", 0.55)),
            "pause_heading": float(self.settings.get("pause_heading", 1.0)),
            "pause_break": float(self.settings.get("pause_break", 1.3)),
            "pause_chapter_start": float(self.settings.get("pause_chapter_start", 0.7)),
            "pause_chapter_end": float(self.settings.get("pause_chapter_end", 1.2)),
            "target_rms_db": float(self.settings.get("target_rms_db", -20.0)),
        }

        self._preview_engine: Optional[object] = None
        self._preview_engine_key: Optional[tuple] = None
        self._preview_sound: Optional[wx.adv.Sound] = None
        self._player: Optional[PlayerFrame] = None
        self._last_output: Optional[str] = None

        self.queue_mgr = QueueManager(
            on_update=lambda jid: wx.CallAfter(self._on_queue_update, jid),
            on_log=lambda jid, msg: wx.CallAfter(self._log, f"[{jid}] {msg}"),
            on_chapter=lambda jid, idx, st: wx.CallAfter(self._on_job_chapter, jid, idx, st),
        )
        self.queue_mgr.set_hybrid_parallel(bool(self.settings.get("hybrid_parallel", False)))

        self._build_ui()
        self.SetDropTarget(BookDropTarget(self._on_files_dropped))
        self.CreateStatusBar()
        self.SetStatusText(f"Cómputo: {device_description()}   •   ffmpeg: {find_ffmpeg() or 'NO ENCONTRADO'}")
        self._apply_icons()
        self.Centre()
        self.Bind(wx.EVT_CLOSE, self.on_close)

        if not find_ffmpeg():
            wx.CallAfter(
                wx.MessageBox,
                "No se encontró ffmpeg en PATH.\nInstálalo (ej. `winget install Gyan.FFmpeg`) y reinicia la app.",
                "ffmpeg no encontrado",
                wx.ICON_WARNING,
            )

    # ------------------------------------------------------------------ icons
    def _apply_icons(self) -> None:
        assets = Path(__file__).resolve().parent.parent / "assets"
        png, ico = assets / "app.png", assets / "app.ico"
        if png.exists():
            try:
                src = wx.Image(str(png), wx.BITMAP_TYPE_PNG)
            except Exception:
                src = None
            if src is not None and src.IsOk():
                bundle = wx.IconBundle()
                for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
                    icon = wx.Icon(wx.Bitmap(src.Scale(size, size, wx.IMAGE_QUALITY_HIGH)))
                    if icon.IsOk():
                        bundle.AddIcon(icon)
                if bundle.GetIconCount():
                    self.SetIcons(bundle)
                    return
        if ico.exists():
            try:
                self.SetIcon(wx.Icon(str(ico), wx.BITMAP_TYPE_ICO))
            except Exception:
                pass

    # ------------------------------------------------------------------ UI layout
    def _build_ui(self) -> None:
        d = self.FromDIP
        outer = wx.Panel(self)
        outer.SetBackgroundColour(self._pal.bg)
        root = wx.BoxSizer(wx.VERTICAL)

        # --- Top Header Bar
        hdr = wx.Panel(outer)
        hdr.SetBackgroundColour(self._pal.surface)
        hdr_sz = wx.BoxSizer(wx.HORIZONTAL)

        assets_png = Path(__file__).resolve().parent.parent / "assets" / "app.png"
        if assets_png.exists():
            try:
                img = wx.Image(str(assets_png), wx.BITMAP_TYPE_PNG).Scale(d(32), d(32), wx.IMAGE_QUALITY_HIGH)
                hdr_sz.Add(wx.StaticBitmap(hdr, bitmap=wx.Bitmap(img)), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(10))
            except Exception:
                pass

        brand_col = wx.BoxSizer(wx.VERTICAL)
        lbl_app = wx.StaticText(hdr, label="Audiobook Factory")
        make_bold(lbl_app, 2)
        lbl_sub = wx.StaticText(hdr, label="Studio de conversión EPUB / PDF → Audiolibro (Kokoro & Supertonic)")
        lbl_sub.SetForegroundColour(self._pal.muted)
        brand_col.Add(lbl_app, 0)
        brand_col.Add(lbl_sub, 0)
        hdr_sz.Add(brand_col, 1, wx.ALIGN_CENTER_VERTICAL)

        self.lbl_hw = wx.StaticText(
            hdr,
            label=f"  {device_description()} • ffmpeg {'OK' if find_ffmpeg() else 'FALTA'}  ",
        )
        self.lbl_hw.SetForegroundColour(self._pal.success if find_ffmpeg() else self._pal.warning)
        hdr_sz.Add(self.lbl_hw, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(10))

        self.btn_open_book = wx.Button(hdr, label="+ Abrir EPUB / PDF…")
        self.btn_player = wx.Button(hdr, label="▶ Reproductor M4B")
        hdr_sz.Add(self.btn_open_book, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(6))
        hdr_sz.Add(self.btn_player, 0, wx.ALIGN_CENTER_VERTICAL)

        hdr_wrap = wx.BoxSizer(wx.VERTICAL)
        hdr_wrap.Add(hdr_sz, 1, wx.EXPAND | wx.ALL, d(10))
        hdr.SetSizer(hdr_wrap)
        root.Add(hdr, 0, wx.EXPAND)

        # --- Main Notebook Tabs
        self.notebook = wx.Notebook(outer)
        self.tab_studio = self._build_studio_tab(self.notebook)
        self.queue_panel = QueuePanel(
            self.notebook,
            self._pal,
            self.queue_mgr,
            on_open_player=self.open_player,
            on_hybrid_changed=self._on_hybrid_saved,
        )
        self.tab_logs = self._build_logs_tab(self.notebook)

        self.notebook.AddPage(self.tab_studio, "Studio & Editor")
        self.notebook.AddPage(self.queue_panel, "Cola de Producción (0)")
        self.notebook.AddPage(self.tab_logs, "Actividad & Logs")
        root.Add(self.notebook, 1, wx.EXPAND | wx.ALL, d(8))

        # --- Persistent Bottom Live Conversion Bar
        live_bar = wx.Panel(outer)
        live_bar.SetBackgroundColour(self._pal.surface)
        lsz = wx.BoxSizer(wx.HORIZONTAL)

        info_col = wx.BoxSizer(wx.VERTICAL)
        top_line = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_live_title = wx.StaticText(live_bar, label="Sin trabajos activos")
        make_bold(self.lbl_live_title)
        self.lbl_live_state = wx.StaticText(live_bar, label="Listo")
        self.lbl_live_state.SetForegroundColour(self._pal.muted)
        top_line.Add(self.lbl_live_title, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(8))
        top_line.Add(self.lbl_live_state, 0, wx.ALIGN_CENTER_VERTICAL)
        info_col.Add(top_line, 0, wx.BOTTOM, d(2))

        self.lbl_live_detail = wx.StaticText(live_bar, label="Carga un EPUB o PDF (o arrástralo aquí) para comenzar.")
        self.lbl_live_detail.SetForegroundColour(self._pal.muted)
        info_col.Add(self.lbl_live_detail, 0, wx.EXPAND)
        lsz.Add(info_col, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(12))

        self.gauge = wx.Gauge(live_bar, range=1000, size=d(wx.Size(260, 18)))
        lsz.Add(self.gauge, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(10))

        self.btn_live_pause = wx.Button(live_bar, label="⏸ Pausar")
        self.btn_live_cancel = wx.Button(live_bar, label="✕ Cancelar")
        self.btn_live_open = wx.Button(live_bar, label="▶ Abrir audio")
        self.btn_live_pause.Disable()
        self.btn_live_cancel.Disable()
        self.btn_live_open.Disable()
        lsz.Add(self.btn_live_pause, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(6))
        lsz.Add(self.btn_live_cancel, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(6))
        lsz.Add(self.btn_live_open, 0, wx.ALIGN_CENTER_VERTICAL)

        lwrap = wx.BoxSizer(wx.VERTICAL)
        lwrap.Add(lsz, 1, wx.EXPAND | wx.ALL, d(10))
        live_bar.SetSizer(lwrap)
        root.Add(live_bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(8))

        outer.SetSizer(root)

        # Top/Bottom bar events
        self.btn_open_book.Bind(wx.EVT_BUTTON, self._on_open_dialog)
        self.btn_player.Bind(wx.EVT_BUTTON, lambda _e: self.open_player(self._last_output))
        self.btn_live_pause.Bind(wx.EVT_BUTTON, self._on_live_pause_resume)
        self.btn_live_cancel.Bind(wx.EVT_BUTTON, self._on_live_cancel)
        self.btn_live_open.Bind(wx.EVT_BUTTON, lambda _e: self._open_active_result())

        self._set_lang(self.settings.get("lang", "a"), voice=self.settings.get("voice"))
        self._update_convert_label()

    def _build_studio_tab(self, parent: wx.Window) -> wx.Window:
        d = self.FromDIP
        main_split = wx.SplitterWindow(parent, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        main_split.SetMinimumPaneSize(d(260))

        # --- LEFT SIDEBAR (Book metadata + Narration + Queue Actions)
        self.left = wx.ScrolledWindow(main_split, style=wx.VSCROLL)
        self.left.SetScrollRate(0, d(10))
        self.left.SetBackgroundColour(self._pal.bg)
        left_sz = wx.BoxSizer(wx.VERTICAL)

        # Book Info Card
        book_box = wx.StaticBoxSizer(wx.VERTICAL, self.left, "Libro Actual")
        bb = book_box.GetStaticBox()
        bb.SetBackgroundColour(self._pal.surface)

        top_book = wx.BoxSizer(wx.HORIZONTAL)
        self.cover = wx.StaticBitmap(bb, size=d(wx.Size(88, 130)))
        self.cover.SetBitmap(wx.Bitmap(d(wx.Size(88, 130))))
        top_book.Add(self.cover, 0, wx.RIGHT, d(10))

        meta_col = wx.BoxSizer(wx.VERTICAL)
        self.lbl_title = wx.StaticText(bb, label="Ningún libro cargado")
        make_bold(self.lbl_title)
        self.lbl_author = wx.StaticText(bb, label="Arrastra un EPUB o PDF")
        self.lbl_lang = wx.StaticText(bb, label="")
        self.lbl_lang.SetForegroundColour(self._pal.muted)
        self._wrap_labels = [self.lbl_title, self.lbl_author, self.lbl_lang]
        for lbl in self._wrap_labels:
            meta_col.Add(lbl, 0, wx.EXPAND | wx.BOTTOM, d(4))
        self.btn_edit_meta = wx.Button(bb, label="Editar metadatos / portada…")
        self.btn_edit_meta.Disable()
        meta_col.Add(self.btn_edit_meta, 0, wx.EXPAND | wx.TOP, d(4))
        top_book.Add(meta_col, 1, wx.EXPAND)
        book_box.Add(top_book, 0, wx.EXPAND | wx.ALL, d(6))

        book_box.Add(wx.StaticText(bb, label="Archivo origen:"), 0, wx.LEFT | wx.RIGHT, d(6))
        self.epub_picker = wx.FilePickerCtrl(
            bb,
            message="Elige un EPUB o PDF",
            wildcard="Libros (*.epub;*.pdf)|*.epub;*.pdf|EPUB (*.epub)|*.epub|PDF (*.pdf)|*.pdf|Todos|*.*",
            style=wx.FLP_OPEN | wx.FLP_FILE_MUST_EXIST | wx.FLP_USE_TEXTCTRL,
        )
        self.epub_picker.SetInitialDirectory(self.settings.get("last_epub_dir", str(Path.home())))
        book_box.Add(self.epub_picker, 0, wx.EXPAND | wx.ALL, d(6))

        book_box.Add(wx.StaticText(bb, label="Archivo de salida:"), 0, wx.LEFT | wx.RIGHT, d(6))
        self.out_picker = wx.FilePickerCtrl(
            bb,
            message="Guardar audiolibro como",
            wildcard="Audiolibro (*.m4b;*.m4a;*.mp3;*.opus;*.flac)|*.m4b;*.m4a;*.mp3;*.opus;*.flac|Todos|*.*",
            style=wx.FLP_SAVE | wx.FLP_OVERWRITE_PROMPT | wx.FLP_USE_TEXTCTRL,
        )
        book_box.Add(self.out_picker, 0, wx.EXPAND | wx.ALL, d(6))

        self.btn_save_project = wx.Button(bb, label="Guardar ediciones (.audiobook.json)")
        self.btn_save_project.Disable()
        book_box.Add(self.btn_save_project, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(6))
        left_sz.Add(book_box, 0, wx.EXPAND | wx.BOTTOM, d(8))

        # Narration Card
        nar_box = wx.StaticBoxSizer(wx.VERTICAL, self.left, "Narración & Motor")
        nb = nar_box.GetStaticBox()
        nb.SetBackgroundColour(self._pal.surface)

        g = wx.FlexGridSizer(0, 2, d(6), d(6))
        g.AddGrowableCol(1, 1)

        g.Add(wx.StaticText(nb, label="Motor:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.engine_choice = wx.Choice(nb, choices=[ENGINE_LABELS[e] for e in ENGINE_IDS])
        saved_engine = self.settings.get("engine", "kokoro")
        self.engine_choice.SetSelection(ENGINE_IDS.index(saved_engine) if saved_engine in ENGINE_IDS else 0)
        g.Add(self.engine_choice, 1, wx.EXPAND)

        g.Add(wx.StaticText(nb, label="Idioma:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.lang_choice = wx.Choice(nb, choices=list(LANGUAGES.values()))
        self._lang_codes = list(LANGUAGES.keys())
        g.Add(self.lang_choice, 1, wx.EXPAND)

        g.Add(wx.StaticText(nb, label="Voz:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.voice_choice = wx.Choice(nb, choices=[])
        g.Add(self.voice_choice, 1, wx.EXPAND)

        g.Add(wx.StaticText(nb, label="Velocidad:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.speed = wx.SpinCtrlDouble(nb, min=0.6, max=1.5, inc=0.05, initial=float(self.settings.get("speed", 1.0)))
        self.speed.SetDigits(2)
        g.Add(self.speed, 0)

        g.Add(wx.StaticText(nb, label="Cómputo:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.device_choice = wx.Choice(nb, choices=["Auto", "CUDA (GPU)", "CPU"])
        self.device_choice.SetSelection({"auto": 0, "cuda": 1, "cpu": 2}.get(self.settings.get("device", "auto"), 0))
        g.Add(self.device_choice, 1, wx.EXPAND)

        g.Add(wx.StaticText(nb, label="Bitrate:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.bitrate_choice = wx.Choice(nb, choices=["48k", "64k", "96k", "128k"])
        self.bitrate_choice.SetStringSelection(self.settings.get("bitrate", "64k"))
        g.Add(self.bitrate_choice, 1, wx.EXPAND)

        g.Add(wx.StaticText(nb, label="Formato:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self._format_ids = list(AUDIO_FORMATS)
        self.format_choice = wx.Choice(
            nb,
            choices=[f"{AUDIO_FORMATS[f][0]} ({AUDIO_FORMATS[f][1]})" for f in self._format_ids],
        )
        saved_fmt = self.settings.get("format", "m4b")
        self.format_choice.SetSelection(self._format_ids.index(saved_fmt) if saved_fmt in self._format_ids else 0)
        g.Add(self.format_choice, 1, wx.EXPAND)

        for c in (self.engine_choice, self.lang_choice, self.voice_choice,
                  self.device_choice, self.bitrate_choice, self.format_choice):
            c.SetMinSize(d(wx.Size(120, -1)))
        nar_box.Add(g, 0, wx.EXPAND | wx.ALL, d(6))

        self.chk_credits = wx.CheckBox(nb, label="Leer créditos iniciales")
        self.chk_credits.SetValue(self.settings.get("read_credits", True))
        self.chk_titles = wx.CheckBox(nb, label="Anunciar títulos de capítulo")
        self.chk_titles.SetValue(self.settings.get("read_titles", True))
        self.chk_keep = wx.CheckBox(nb, label="Conservar temporales (reanudar)")
        self.chk_keep.SetValue(self.settings.get("keep_work_files", False))
        self.chk_split = wx.CheckBox(nb, label="Un archivo por capítulo (carpeta)")
        self.chk_split.SetValue(self.settings.get("chapter_files", False))
        for chk in (self.chk_credits, self.chk_titles, self.chk_keep, self.chk_split):
            nar_box.Add(chk, 0, wx.LEFT | wx.RIGHT | wx.TOP, d(5))

        prow = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_preview = wx.Button(nb, label="▶ Probar voz")
        self.btn_stop_preview = wx.Button(nb, label="■", size=d(wx.Size(36, -1)))
        self.btn_advanced = wx.Button(nb, label="Pausas / Avanzado…")
        prow.Add(self.btn_preview, 1, wx.RIGHT, d(4))
        prow.Add(self.btn_stop_preview, 0, wx.RIGHT, d(4))
        prow.Add(self.btn_advanced, 0)
        nar_box.Add(prow, 0, wx.EXPAND | wx.ALL, d(6))

        left_sz.Add(nar_box, 0, wx.EXPAND | wx.BOTTOM, d(8))

        # Action Buttons
        self.btn_enqueue = wx.Button(self.left, label="+ Enviar a la Cola de Producción")
        make_bold(self.btn_enqueue)
        self.btn_enqueue.SetBackgroundColour(self._pal.primary)
        self.btn_enqueue.SetForegroundColour(self._pal.primary_text)
        self.btn_convert = wx.Button(self.left, label="Convertir ahora (prioritario)")
        left_sz.Add(self.btn_enqueue, 0, wx.EXPAND | wx.BOTTOM, d(6))
        left_sz.Add(self.btn_convert, 0, wx.EXPAND)

        self.left.SetSizer(left_sz)

        # --- RIGHT SPLIT: Center (Chapter List) + Right (Section Visualizer/Editor)
        right_split = wx.SplitterWindow(main_split, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        right_split.SetMinimumPaneSize(d(320))

        ch_panel = wx.Panel(right_split)
        ch_panel.SetBackgroundColour(self._pal.surface)
        ch_sz = wx.BoxSizer(wx.VERTICAL)

        ch_hdr = wx.BoxSizer(wx.HORIZONTAL)
        lbl_ch = wx.StaticText(ch_panel, label="Secciones del Libro")
        make_bold(lbl_ch)
        ch_hdr.Add(lbl_ch, 1, wx.ALIGN_CENTER_VERTICAL)
        self.txt_filter = wx.SearchCtrl(ch_panel, style=wx.TE_PROCESS_ENTER)
        self.txt_filter.SetDescriptiveText("Filtrar secciones…")
        self.txt_filter.ShowCancelButton(True)
        self.txt_filter.SetMinSize(d(wx.Size(180, -1)))
        ch_hdr.Add(self.txt_filter, 0)
        ch_sz.Add(ch_hdr, 0, wx.EXPAND | wx.ALL, d(8))

        self.list = wx.ListCtrl(ch_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.EnableCheckBoxes(True)
        cols = [
            ("#", 42, wx.LIST_FORMAT_RIGHT),
            ("Título", 260, wx.LIST_FORMAT_LEFT),
            ("Tipo", 95, wx.LIST_FORMAT_LEFT),
            ("Pal.", 62, wx.LIST_FORMAT_RIGHT),
            ("Est.", 54, wx.LIST_FORMAT_RIGHT),
            ("Estado", 105, wx.LIST_FORMAT_LEFT),
        ]
        for i, (name, w, fmt) in enumerate(cols):
            self.list.InsertColumn(i, name, width=d(w), format=fmt)
        ch_sz.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, d(8))

        brow = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_all = wx.Button(ch_panel, label="Todo")
        self.btn_none = wx.Button(ch_panel, label="Nada")
        self.btn_smart = wx.Button(ch_panel, label="Inteligente")
        self.btn_ch_up = wx.Button(ch_panel, label="↑", size=d(wx.Size(32, -1)))
        self.btn_ch_down = wx.Button(ch_panel, label="↓", size=d(wx.Size(32, -1)))
        self.btn_ch_merge = wx.Button(ch_panel, label="Unir sig.")
        for b in (self.btn_all, self.btn_none, self.btn_smart):
            brow.Add(b, 0, wx.RIGHT, d(4))
        brow.AddStretchSpacer()
        for b in (self.btn_ch_up, self.btn_ch_down, self.btn_ch_merge):
            brow.Add(b, 0, wx.LEFT, d(4))
        ch_sz.Add(brow, 0, wx.EXPAND | wx.ALL, d(8))

        self.lbl_selection = wx.StaticText(ch_panel, label="Carga un libro para inspeccionar sus secciones.")
        self.lbl_selection.SetForegroundColour(self._pal.muted)
        ch_sz.Add(self.lbl_selection, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, d(8))
        ch_panel.SetSizer(ch_sz)

        # Inspector / Editor Panel
        self.inspector = ChapterInspectorPanel(
            right_split,
            self._pal,
            on_changed=self._on_inspector_changed,
            on_restore=self._on_inspector_restore,
            on_split=self._on_inspector_split,
            on_preview_text=self._preview_custom_text,
            get_tts_params=self._current_tts_params,
        )

        right_split.SplitVertically(ch_panel, self.inspector, d(470))
        right_split.SetSashGravity(0.45)

        main_split.SplitVertically(self.left, right_split, d(LEFT_COL_DIP))
        main_split.SetSashGravity(0.0)

        # Studio events
        self.epub_picker.Bind(wx.EVT_FILEPICKER_CHANGED, self.on_epub_chosen)
        self.btn_edit_meta.Bind(wx.EVT_BUTTON, self._on_edit_metadata)
        self.btn_save_project.Bind(wx.EVT_BUTTON, self._on_save_project)
        self.lang_choice.Bind(wx.EVT_CHOICE, self.on_lang_changed)
        self.engine_choice.Bind(wx.EVT_CHOICE, self.on_engine_changed)
        self.format_choice.Bind(wx.EVT_CHOICE, self.on_format_changed)
        self.chk_split.Bind(wx.EVT_CHECKBOX, lambda _e: self._update_convert_label())
        self.chk_titles.Bind(wx.EVT_CHECKBOX, lambda _e: self.inspector.refresh_tts_view())
        self.speed.Bind(wx.EVT_SPINCTRLDOUBLE, self._on_speed_changed)
        self.btn_preview.Bind(wx.EVT_BUTTON, self.on_preview)
        self.btn_stop_preview.Bind(wx.EVT_BUTTON, lambda _e: self._stop_preview())
        self.btn_advanced.Bind(wx.EVT_BUTTON, self._on_advanced)
        self.btn_enqueue.Bind(wx.EVT_BUTTON, lambda _e: self._enqueue_current(prioritize=False))
        self.btn_convert.Bind(wx.EVT_BUTTON, lambda _e: self._enqueue_current(prioritize=True))

        self.txt_filter.Bind(wx.EVT_TEXT, lambda _e: self._populate_chapter_list())
        self.txt_filter.Bind(wx.EVT_SEARCHCTRL_CANCEL_BTN, lambda _e: self.txt_filter.SetValue(""))
        self.btn_all.Bind(wx.EVT_BUTTON, lambda _e: self._set_all(True))
        self.btn_none.Bind(wx.EVT_BUTTON, lambda _e: self._set_all(False))
        self.btn_smart.Bind(wx.EVT_BUTTON, lambda _e: self._smart_select())
        self.btn_ch_up.Bind(wx.EVT_BUTTON, lambda _e: self._move_selected_chapter(-1))
        self.btn_ch_down.Bind(wx.EVT_BUTTON, lambda _e: self._move_selected_chapter(1))
        self.btn_ch_merge.Bind(wx.EVT_BUTTON, self._merge_selected_chapter)
        self.list.Bind(wx.EVT_LIST_ITEM_CHECKED, self._on_item_checked)
        self.list.Bind(wx.EVT_LIST_ITEM_UNCHECKED, self._on_item_checked)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_chapter_selected)
        self.left.Bind(wx.EVT_SIZE, self._on_left_size)

        return main_split

    def _build_logs_tab(self, parent: wx.Window) -> wx.Window:
        d = self.FromDIP
        panel = wx.Panel(parent)
        panel.SetBackgroundColour(self._pal.bg)
        sz = wx.BoxSizer(wx.VERTICAL)

        bar = wx.BoxSizer(wx.HORIZONTAL)
        lbl = wx.StaticText(panel, label="Registro de actividad (TTS, caché, encoding y cola)")
        make_bold(lbl)
        btn_copy = wx.Button(panel, label="Copiar logs")
        btn_clear = wx.Button(panel, label="Limpiar")
        bar.Add(lbl, 1, wx.ALIGN_CENTER_VERTICAL)
        bar.Add(btn_copy, 0, wx.RIGHT, d(6))
        bar.Add(btn_clear, 0)
        sz.Add(bar, 0, wx.EXPAND | wx.ALL, d(8))

        self.log = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.log.SetFont(wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE)))
        sz.Add(self.log, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(8))
        panel.SetSizer(sz)

        btn_copy.Bind(wx.EVT_BUTTON, self._on_copy_logs)
        btn_clear.Bind(wx.EVT_BUTTON, lambda _e: self.log.Clear())
        return panel

    # ------------------------------------------------------------------ helpers
    def _log(self, msg: str) -> None:
        self.log.AppendText(msg + "\n")

    def _on_copy_logs(self, _evt) -> None:
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(self.log.GetValue()))
            finally:
                wx.TheClipboard.Close()

    def _on_hybrid_saved(self, val: bool) -> None:
        self.settings["hybrid_parallel"] = val
        save_settings(self.settings)

    def _on_left_size(self, evt) -> None:
        evt.Skip()
        wx.CallAfter(self._rewrap)

    def _rewrap(self) -> None:
        if not self:
            return
        width = max(120, self.left.GetClientSize().width - self.FromDIP(140))
        for lbl in self._wrap_labels:
            text = getattr(lbl, "_full_text", lbl.GetLabel())
            lbl._full_text = text
            lbl.SetLabel(_wrap_text(lbl, text, width))
            lbl.SetMinSize(wx.Size(width, -1))
        self.left.Layout()
        self.left.FitInside()

    def _set_wrapped(self, label: wx.StaticText, text: str) -> None:
        label._full_text = text
        label.SetLabel(text)
        self._rewrap()

    def _set_lang(self, code: str, voice: Optional[str] = None) -> None:
        if code not in self._lang_codes:
            code = "a"
        self.lang_choice.SetSelection(self._lang_codes.index(code))
        self._fill_voices(code, voice)

    def _fill_voices(self, code: str, voice: Optional[str] = None) -> None:
        self.voice_choice.Clear()
        entries = voices_for(self.current_engine(), code)
        self._voice_ids = [v for v, _ in entries]
        for v, label in entries:
            self.voice_choice.Append(f"{label}  [{v}]")
        sel = self._voice_ids.index(voice) if voice in self._voice_ids else 0
        self.voice_choice.SetSelection(sel)

    def current_lang(self) -> str:
        return self._lang_codes[max(0, self.lang_choice.GetSelection())]

    def current_engine(self) -> str:
        i = self.engine_choice.GetSelection()
        return ENGINE_IDS[i] if 0 <= i < len(ENGINE_IDS) else "kokoro"

    def current_voice(self) -> str:
        i = self.voice_choice.GetSelection()
        if 0 <= i < len(self._voice_ids):
            return self._voice_ids[i]
        return default_voice_for(self.current_engine(), self.current_lang())

    def current_device(self) -> str:
        return ["auto", "cuda", "cpu"][max(0, self.device_choice.GetSelection())]

    def current_format(self) -> str:
        i = self.format_choice.GetSelection()
        return self._format_ids[i] if 0 <= i < len(self._format_ids) else "m4b"

    def _current_tts_params(self) -> dict:
        return {
            "lang": self.current_lang(),
            "max_chars": self._adv_options["max_chars"],
            "read_titles": self.chk_titles.GetValue(),
            "speed": float(self.speed.GetValue()),
        }

    def on_format_changed(self, _evt) -> None:
        fmt = self.current_format()
        self.settings["format"] = fmt
        save_settings(self.settings)
        path = self.out_picker.GetPath()
        if path:
            self.out_picker.SetPath(str(Path(path).with_suffix(format_extension(fmt))))
        self._update_convert_label()

    def _update_convert_label(self) -> None:
        ext = format_extension(self.current_format()).lstrip(".").upper()
        self.btn_convert.SetLabel(
            f"Exportar capítulos ({ext}) ahora" if self.chk_split.GetValue() else f"Convertir a {ext} ahora"
        )

    def on_lang_changed(self, _evt) -> None:
        self._fill_voices(self.current_lang(), self.settings.get(f"voice_{self.current_lang()}"))
        self.inspector.refresh_tts_view()

    def on_engine_changed(self, _evt) -> None:
        self.settings["engine"] = self.current_engine()
        save_settings(self.settings)
        self._fill_voices(self.current_lang(), self.settings.get(f"voice_{self.current_lang()}"))

    def _on_speed_changed(self, _evt) -> None:
        self._update_selection_label()
        self.inspector.refresh_tts_view()

    def _get_preview_engine(self) -> object:
        name = self.current_engine()
        dev = self.current_device() if name == "kokoro" else "cpu"
        if self._preview_engine is None or self._preview_engine_key != (name, dev):
            if self._preview_engine is not None:
                self._preview_engine.release()  # type: ignore[attr-defined]
            self._preview_engine = make_engine(name, dev)
            self._preview_engine_key = (name, dev)
        return self._preview_engine

    # ------------------------------------------------------------------ book loading
    def _on_open_dialog(self, _evt) -> None:
        with wx.FileDialog(
            self,
            "Abrir libro EPUB o PDF",
            defaultDir=self.settings.get("last_epub_dir", str(Path.home())),
            wildcard="Libros (*.epub;*.pdf)|*.epub;*.pdf|EPUB (*.epub)|*.epub|PDF (*.pdf)|*.pdf",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            paths = dlg.GetPaths()
            if paths:
                self._on_files_dropped(paths)

    def _on_files_dropped(self, paths: list[str]) -> None:
        first = paths[0]
        self._setting_picker = True
        try:
            self.epub_picker.SetPath(first)
        finally:
            self._setting_picker = False
        self.settings["last_epub_dir"] = str(Path(first).parent)
        self._load_path(first)

        if len(paths) > 1:
            epubs = [p for p in paths[1:] if Path(p).suffix.lower() == ".epub"]
            if epubs and wx.MessageBox(
                f"Además de '{Path(first).name}', soltaste {len(epubs)} EPUB(s) más.\n"
                "¿Quieres añadirlos automáticamente a la Cola de Producción con la configuración actual?",
                "Encolar múltiples libros",
                wx.YES_NO | wx.ICON_QUESTION,
            ) == wx.YES:
                threading.Thread(target=self._batch_enqueue_worker, args=(epubs,), daemon=True).start()

    def _batch_enqueue_worker(self, paths: list[str]) -> None:
        for p in paths:
            try:
                b = load_book(p)
                chs = [c for c in b.chapters if c.include_default] or list(b.chapters)
                iso_guess = detect_language_from_text(b.chapters) if not b.kokoro_lang else None
                lang = b.kokoro_lang or (iso_to_kokoro(iso_guess) if iso_guess else None) or self.current_lang()
                fmt = self.current_format()
                safe = "".join(c for c in b.title if c not in '<>:"/\\|?*').strip() or Path(p).stem
                out_dir = self.settings.get("last_out_dir") or str(Path(p).parent)
                out = str(Path(out_dir) / f"{safe}{format_extension(fmt)}")
                opts = self._build_options(out, lang=lang, voice=default_voice_for(self.current_engine(), lang))
                wx.CallAfter(self.queue_mgr.enqueue, b, chs, opts, False)
            except Exception as e:
                wx.CallAfter(self._log, f"Error cargando lote '{Path(p).name}': {e}")

    def on_epub_chosen(self, _evt) -> None:
        if self._setting_picker:
            return
        path = self.epub_picker.GetPath()
        if not path or not os.path.exists(path):
            return
        self.settings["last_epub_dir"] = str(Path(path).parent)
        self._load_path(path)

    def _load_path(self, path: str) -> None:
        if Path(path).suffix.lower() == ".pdf":
            self._start_pdf_import(path)
        else:
            self._start_epub_load(path)

    def _start_epub_load(self, path: str) -> None:
        self._loading_book = True
        self.lbl_selection.SetLabel("Analizando EPUB…")
        self.btn_open_book.Disable()

        def work():
            try:
                book = load_book(path)
                iso_guess = detect_language_from_text(book.chapters) if not book.kokoro_lang else None
                wx.CallAfter(self._book_loaded, book, iso_guess)
            except Exception as e:
                wx.CallAfter(self._load_failed, e)

        threading.Thread(target=work, daemon=True).start()

    def _start_pdf_import(self, pdf: str) -> None:
        self._loading_book = True
        self.lbl_selection.SetLabel("Convirtiendo PDF a EPUB…")
        self.btn_open_book.Disable()
        self._log(f"PDF → EPUB: {pdf}")
        _log_line(f"pdf import start: {pdf}")

        def work():
            try:
                from ..pdf2epub import convert_pdf
                out = convert_pdf(pdf, on_log=lambda m: wx.CallAfter(self._log, m))
                wx.CallAfter(self._pdf_imported, out, None)
            except Exception as e:
                wx.CallAfter(self._pdf_imported, "", e)

        threading.Thread(target=work, daemon=True).start()

    def _pdf_imported(self, out: str, err: Optional[Exception]) -> None:
        _log_line(f"pdf import done: out={out!r} err={err!r}")
        if err is not None:
            self._loading_book = False
            self.btn_open_book.Enable()
            self.lbl_selection.SetLabel("No se pudo convertir este PDF.")
            if isinstance(err, ScannedPdfError):
                msg = (
                    "Este PDF no tiene capa de texto (parece escaneado como imágenes).\n\n"
                    "Necesita pasar por OCR (ej. OCRmyPDF o Acrobat) antes de convertirse en audiolibro."
                )
            else:
                msg = f"No se pudo convertir el PDF:\n{type(err).__name__}: {err}"
            self._log("PDF → EPUB falló: " + str(err))
            wx.MessageBox(msg, "PDF → EPUB", wx.ICON_WARNING)
            return

        self._log(f"EPUB generado desde PDF: {out}")
        self._setting_picker = True
        try:
            self.epub_picker.SetPath(out)
        finally:
            self._setting_picker = False
        self._start_epub_load(out)

    def _load_failed(self, e: Exception) -> None:
        self._loading_book = False
        self.btn_open_book.Enable()
        self.lbl_selection.SetLabel("Error al leer el libro.")
        self._log("ERROR: " + "".join(traceback.format_exception_only(type(e), e)).strip())
        wx.MessageBox(f"No se pudo leer este EPUB:\n{e}", "Error", wx.ICON_ERROR)

    def _book_loaded(self, book: Book, iso_guess: Optional[str]) -> None:
        self._loading_book = False
        self.btn_open_book.Enable()
        self.book = book
        self._snapshots = snapshot_book(book)
        self._chapter_statuses.clear()
        self._current_book_job_id = None

        saved = load_project_edits(book)
        if saved is not None:
            self._checked_indices = set(saved["selected_indices"])
            self._log(f"Ediciones previas cargadas desde proyecto sidecar para '{book.title}'.")
        else:
            self._checked_indices = {ch.index for ch in book.chapters if ch.include_default}

        self._refresh_book_header(iso_guess)
        self.btn_edit_meta.Enable()
        self.btn_save_project.Enable()

        safe = "".join(c for c in book.title if c not in '<>:"/\\|?*').strip() or "audiobook"
        out_dir = self.settings.get("last_out_dir") or str(Path(book.path).parent)
        ext = format_extension(self.current_format())
        self.out_picker.SetPath(str(Path(out_dir) / f"{safe}{ext}"))

        self._populate_chapter_list(select_first_checked=True)
        skipped = [c.title for c in book.chapters if c.index not in self._checked_indices]
        self._log(
            f"Cargado '{book.title}': {len(book.chapters)} secciones, "
            f"{sum(c.words for c in book.chapters):,} palabras. Omitidas por defecto: {len(skipped)}."
        )
        self.Layout()
        self._rewrap()

    def _refresh_book_header(self, iso_guess: Optional[str] = None) -> None:
        if not self.book:
            return
        book = self.book
        self._set_wrapped(self.lbl_title, book.title + (f": {book.subtitle}" if book.subtitle else ""))
        self._set_wrapped(self.lbl_author, book.author or "Autor desconocido")

        lang = book.kokoro_lang
        note = f"Idioma: {book.language_iso or '?'}"
        if not lang and iso_guess:
            lang = iso_to_kokoro(iso_guess)
            note += f" (detectado: {iso_guess})"
        if lang:
            self._set_lang(lang, voice=self.settings.get(f"voice_{lang}"))
            note += f" → {LANGUAGES[lang]}"
        else:
            note += " — selecciona idioma manualmente"
        self._set_wrapped(self.lbl_lang, note)

        tw, th = self.FromDIP(wx.Size(88, 130)).Get()
        bmp = wx.Bitmap(tw, th)
        if book.cover_bytes:
            try:
                img = wx.Image(io.BytesIO(book.cover_bytes))
                if img.IsOk():
                    sc = min(tw / img.GetWidth(), th / img.GetHeight())
                    img = img.Scale(
                        max(1, int(img.GetWidth() * sc)),
                        max(1, int(img.GetHeight() * sc)),
                        wx.IMAGE_QUALITY_HIGH,
                    )
                    bmp = wx.Bitmap(img)
            except Exception:
                pass
        self.cover.SetBitmap(bmp)

    # ------------------------------------------------------------------ chapters & inspector
    def _populate_chapter_list(self, select_first_checked: bool = False, keep_chapter: Optional[Chapter] = None) -> None:
        if not self.book:
            return
        query = self.txt_filter.GetValue().strip().lower()
        target_ch = keep_chapter
        if target_ch is None and not select_first_checked:
            sel_row = self.list.GetFirstSelected()
            if 0 <= sel_row < len(self._visible_chapters):
                target_ch = self._visible_chapters[sel_row]

        self._visible_chapters = [
            ch for ch in self.book.chapters
            if not query or query in ch.title.lower() or query in ch.kind.lower()
        ]

        self._suppress_check_event = True
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            row_to_select = -1
            for row, ch in enumerate(self._visible_chapters):
                edited = is_chapter_edited(ch, self._snapshots)
                title_disp = f"✎ {ch.title}" if edited else ch.title
                idx = self.list.InsertItem(row, str(ch.index))
                self.list.SetItem(idx, 1, title_disp)
                self.list.SetItem(idx, 2, ch.kind)
                self.list.SetItem(idx, 3, f"{ch.words:,}")
                self.list.SetItem(idx, 4, fmt_minutes(ch.words / WORDS_PER_MIN))
                self.list.SetItem(idx, 5, self._chapter_statuses.get(ch.index, ""))
                checked = ch.index in self._checked_indices
                self.list.CheckItem(idx, checked)

                if edited:
                    self.list.SetItemBackgroundColour(idx, self._pal.edited_bg)
                elif not checked:
                    self.list.SetItemTextColour(idx, self._pal.muted)

                if target_ch is not None and ch is target_ch:
                    row_to_select = row
                elif select_first_checked and row_to_select < 0 and checked:
                    row_to_select = row

            if row_to_select < 0 and self._visible_chapters:
                row_to_select = 0
            if row_to_select >= 0:
                self.list.Select(row_to_select)
                self.list.EnsureVisible(row_to_select)
        finally:
            self.list.Thaw()
            self._suppress_check_event = False

        self._update_selection_label()
        self._sync_inspector_to_selection()

    def _selected_chapter(self) -> Optional[Chapter]:
        row = self.list.GetFirstSelected()
        if 0 <= row < len(self._visible_chapters):
            return self._visible_chapters[row]
        return None

    def _sync_inspector_to_selection(self) -> None:
        ch = self._selected_chapter()
        if ch is None:
            self.inspector.set_chapter(None, False)
            return
        self.inspector.set_chapter(ch, edited=is_chapter_edited(ch, self._snapshots))

    def _on_chapter_selected(self, _evt) -> None:
        self._sync_inspector_to_selection()

    def _on_item_checked(self, evt) -> None:
        if self._suppress_check_event or not self.book:
            return
        row = evt.GetIndex()
        if 0 <= row < len(self._visible_chapters):
            ch = self._visible_chapters[row]
            if self.list.IsItemChecked(row):
                self._checked_indices.add(ch.index)
                self.list.SetItemTextColour(row, self._pal.text)
            else:
                self._checked_indices.discard(ch.index)
                self.list.SetItemTextColour(row, self._pal.muted)
        self._update_selection_label()

    def _selected_chapters_ordered(self) -> list[Chapter]:
        if not self.book:
            return []
        return [ch for ch in self.book.chapters if ch.index in self._checked_indices]

    def _set_all(self, value: bool) -> None:
        if not self.book:
            return
        for ch in self._visible_chapters:
            if value:
                self._checked_indices.add(ch.index)
            else:
                self._checked_indices.discard(ch.index)
        self._populate_chapter_list(keep_chapter=self._selected_chapter())

    def _smart_select(self) -> None:
        if not self.book:
            return
        self._checked_indices = {ch.index for ch in self.book.chapters if ch.include_default}
        self._populate_chapter_list(keep_chapter=self._selected_chapter())

    def _update_selection_label(self) -> None:
        chs = self._selected_chapters_ordered()
        words = sum(c.words for c in chs)
        est = words / (WORDS_PER_MIN * float(self.speed.GetValue() or 1.0))
        self.lbl_selection.SetLabel(
            f"{len(chs)} secciones seleccionadas — {words:,} palabras — ~{fmt_minutes(est)} de audio"
        )

    def _on_inspector_changed(self, ch: Chapter) -> None:
        edited = is_chapter_edited(ch, self._snapshots)
        self.inspector.set_edited_badge(edited)
        for row, item in enumerate(self._visible_chapters):
            if item is ch:
                self.list.SetItem(row, 1, f"✎ {ch.title}" if edited else ch.title)
                self.list.SetItem(row, 2, ch.kind)
                self.list.SetItem(row, 3, f"{ch.words:,}")
                self.list.SetItem(row, 4, fmt_minutes(ch.words / WORDS_PER_MIN))
                if edited:
                    self.list.SetItemBackgroundColour(row, self._pal.edited_bg)
                break
        self._update_selection_label()

    def _on_inspector_restore(self, ch: Chapter) -> None:
        snap = self._snapshots.get(ch.index)
        if not snap:
            return
        ch.title = snap.title
        ch.kind = snap.kind
        ch.blocks = [Block(kind=b.kind, text=b.text, level=b.level) for b in snap.blocks]
        self._populate_chapter_list(keep_chapter=ch)
        self._log(f"Sección #{ch.index} restaurada al texto original.")

    def _on_inspector_split(self, ch: Chapter, before_blocks: list[Block], after_blocks: list[Block]) -> None:
        if not self.book:
            return
        pos = next((i for i, c in enumerate(self.book.chapters) if c is ch), -1)
        if pos < 0:
            return
        next_idx = max((c.index for c in self.book.chapters), default=0) + 1
        ch.blocks = before_blocks
        new_ch = Chapter(
            index=next_idx,
            href=ch.href,
            title=f"{ch.title} (Parte 2)",
            kind=ch.kind,
            blocks=after_blocks,
            include_default=True,
        )
        self.book.chapters.insert(pos + 1, new_ch)
        self._checked_indices.add(new_ch.index)
        self._populate_chapter_list(keep_chapter=ch)
        self._log(f"Sección #{ch.index} dividida; nueva sección #{new_ch.index} '{new_ch.title}'.")

    def _move_selected_chapter(self, delta: int) -> None:
        if not self.book:
            return
        ch = self._selected_chapter()
        if not ch:
            return
        pos = next((i for i, c in enumerate(self.book.chapters) if c is ch), -1)
        new_pos = pos + delta
        if pos < 0 or not (0 <= new_pos < len(self.book.chapters)):
            return
        self.book.chapters[pos], self.book.chapters[new_pos] = (
            self.book.chapters[new_pos],
            self.book.chapters[pos],
        )
        self._populate_chapter_list(keep_chapter=ch)

    def _merge_selected_chapter(self, _evt) -> None:
        if not self.book:
            return
        ch = self._selected_chapter()
        if not ch:
            return
        pos = next((i for i, c in enumerate(self.book.chapters) if c is ch), -1)
        if pos < 0 or pos + 1 >= len(self.book.chapters):
            wx.MessageBox("Selecciona una sección que tenga otra sección debajo para unir.", "Unir", wx.ICON_INFORMATION)
            return
        nxt = self.book.chapters[pos + 1]
        if wx.MessageBox(
            f"¿Unir '{nxt.title}' al final de '{ch.title}'?",
            "Unir secciones",
            wx.YES_NO | wx.ICON_QUESTION,
        ) != wx.YES:
            return
        ch.blocks.append(Block(kind="break"))
        if nxt.title:
            ch.blocks.append(Block(kind="heading", text=nxt.title, level=2))
        ch.blocks.extend(nxt.blocks)
        self._checked_indices.discard(nxt.index)
        self.book.chapters.pop(pos + 1)
        self._populate_chapter_list(keep_chapter=ch)
        self._log(f"Sección '{nxt.title}' unida dentro de '{ch.title}'.")

    def _on_edit_metadata(self, _evt) -> None:
        if not self.book:
            return
        dlg = MetadataDialog(self, self.book)
        if dlg.ShowModal() == wx.ID_OK:
            dlg.apply_to_book()
            self._refresh_book_header()
            self._log(f"Metadatos actualizados: {self.book.title} — {self.book.author}")
        dlg.Destroy()

    def _on_save_project(self, _evt) -> None:
        if not self.book:
            return
        out = save_project_edits(self.book, self._checked_indices, self._snapshots)
        self._log(f"Proyecto guardado en: {out}")
        self.SetStatusText(f"Ediciones guardadas en {out.name}")

    def _on_advanced(self, _evt) -> None:
        dlg = AdvancedOptionsDialog(self, self._adv_options)
        if dlg.ShowModal() == wx.ID_OK:
            self._adv_options = dlg.get_values()
            self.settings.update(self._adv_options)
            save_settings(self.settings)
            self.inspector.refresh_tts_view()
        dlg.Destroy()

    # ------------------------------------------------------------------ preview
    def on_preview(self, _evt) -> None:
        if not self.book:
            wx.MessageBox("Carga un libro primero.", "Probar voz", wx.ICON_INFORMATION)
            return
        ch = self._selected_chapter()
        chs = self._selected_chapters_ordered()
        target = ch or (chs[0] if chs else None)
        text = " ".join(b.text for b in target.blocks if b.kind == "para")[:600] if target else ""
        if not text:
            text = "Esta es una muestra de la voz seleccionada para comprobar su tono, ritmo y claridad."
        self._preview_custom_text(text)

    def _preview_custom_text(self, text: str) -> None:
        lang = self.current_lang()
        voice = self.current_voice()
        speed = float(self.speed.GetValue())
        self._stop_preview()
        self.btn_preview.Disable()
        self.SetStatusText(f"Sintetizando muestra con {voice}…")

        def work():
            try:
                audio = preview_audio(
                    lang,
                    voice,
                    text,
                    speed,
                    self._get_preview_engine(),
                    max_chars=int(self._adv_options["max_chars"]),
                )
                tmp = Path(tempfile.gettempdir()) / "epub2audiobook_preview.wav"
                write_wav(tmp, audio)
                wx.CallAfter(self._play_preview, tmp)
            except Exception as e:
                wx.CallAfter(self._preview_failed, e)

        threading.Thread(target=work, daemon=True).start()

    def _play_preview(self, path: Path) -> None:
        self.btn_preview.Enable()
        self.SetStatusText("Reproduciendo muestra de voz…")
        self._preview_sound = wx.adv.Sound(str(path))
        if self._preview_sound.IsOk():
            self._preview_sound.Play(wx.adv.SOUND_ASYNC)
        else:
            self.SetStatusText("No se pudo reproducir la muestra.")

    def _stop_preview(self) -> None:
        try:
            wx.adv.Sound.Stop()
        except Exception:
            pass

    def _preview_failed(self, e: Exception) -> None:
        self.btn_preview.Enable()
        self.SetStatusText("Error al generar muestra de voz.")
        self._log("ERROR preview: " + "".join(traceback.format_exception(type(e), e, e.__traceback__)).strip())

    # ------------------------------------------------------------------ player
    def open_player(self, path: Optional[str] = None) -> None:
        if self._player:
            try:
                self._player.Raise()
                if path:
                    self._player.load(path)
                return
            except RuntimeError:
                self._player = None
        self._player = PlayerFrame(self, path if path and os.path.exists(path) else None)
        self._player.Bind(wx.EVT_WINDOW_DESTROY, self._player_closed)
        self._player.Show()
        if not path:
            wx.CallAfter(self._player._on_open, None)

    def _player_closed(self, evt) -> None:
        if evt.GetEventObject() is self._player:
            self._player = None
        evt.Skip()

    # ------------------------------------------------------------------ queue / convert
    def _build_options(self, out: str, lang: Optional[str] = None, voice: Optional[str] = None) -> ConvertOptions:
        fmt = self.current_format()
        split = self.chk_split.GetValue()
        lang_code = lang or self.current_lang()
        voice_id = voice or self.current_voice()
        return ConvertOptions(
            output_path=out,
            voice=voice_id,
            lang=lang_code,
            engine=self.current_engine(),
            format=fmt,
            chapter_files=split,
            speed=float(self.speed.GetValue()),
            device=self.current_device(),
            bitrate=self.bitrate_choice.GetStringSelection() or "64k",
            max_chars=int(self._adv_options["max_chars"]),
            read_titles=self.chk_titles.GetValue(),
            read_credits=self.chk_credits.GetValue(),
            keep_work_files=self.chk_keep.GetValue(),
            pause_chunk=float(self._adv_options["pause_chunk"]),
            pause_para=float(self._adv_options["pause_para"]),
            pause_heading=float(self._adv_options["pause_heading"]),
            pause_break=float(self._adv_options["pause_break"]),
            pause_chapter_start=float(self._adv_options["pause_chapter_start"]),
            pause_chapter_end=float(self._adv_options["pause_chapter_end"]),
            target_rms_db=float(self._adv_options["target_rms_db"]),
        )

    def _enqueue_current(self, prioritize: bool = False) -> None:
        if not self.book:
            wx.MessageBox("Carga un EPUB o PDF primero.", "Cola", wx.ICON_INFORMATION)
            return
        chapters = self._selected_chapters_ordered()
        if not chapters:
            wx.MessageBox("No hay secciones seleccionadas.", "Cola", wx.ICON_INFORMATION)
            return
        out = self.out_picker.GetPath().strip()
        if not out:
            wx.MessageBox("Elige un archivo o carpeta de salida.", "Cola", wx.ICON_INFORMATION)
            return

        fmt = self.current_format()
        split = self.chk_split.GetValue()
        out = str(Path(out).with_suffix("")) if split else str(Path(out).with_suffix(format_extension(fmt)))
        opts = self._build_options(out)

        self.settings.update({
            "lang": opts.lang,
            "voice": opts.voice,
            f"voice_{opts.lang}": opts.voice,
            "engine": opts.engine,
            "format": opts.format,
            "chapter_files": opts.chapter_files,
            "speed": opts.speed,
            "device": opts.device,
            "bitrate": opts.bitrate,
            "read_titles": opts.read_titles,
            "read_credits": opts.read_credits,
            "keep_work_files": opts.keep_work_files,
            "last_out_dir": str(Path(out).parent),
            **self._adv_options,
        })
        save_settings(self.settings)
        self._stop_preview()

        job = self.queue_mgr.enqueue(self.book, chapters, opts, prioritize=prioritize)
        self._current_book_job_id = job.id
        self._chapter_statuses.clear()
        self._populate_chapter_list(keep_chapter=self._selected_chapter())

        if prioritize:
            self.SetStatusText(f"'{job.title}' añadido al frente de la cola.")
        else:
            self.SetStatusText(f"'{job.title}' añadido a la cola de producción.")

    def _on_job_chapter(self, job_id: str, idx: int, state: str) -> None:
        if job_id != self._current_book_job_id or idx <= 0:
            return
        self._chapter_statuses[idx] = STATUS_TEXT.get(state, state)
        for row, ch in enumerate(self._visible_chapters):
            if ch.index == idx:
                self.list.SetItem(row, 5, self._chapter_statuses[idx])
                if state in ("synth", "encode"):
                    self.list.EnsureVisible(row)
                break

    def _on_queue_update(self, _job_id: str) -> None:
        self.queue_panel.refresh()
        jobs = self.queue_mgr.get_jobs()
        queued_cnt = sum(1 for j in jobs if j.status in ("running", "paused", "pending"))
        self.notebook.SetPageText(1, f"Cola de Producción ({queued_cnt})")
        self._sync_live_bar()

    def _sync_live_bar(self) -> None:
        job = self.queue_mgr.primary_active_job()
        if not job:
            self.lbl_live_title.SetLabel("Sin trabajos activos")
            self.lbl_live_state.SetLabel("Listo")
            self.lbl_live_state.SetForegroundColour(self._pal.muted)
            self.lbl_live_detail.SetLabel("Carga un EPUB o PDF (o arrástralo aquí) para comenzar.")
            self.gauge.SetValue(0)
            self.btn_live_pause.Disable()
            self.btn_live_cancel.Disable()
            self.btn_live_open.Disable()
            return

        self.lbl_live_title.SetLabel(f"{job.title} ({job.options.format.upper()} • {job.options.engine})")
        state_map = {
            "pending": ("En espera", self._pal.muted),
            "running": ("Sintetizando", self._pal.primary),
            "paused": ("Pausado", self._pal.warning),
            "done": ("Completado", self._pal.success),
            "error": ("Error", self._pal.error),
            "cancelled": ("Cancelado", self._pal.muted),
        }
        st_lbl, st_col = state_map.get(job.status, (job.status, self._pal.text))
        self.lbl_live_state.SetLabel(st_lbl)
        self.lbl_live_state.SetForegroundColour(st_col)

        extra = []
        if job.realtime_x > 0:
            extra.append(f"{job.realtime_x:.1f}x realtime")
        if job.eta_text and job.status == "running":
            extra.append(f"ETA {job.eta_text}")
        detail = job.status_text + (f"  •  {'  •  '.join(extra)}" if extra else "")
        self.lbl_live_detail.SetLabel(detail)
        self.gauge.SetValue(int(max(0.0, min(1.0, job.progress)) * 1000))

        self.btn_live_pause.Enable(job.status in ("running", "paused"))
        self.btn_live_pause.SetLabel("▶ Reanudar" if job.status == "paused" else "⏸ Pausar")
        self.btn_live_cancel.Enable(job.status in ("running", "paused", "pending"))

        has_out = bool(job.result_path and os.path.exists(job.result_path))
        self.btn_live_open.Enable(has_out)
        if has_out:
            self._last_output = job.result_path

    def _on_live_pause_resume(self, _evt) -> None:
        job = self.queue_mgr.primary_active_job()
        if not job:
            return
        if job.status == "running":
            self.queue_mgr.pause_job(job.id)
        elif job.status == "paused":
            self.queue_mgr.resume_job(job.id)

    def _on_live_cancel(self, _evt) -> None:
        job = self.queue_mgr.primary_active_job()
        if job:
            self.queue_mgr.cancel_job(job.id)

    def _open_active_result(self) -> None:
        job = self.queue_mgr.primary_active_job()
        if job and job.result_path and os.path.exists(job.result_path):
            self.open_player(job.result_path)

    # ------------------------------------------------------------------ close
    def on_close(self, _evt) -> None:
        _log_line("closing")
        if self.queue_mgr.has_active_jobs():
            if wx.MessageBox(
                "Hay conversiones activas o en pausa en la cola.\n¿Deseas cancelarlas y salir?",
                "Salir",
                wx.YES_NO | wx.ICON_QUESTION,
            ) != wx.YES:
                return
        self.queue_mgr.shutdown()
        save_settings(self.settings)
        self._stop_preview()
        if self._preview_engine is not None:
            try:
                self._preview_engine.release()  # type: ignore[attr-defined]
            except Exception:
                pass
        if self._player:
            try:
                self._player.Close()
            except Exception:
                pass
        self.Destroy()


def main(epub_path: Optional[str] = None) -> None:
    def _excepthook(tp, value, tb):
        if value is not None:
            _record_error("unhandled", value)
        sys.__excepthook__(tp, value, tb)

    def _thread_hook(args):
        if args.exc_value is not None:
            name = args.thread.name if args.thread is not None else "?"
            _record_error(f"thread {name}", args.exc_value)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    _log_line(f"app start (argv={sys.argv[1:]}, error log: {_error_log_path()})")

    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Epub2Audiobook.Desktop")
        except Exception:
            pass

    app = App(False)
    frame = MainFrame()
    frame.Show()
    if epub_path and os.path.exists(epub_path):
        frame._on_files_dropped([os.path.abspath(epub_path)])
    app.MainLoop()
    _log_line("main loop returned")
