"""Compatibility facade for the modular wxPython UI in `epub2audiobook.ui`."""
from __future__ import annotations

from .ui.main_frame import App, MainFrame, SETTINGS_FILE, load_settings, main, save_settings

__all__ = ["App", "MainFrame", "SETTINGS_FILE", "load_settings", "main", "save_settings"]

if __name__ == "__main__":
    main()
