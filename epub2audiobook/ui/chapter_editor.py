"""Integrated section editor and real-time TTS chunk visualizer."""
from __future__ import annotations

from typing import Callable, Optional

import wx

from ..epub_parser import Block, Chapter
from ..section_editor import analyze_chapter_tts, blocks_to_text, text_to_blocks
from .theme import Palette, make_bold

KNOWN_KINDS = [
    "chapter", "body", "introduction", "prologue", "foreword", "preface",
    "epilogue", "afterword", "conclusion", "part", "dedication", "epigraph",
    "appendix", "frontmatter", "backmatter", "acknowledgments", "notes",
]


class ChapterInspectorPanel(wx.Panel):
    """Right-hand Studio panel: live chapter editor + TTS chunk visualizer."""

    def __init__(
        self,
        parent: wx.Window,
        palette: Palette,
        on_changed: Callable[[Chapter], None],
        on_restore: Callable[[Chapter], None],
        on_split: Callable[[Chapter, list[Block], list[Block]], None],
        on_preview_text: Callable[[str], None],
        get_tts_params: Callable[[], dict],
    ):
        super().__init__(parent)
        self._pal = palette
        self._on_changed = on_changed
        self._on_restore = on_restore
        self._on_split = on_split
        self._on_preview_text = on_preview_text
        self._get_tts_params = get_tts_params

        self._chapter: Optional[Chapter] = None
        self._loading = False
        self._mode = "edit"  # "edit" | "tts"

        self.SetBackgroundColour(palette.surface)
        self._build_ui()
        self._set_enabled(False)

    def _build_ui(self) -> None:
        d = self.FromDIP
        root = wx.BoxSizer(wx.VERTICAL)

        # Header row
        hdr = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_header = wx.StaticText(self, label="Visualizador & Editor de Sección")
        make_bold(self.lbl_header)
        hdr.Add(self.lbl_header, 1, wx.ALIGN_CENTER_VERTICAL)

        self.lbl_edited = wx.StaticText(self, label="")
        self.lbl_edited.SetForegroundColour(self._pal.warning)
        make_bold(self.lbl_edited)
        hdr.Add(self.lbl_edited, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(8))

        self.btn_mode_edit = wx.ToggleButton(self, label="Editar texto")
        self.btn_mode_tts = wx.ToggleButton(self, label="Vista TTS (Chunks)")
        self.btn_mode_edit.SetValue(True)
        hdr.Add(self.btn_mode_edit, 0, wx.RIGHT, d(4))
        hdr.Add(self.btn_mode_tts, 0)
        root.Add(hdr, 0, wx.EXPAND | wx.ALL, d(8))

        # Metadata row for the selected section
        meta = wx.BoxSizer(wx.HORIZONTAL)
        meta.Add(wx.StaticText(self, label="Título:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(6))
        self.txt_title = wx.TextCtrl(self)
        meta.Add(self.txt_title, 1, wx.EXPAND | wx.RIGHT, d(8))
        meta.Add(wx.StaticText(self, label="Tipo:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(4))
        self.choice_kind = wx.Choice(self, choices=KNOWN_KINDS)
        meta.Add(self.choice_kind, 0)
        root.Add(meta, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(8))

        # Action toolbar
        tbar = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_break = wx.Button(self, label="* * * Pausa")
        self.btn_split = wx.Button(self, label="Dividir aquí")
        self.btn_restore = wx.Button(self, label="Restaurar")
        self.btn_speak = wx.Button(self, label="▶ Escuchar")
        self.btn_speak.SetToolTip("Sintetiza el fragmento seleccionado o los primeros párrafos de esta sección")
        tbar.Add(self.btn_break, 0, wx.RIGHT, d(4))
        tbar.Add(self.btn_split, 0, wx.RIGHT, d(4))
        tbar.Add(self.btn_restore, 0, wx.RIGHT, d(4))
        tbar.AddStretchSpacer()
        tbar.Add(self.btn_speak, 0)
        root.Add(tbar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(6))

        # Hint + live stats bar
        sbar = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_hint = wx.StaticText(self, label="Usa '## Título' para encabezados y '* * *' para pausas largas.")
        self.lbl_hint.SetForegroundColour(self._pal.muted)
        self.lbl_stats = wx.StaticText(self, label="0 palabras • 0 chunks")
        self.lbl_stats.SetForegroundColour(self._pal.muted)
        sbar.Add(self.lbl_hint, 1, wx.ALIGN_CENTER_VERTICAL)
        sbar.Add(self.lbl_stats, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(sbar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(6))

        # Editor TextCtrl
        self.txt_editor = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_PROCESS_ENTER | wx.TE_RICH2,
        )
        self.txt_editor.SetFont(wx.Font(wx.FontInfo(10.5)))
        root.Add(self.txt_editor, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(8))

        # TTS Chunks Rich Viewer (hidden initially)
        self.txt_tts = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
        )
        self.txt_tts.SetFont(wx.Font(wx.FontInfo(10)))
        self.txt_tts.Hide()
        root.Add(self.txt_tts, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(8))

        self.SetSizer(root)

        # Events
        self.btn_mode_edit.Bind(wx.EVT_TOGGLEBUTTON, lambda _e: self._switch_mode("edit"))
        self.btn_mode_tts.Bind(wx.EVT_TOGGLEBUTTON, lambda _e: self._switch_mode("tts"))
        self.txt_title.Bind(wx.EVT_TEXT, self._on_title_or_kind_edit)
        self.choice_kind.Bind(wx.EVT_CHOICE, self._on_title_or_kind_edit)
        self.txt_editor.Bind(wx.EVT_TEXT, self._on_text_edit)
        self.btn_break.Bind(wx.EVT_BUTTON, self._on_insert_break)
        self.btn_split.Bind(wx.EVT_BUTTON, self._on_split_click)
        self.btn_restore.Bind(wx.EVT_BUTTON, self._on_restore_click)
        self.btn_speak.Bind(wx.EVT_BUTTON, self._on_speak_click)

    def _set_enabled(self, enabled: bool) -> None:
        for w in (self.txt_title, self.choice_kind, self.btn_break, self.btn_split,
                  self.btn_restore, self.btn_speak, self.txt_editor, self.btn_mode_edit, self.btn_mode_tts):
            w.Enable(enabled)

    def _switch_mode(self, mode: str) -> None:
        self._mode = mode
        self.btn_mode_edit.SetValue(mode == "edit")
        self.btn_mode_tts.SetValue(mode == "tts")
        if mode == "edit":
            self.txt_tts.Hide()
            self.txt_editor.Show()
            self.lbl_hint.SetLabel("Usa '## Título' para encabezados y '* * *' para pausas largas.")
        else:
            self.txt_editor.Hide()
            self.txt_tts.Show()
            self.lbl_hint.SetLabel("Simulación real de normalize_text() + chunk_paragraph().")
            self.refresh_tts_view()
        self.Layout()

    def set_chapter(self, ch: Optional[Chapter], edited: bool = False) -> None:
        self._loading = True
        try:
            self._chapter = ch
            if ch is None:
                self._set_enabled(False)
                self.txt_title.SetValue("")
                self.txt_editor.SetValue("")
                self.txt_tts.SetValue("")
                self.lbl_edited.SetLabel("")
                self.lbl_stats.SetLabel("Sin sección seleccionada")
                return

            self._set_enabled(True)
            self.txt_title.SetValue(ch.title)
            if ch.kind in KNOWN_KINDS:
                self.choice_kind.SetStringSelection(ch.kind)
            else:
                self.choice_kind.Append(ch.kind)
                self.choice_kind.SetStringSelection(ch.kind)
            self.txt_editor.SetValue(blocks_to_text(ch.blocks))
            self.set_edited_badge(edited)
            self.refresh_tts_view()
        finally:
            self._loading = False

    def set_edited_badge(self, edited: bool) -> None:
        self.lbl_edited.SetLabel("✎ Editado" if edited else "")
        self.btn_restore.Enable(edited and self._chapter is not None)
        self.Layout()

    def refresh_tts_view(self) -> None:
        if not self._chapter:
            return
        params = self._get_tts_params()
        info = analyze_chapter_tts(
            self._chapter.title,
            self._chapter.blocks,
            lang=params.get("lang", "a"),
            max_chars=int(params.get("max_chars", 350)),
            read_titles=bool(params.get("read_titles", True)),
            speed=float(params.get("speed", 1.0)),
        )
        warn_suffix = f" • ⚠ {info['warnings']} aviso(s)" if info["warnings"] else ""
        self.lbl_stats.SetLabel(
            f"{info['words']:,} pal. • {info['chunks']} chunks • ~{info['est_minutes']:.1f}m{warn_suffix}"
        )

        if self._mode != "tts":
            return

        self.txt_tts.Freeze()
        try:
            self.txt_tts.Clear()
            bold_font = self.txt_tts.GetFont().Bold()
            norm_font = self.txt_tts.GetFont()
            mono_font = wx.Font(wx.FontInfo(9).Family(wx.FONTFAMILY_TELETYPE))

            chunk_n = 0
            for item in info["items"]:
                kind = item["kind"]
                if kind == "break":
                    self.txt_tts.SetDefaultStyle(wx.TextAttr(self._pal.muted, wx.NullColour, mono_font))
                    self.txt_tts.AppendText("──────────  Pausa de escena (* * *)  ──────────\n\n")
                    continue

                if kind == "title":
                    hdr = f"[Título de capítulo • {item['chars']} car.]"
                elif kind == "heading":
                    hdr = f"[Subtítulo • {item['chars']} car.]"
                else:
                    chunk_n += 1
                    hdr = f"[Chunk #{chunk_n} • {item['chars']} car.]"

                if item["warnings"]:
                    hdr += "  ⚠ " + ", ".join(item["warnings"])
                    self.txt_tts.SetDefaultStyle(wx.TextAttr(self._pal.warning, wx.NullColour, mono_font))
                else:
                    self.txt_tts.SetDefaultStyle(wx.TextAttr(self._pal.primary, wx.NullColour, mono_font))
                self.txt_tts.AppendText(hdr + "\n")

                body_font = bold_font if kind in ("title", "heading") else norm_font
                self.txt_tts.SetDefaultStyle(wx.TextAttr(self._pal.text, wx.NullColour, body_font))
                self.txt_tts.AppendText(item["text"] + "\n\n")
        finally:
            self.txt_tts.ShowPosition(0)
            self.txt_tts.Thaw()

    def _on_title_or_kind_edit(self, _evt) -> None:
        if self._loading or not self._chapter:
            return
        self._chapter.title = self.txt_title.GetValue().strip() or self._chapter.title
        sel = self.choice_kind.GetStringSelection()
        if sel:
            self._chapter.kind = sel
        self.refresh_tts_view()
        self._on_changed(self._chapter)

    def _on_text_edit(self, _evt) -> None:
        if self._loading or not self._chapter:
            return
        self._chapter.blocks = text_to_blocks(self.txt_editor.GetValue())
        self.refresh_tts_view()
        self._on_changed(self._chapter)

    def _on_insert_break(self, _evt) -> None:
        if not self._chapter:
            return
        pos = self.txt_editor.GetInsertionPoint()
        val = self.txt_editor.GetValue()
        ins = "\n\n* * *\n\n"
        self.txt_editor.SetValue(val[:pos] + ins + val[pos:])
        self.txt_editor.SetInsertionPoint(pos + len(ins))
        self.txt_editor.SetFocus()

    def _on_split_click(self, _evt) -> None:
        if not self._chapter:
            return
        pos = self.txt_editor.GetInsertionPoint()
        val = self.txt_editor.GetValue()
        left_txt = val[:pos].strip()
        right_txt = val[pos:].strip()
        if not left_txt or not right_txt:
            wx.MessageBox(
                "Coloca el cursor en medio del texto (donde quieras dividir la sección en dos capítulos).",
                "Dividir sección",
                wx.ICON_INFORMATION,
            )
            return
        before_blocks = text_to_blocks(left_txt)
        after_blocks = text_to_blocks(right_txt)
        if before_blocks and after_blocks:
            self._on_split(self._chapter, before_blocks, after_blocks)

    def _on_restore_click(self, _evt) -> None:
        if self._chapter:
            self._on_restore(self._chapter)

    def _on_speak_click(self, _evt) -> None:
        if not self._chapter:
            return
        sel = self.txt_editor.GetStringSelection().strip() if self._mode == "edit" else ""
        if not sel:
            sel = " ".join(b.text for b in self._chapter.blocks if b.kind != "break")[:650]
        if not sel:
            wx.MessageBox("Esta sección no tiene texto narrable para previsualizar.", "Escuchar", wx.ICON_INFORMATION)
            return
        self._on_preview_text(sel)
