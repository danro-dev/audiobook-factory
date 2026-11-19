"""Production Queue UI tab: manage multi-book queue, pause/resume, priority, and hybrid mode."""
from __future__ import annotations

import os
from typing import Callable, Optional

import wx

from ..audio import open_in_explorer
from ..queue_manager import ConversionJob, QueueManager, TERMINAL_STATES
from .theme import Palette, make_bold

STATUS_LABELS = {
    "pending": "⏳ En espera",
    "running": "▶ Sintetizando",
    "paused": "⏸ Pausado",
    "done": "✓ Completado",
    "error": "⚠ Error",
    "cancelled": "✕ Cancelado",
}


class QueuePanel(wx.Panel):
    """Queue management tab with live per-job status and controls."""

    def __init__(
        self,
        parent: wx.Window,
        palette: Palette,
        queue_mgr: QueueManager,
        on_open_player: Callable[[str], None],
        on_hybrid_changed: Callable[[bool], None],
    ):
        super().__init__(parent)
        self._pal = palette
        self._qm = queue_mgr
        self._open_player = on_open_player
        self._on_hybrid_changed = on_hybrid_changed
        self._job_ids: list[str] = []

        self.SetBackgroundColour(palette.bg)
        self._build_ui()

    def _build_ui(self) -> None:
        d = self.FromDIP
        root = wx.BoxSizer(wx.VERTICAL)

        # Header Card
        top_card = wx.Panel(self)
        top_card.SetBackgroundColour(self._pal.surface)
        top_sz = wx.BoxSizer(wx.HORIZONTAL)

        title_col = wx.BoxSizer(wx.VERTICAL)
        lbl_h = wx.StaticText(top_card, label="Cola de Producción Multi-Libro")
        make_bold(lbl_h, 1)
        self.lbl_sub = wx.StaticText(
            top_card,
            label="Encola varios libros sin bloquear el editor. Por defecto procesa 1 a la vez para máxima estabilidad.",
        )
        self.lbl_sub.SetForegroundColour(self._pal.muted)
        title_col.Add(lbl_h, 0, wx.BOTTOM, d(2))
        title_col.Add(self.lbl_sub, 0)
        top_sz.Add(title_col, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, d(12))

        self.chk_hybrid = wx.CheckBox(
            top_card,
            label="Modo híbrido (1 GPU + 1 CPU en paralelo)",
        )
        self.chk_hybrid.SetValue(self._qm.hybrid_parallel)
        self.chk_hybrid.SetToolTip(
            "Permite procesar 2 libros simultáneamente únicamente cuando uno usa Kokoro (CUDA GPU) "
            "y el otro usa Supertonic / CPU, evitando saturar la VRAM o el procesador."
        )
        top_sz.Add(self.chk_hybrid, 0, wx.ALIGN_CENTER_VERTICAL)
        top_card.SetSizer(top_sz)
        root.Add(top_card, 0, wx.EXPAND | wx.ALL, d(8))

        # Action Toolbar
        tbar = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_pause = wx.Button(self, label="⏸ Pausar")
        self.btn_cancel = wx.Button(self, label="✕ Cancelar")
        self.btn_up = wx.Button(self, label="↑ Subir")
        self.btn_down = wx.Button(self, label="↓ Bajar")
        self.btn_retry = wx.Button(self, label="↻ Reintentar")
        self.btn_remove = wx.Button(self, label="Quitar")
        self.btn_clear = wx.Button(self, label="Limpiar terminados")
        self.btn_folder = wx.Button(self, label="Abrir carpeta")
        self.btn_play = wx.Button(self, label="▶ Reproductor")

        for b in (self.btn_pause, self.btn_cancel, self.btn_up, self.btn_down,
                  self.btn_retry, self.btn_remove):
            tbar.Add(b, 0, wx.RIGHT, d(4))
        tbar.AddStretchSpacer()
        for b in (self.btn_clear, self.btn_folder, self.btn_play):
            tbar.Add(b, 0, wx.LEFT, d(4))
        root.Add(tbar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(8))

        # Queue ListCtrl
        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        cols = [
            ("#", 40, wx.LIST_FORMAT_RIGHT),
            ("Libro", 260, wx.LIST_FORMAT_LEFT),
            ("Motor / Voz", 170, wx.LIST_FORMAT_LEFT),
            ("Formato", 75, wx.LIST_FORMAT_LEFT),
            ("Estado", 130, wx.LIST_FORMAT_LEFT),
            ("Progreso", 80, wx.LIST_FORMAT_RIGHT),
            ("Vel. / ETA", 145, wx.LIST_FORMAT_LEFT),
            ("Detalle", 260, wx.LIST_FORMAT_LEFT),
        ]
        for i, (name, w, fmt) in enumerate(cols):
            self.list.InsertColumn(i, name, width=d(w), format=fmt)
        root.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, d(8))

        self.SetSizer(root)

        # Events
        self.chk_hybrid.Bind(wx.EVT_CHECKBOX, self._on_hybrid_click)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda _e: self._sync_buttons())
        self.list.Bind(wx.EVT_LIST_ITEM_DESELECTED, lambda _e: self._sync_buttons())
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activated)
        self.btn_pause.Bind(wx.EVT_BUTTON, self._on_pause_resume)
        self.btn_cancel.Bind(wx.EVT_BUTTON, self._on_cancel)
        self.btn_up.Bind(wx.EVT_BUTTON, lambda _e: self._move(-1))
        self.btn_down.Bind(wx.EVT_BUTTON, lambda _e: self._move(1))
        self.btn_retry.Bind(wx.EVT_BUTTON, self._on_retry)
        self.btn_remove.Bind(wx.EVT_BUTTON, self._on_remove)
        self.btn_clear.Bind(wx.EVT_BUTTON, lambda _e: self._qm.clear_finished())
        self.btn_folder.Bind(wx.EVT_BUTTON, self._on_open_folder)
        self.btn_play.Bind(wx.EVT_BUTTON, self._on_play)

        self._sync_buttons()

    def _selected_job(self) -> Optional[ConversionJob]:
        row = self.list.GetFirstSelected()
        if 0 <= row < len(self._job_ids):
            return self._qm.get_job(self._job_ids[row])
        return None

    def refresh(self) -> None:
        selected = self._selected_job()
        sel_id = selected.id if selected else None
        jobs = self._qm.get_jobs()
        self._job_ids = [j.id for j in jobs]

        self.list.Freeze()
        try:
            # Update in place if row count matches to prevent flicker
            if self.list.GetItemCount() != len(jobs):
                self.list.DeleteAllItems()
                for i, j in enumerate(jobs):
                    self.list.InsertItem(i, str(i + 1))

            for i, j in enumerate(jobs):
                self.list.SetItem(i, 0, str(i + 1))
                self.list.SetItem(i, 1, j.title)
                self.list.SetItem(i, 2, f"{j.options.engine} • {j.options.voice}")
                self.list.SetItem(i, 3, j.options.format.upper())
                self.list.SetItem(i, 4, STATUS_LABELS.get(j.status, j.status))
                self.list.SetItem(i, 5, f"{int(round(j.progress * 100))}%")
                speed_eta = []
                if j.realtime_x > 0:
                    speed_eta.append(f"{j.realtime_x:.1f}x")
                if j.eta_text and j.status == "running":
                    speed_eta.append(f"ETA {j.eta_text}")
                self.list.SetItem(i, 6, " • ".join(speed_eta))
                self.list.SetItem(i, 7, j.status_text)

                if j.status == "done":
                    self.list.SetItemTextColour(i, self._pal.success)
                elif j.status == "paused":
                    self.list.SetItemTextColour(i, self._pal.warning)
                elif j.status == "error":
                    self.list.SetItemTextColour(i, self._pal.error)
                elif j.status == "cancelled":
                    self.list.SetItemTextColour(i, self._pal.muted)
                else:
                    self.list.SetItemTextColour(i, self._pal.text)

            if sel_id and sel_id in self._job_ids:
                idx = self._job_ids.index(sel_id)
                self.list.Select(idx)
        finally:
            self.list.Thaw()

        running_cnt = sum(1 for j in jobs if j.status in ("running", "paused"))
        pending_cnt = sum(1 for j in jobs if j.status == "pending")
        self.lbl_sub.SetLabel(
            f"{len(jobs)} trabajo(s) en historial • {running_cnt} activo(s) • {pending_cnt} en espera."
        )
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        job = self._selected_job()
        has = job is not None
        st = job.status if job else ""

        self.btn_pause.Enable(st in ("running", "paused"))
        self.btn_pause.SetLabel("▶ Reanudar" if st == "paused" else "⏸ Pausar")
        self.btn_cancel.Enable(has and st not in TERMINAL_STATES)
        self.btn_up.Enable(st == "pending")
        self.btn_down.Enable(st == "pending")
        self.btn_retry.Enable(st in ("error", "cancelled"))
        self.btn_remove.Enable(has and st not in ("running", "paused"))
        self.btn_clear.Enable(any(j.status in TERMINAL_STATES for j in self._qm.get_jobs()))

        has_result = bool(job and job.result_path and os.path.exists(job.result_path))
        self.btn_folder.Enable(has_result)
        self.btn_play.Enable(has_result)

    def _on_hybrid_click(self, _evt) -> None:
        val = self.chk_hybrid.GetValue()
        self._qm.set_hybrid_parallel(val)
        self._on_hybrid_changed(val)

    def _on_pause_resume(self, _evt) -> None:
        job = self._selected_job()
        if not job:
            return
        if job.status == "running":
            self._qm.pause_job(job.id)
        elif job.status == "paused":
            self._qm.resume_job(job.id)

    def _on_cancel(self, _evt) -> None:
        job = self._selected_job()
        if job:
            self._qm.cancel_job(job.id)

    def _move(self, delta: int) -> None:
        job = self._selected_job()
        if job:
            self._qm.move_job(job.id, delta)

    def _on_retry(self, _evt) -> None:
        job = self._selected_job()
        if job:
            self._qm.retry_job(job.id)

    def _on_remove(self, _evt) -> None:
        job = self._selected_job()
        if job:
            self._qm.remove_job(job.id)

    def _on_open_folder(self, _evt) -> None:
        job = self._selected_job()
        if job and job.result_path and os.path.exists(job.result_path):
            open_in_explorer(job.result_path)

    def _on_play(self, _evt) -> None:
        job = self._selected_job()
        if job and job.result_path and os.path.exists(job.result_path):
            self._open_player(job.result_path)

    def _on_activated(self, _evt) -> None:
        job = self._selected_job()
        if job and job.result_path and os.path.exists(job.result_path):
            self._open_player(job.result_path)
