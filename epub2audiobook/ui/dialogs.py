"""Modal dialogs for editing book metadata/cover and advanced TTS/pause parameters."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import wx

from ..epub_parser import Book


class MetadataDialog(wx.Dialog):
    """Edit title, subtitle, authors, year, and optionally replace the book cover."""

    def __init__(self, parent: wx.Window, book: Book):
        super().__init__(
            parent,
            title="Editar metadatos y portada",
            size=parent.FromDIP(wx.Size(520, 400)),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._book = book
        self._new_cover_bytes: Optional[bytes] = book.cover_bytes
        self._new_cover_ext: str = book.cover_ext

        d = self.FromDIP
        root = wx.BoxSizer(wx.VERTICAL)
        top = wx.BoxSizer(wx.HORIZONTAL)

        # Cover column
        cov_col = wx.BoxSizer(wx.VERTICAL)
        self.cov_bmp = wx.StaticBitmap(self, size=d(wx.Size(120, 176)))
        self._refresh_cover()
        cov_col.Add(self.cov_bmp, 0, wx.ALIGN_CENTER | wx.BOTTOM, d(6))
        btn_cov = wx.Button(self, label="Cambiar portada…")
        btn_cov.Bind(wx.EVT_BUTTON, self._on_pick_cover)
        cov_col.Add(btn_cov, 0, wx.EXPAND)
        top.Add(cov_col, 0, wx.RIGHT, d(14))

        # Fields column
        g = wx.FlexGridSizer(0, 2, d(8), d(8))
        g.AddGrowableCol(1, 1)

        g.Add(wx.StaticText(self, label="Título:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_title = wx.TextCtrl(self, value=book.title)
        g.Add(self.txt_title, 1, wx.EXPAND)

        g.Add(wx.StaticText(self, label="Subtítulo:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_sub = wx.TextCtrl(self, value=book.subtitle or "")
        g.Add(self.txt_sub, 1, wx.EXPAND)

        g.Add(wx.StaticText(self, label="Autor(es):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_authors = wx.TextCtrl(self, value=", ".join(book.authors))
        g.Add(self.txt_authors, 1, wx.EXPAND)

        g.Add(wx.StaticText(self, label="Año:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_year = wx.TextCtrl(self, value=book.year or "")
        g.Add(self.txt_year, 1, wx.EXPAND)

        top.Add(g, 1, wx.EXPAND)
        root.Add(top, 1, wx.EXPAND | wx.ALL, d(12))
        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, d(10))
        self.SetSizer(root)

    def _refresh_cover(self) -> None:
        tw, th = self.FromDIP(wx.Size(120, 176)).Get()
        bmp = wx.Bitmap(tw, th)
        if self._new_cover_bytes:
            try:
                img = wx.Image(io.BytesIO(self._new_cover_bytes))
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
        self.cov_bmp.SetBitmap(bmp)

    def _on_pick_cover(self, _evt) -> None:
        with wx.FileDialog(
            self,
            "Seleccionar imagen de portada",
            wildcard="Imágenes (*.jpg;*.jpeg;*.png;*.webp)|*.jpg;*.jpeg;*.png;*.webp",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            p = Path(dlg.GetPath())
            try:
                self._new_cover_bytes = p.read_bytes()
                self._new_cover_ext = p.suffix.lower() or ".jpg"
                self._refresh_cover()
            except Exception as e:
                wx.MessageBox(f"No se pudo leer la imagen:\n{e}", "Portada", wx.ICON_ERROR)

    def apply_to_book(self) -> None:
        title = self.txt_title.GetValue().strip()
        if title:
            self._book.title = title
        self._book.subtitle = self.txt_sub.GetValue().strip()
        authors = [a.strip() for a in self.txt_authors.GetValue().split(",") if a.strip()]
        self._book.authors = authors
        self._book.year = self.txt_year.GetValue().strip() or None
        self._book.cover_bytes = self._new_cover_bytes
        self._book.cover_ext = self._new_cover_ext


class AdvancedOptionsDialog(wx.Dialog):
    """Configure TTS chunk size, cadence pauses, and audio loudness target."""

    PRESETS = {
        "Audiolibro natural (recomendado)": {
            "max_chars": 350, "pause_chunk": 0.15, "pause_para": 0.55,
            "pause_heading": 1.0, "pause_break": 1.3, "pause_chapter_start": 0.7,
            "pause_chapter_end": 1.2, "target_rms_db": -20.0,
        },
        "Lectura ágil / dinámica": {
            "max_chars": 380, "pause_chunk": 0.08, "pause_para": 0.35,
            "pause_heading": 0.7, "pause_break": 0.9, "pause_chapter_start": 0.4,
            "pause_chapter_end": 0.8, "target_rms_db": -19.0,
        },
        "Estudio / pausas marcadas": {
            "max_chars": 280, "pause_chunk": 0.25, "pause_para": 0.80,
            "pause_heading": 1.3, "pause_break": 1.8, "pause_chapter_start": 0.9,
            "pause_chapter_end": 1.5, "target_rms_db": -20.0,
        },
    }

    def __init__(self, parent: wx.Window, values: dict):
        super().__init__(
            parent,
            title="Ajustes avanzados de narración",
            size=parent.FromDIP(wx.Size(440, 420)),
            style=wx.DEFAULT_DIALOG_STYLE,
        )
        d = self.FromDIP
        root = wx.BoxSizer(wx.VERTICAL)

        prow = wx.BoxSizer(wx.HORIZONTAL)
        prow.Add(wx.StaticText(self, label="Preset:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(8))
        self.preset = wx.Choice(self, choices=list(self.PRESETS.keys()))
        self.preset.SetSelection(0)
        self.preset.Bind(wx.EVT_CHOICE, self._on_preset)
        prow.Add(self.preset, 1, wx.EXPAND)
        root.Add(prow, 0, wx.EXPAND | wx.ALL, d(10))

        g = wx.FlexGridSizer(0, 2, d(6), d(10))
        g.AddGrowableCol(0, 1)

        self._ctrls: dict[str, wx.Control] = {}
        specs = [
            ("max_chars", "Máx. caracteres por chunk:", "int", 120, 600, 10),
            ("pause_chunk", "Pausa entre frases/chunks (s):", "float", 0.0, 2.0, 0.05),
            ("pause_para", "Pausa entre párrafos (s):", "float", 0.1, 3.0, 0.05),
            ("pause_heading", "Pausa tras subtítulos (s):", "float", 0.2, 3.0, 0.1),
            ("pause_break", "Pausa en * * * (s):", "float", 0.3, 4.0, 0.1),
            ("pause_chapter_start", "Silencio inicial de capítulo (s):", "float", 0.0, 3.0, 0.1),
            ("pause_chapter_end", "Silencio final de capítulo (s):", "float", 0.2, 4.0, 0.1),
            ("target_rms_db", "Normalización RMS (dBFS):", "float", -30.0, -12.0, 0.5),
        ]
        for key, label, kind, lo, hi, inc in specs:
            g.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            if kind == "int":
                ctrl = wx.SpinCtrl(self, min=int(lo), max=int(hi), initial=int(values.get(key, 350)))
            else:
                ctrl = wx.SpinCtrlDouble(self, min=float(lo), max=float(hi), inc=float(inc),
                                         initial=float(values.get(key, 0.5)))
                ctrl.SetDigits(2)
            self._ctrls[key] = ctrl
            g.Add(ctrl, 0, wx.ALIGN_RIGHT)

        root.Add(g, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, d(12))
        root.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, d(10))
        self.SetSizer(root)

    def _on_preset(self, _evt) -> None:
        name = self.preset.GetStringSelection()
        data = self.PRESETS.get(name)
        if not data:
            return
        for k, v in data.items():
            ctrl = self._ctrls.get(k)
            if ctrl is not None:
                ctrl.SetValue(v)

    def get_values(self) -> dict:
        return {k: ctrl.GetValue() for k, ctrl in self._ctrls.items()}
