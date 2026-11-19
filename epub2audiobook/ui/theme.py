"""Adaptive theme palette, typography, and DPI helpers for the modern wxPython UI."""
from __future__ import annotations

from dataclasses import dataclass

import wx


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: wx.Colour
    surface: wx.Colour
    surface_alt: wx.Colour
    border: wx.Colour
    text: wx.Colour
    muted: wx.Colour
    primary: wx.Colour
    primary_text: wx.Colour
    success: wx.Colour
    warning: wx.Colour
    error: wx.Colour
    edited_bg: wx.Colour


def is_dark_mode() -> bool:
    try:
        app = wx.GetApp()
        if app is not None and hasattr(wx.SystemSettings, "GetAppearance"):
            return bool(wx.SystemSettings.GetAppearance().IsDark())
    except Exception:
        pass
    bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
    lum = 0.299 * bg.Red() + 0.587 * bg.Green() + 0.114 * bg.Blue()
    return lum < 128


def get_palette() -> Palette:
    if is_dark_mode():
        return Palette(
            dark=True,
            bg=wx.Colour(24, 26, 31),
            surface=wx.Colour(33, 36, 43),
            surface_alt=wx.Colour(41, 45, 54),
            border=wx.Colour(60, 65, 77),
            text=wx.Colour(236, 240, 246),
            muted=wx.Colour(156, 163, 175),
            primary=wx.Colour(79, 134, 247),
            primary_text=wx.Colour(255, 255, 255),
            success=wx.Colour(52, 211, 153),
            warning=wx.Colour(251, 191, 36),
            error=wx.Colour(248, 113, 113),
            edited_bg=wx.Colour(56, 48, 28),
        )
    return Palette(
        dark=False,
        bg=wx.Colour(244, 246, 250),
        surface=wx.Colour(255, 255, 255),
        surface_alt=wx.Colour(236, 240, 247),
        border=wx.Colour(214, 220, 230),
        text=wx.Colour(24, 28, 36),
        muted=wx.Colour(98, 108, 124),
        primary=wx.Colour(37, 99, 235),
        primary_text=wx.Colour(255, 255, 255),
        success=wx.Colour(16, 152, 104),
        warning=wx.Colour(202, 122, 12),
        error=wx.Colour(212, 56, 56),
        edited_bg=wx.Colour(255, 248, 225),
    )


def apply_card_style(win: wx.Window, pal: Palette, alt: bool = False) -> None:
    win.SetBackgroundColour(pal.surface_alt if alt else pal.surface)
    win.SetForegroundColour(pal.text)


def make_bold(ctrl: wx.Window, delta_pt: int = 0) -> wx.Font:
    f = ctrl.GetFont()
    if delta_pt:
        f.SetPointSize(max(8, f.GetPointSize() + delta_pt))
    f = f.Bold()
    ctrl.SetFont(f)
    return f
