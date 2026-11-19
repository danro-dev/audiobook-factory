"""Unit & GUI smoke tests for section editor, pause/resume converter, and queue manager."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from epub2audiobook.converter import CancelledError, ConvertOptions, Converter
from epub2audiobook.epub_parser import Block, Book, Chapter
from epub2audiobook.queue_manager import QueueManager
from epub2audiobook.section_editor import (
    analyze_chapter_tts,
    blocks_to_text,
    clone_book_and_chapters,
    is_chapter_edited,
    load_project_edits,
    save_project_edits,
    snapshot_book,
    text_to_blocks,
)


class _FakeEngine:
    label = "FakeTTS"
    device = "cpu"

    def __init__(self, delay: float = 0.02):
        self.delay = delay
        self.calls: list[str] = []

    def warm_up(self, lang: str, voice: str) -> None:
        return None

    def synthesize(self, text: str, voice: str, lang: str, speed: float = 1.0) -> np.ndarray:
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        return np.full(2400, 0.05, dtype=np.float32)

    def release(self) -> None:
        return None


def _sample_book(tmp_dir: Path) -> Book:
    p = tmp_dir / "demo.epub"
    p.write_bytes(b"dummy")
    ch1 = Chapter(
        index=1,
        href="ch1.xhtml",
        title="Chapter One",
        kind="chapter",
        blocks=[
            Block(kind="heading", text="Chapter One", level=2),
            Block(kind="para", text="First sentence in the first chapter. Second sentence follows."),
            Block(kind="break"),
            Block(kind="para", text="After the break in chapter one."),
        ],
        include_default=True,
    )
    ch2 = Chapter(
        index=2,
        href="ch2.xhtml",
        title="Chapter Two",
        kind="chapter",
        blocks=[
            Block(kind="para", text="Visit https://example.com in 2026 for MORE DETAILS [12]."),
        ],
        include_default=True,
    )
    return Book(
        path=str(p),
        title="Demo Book",
        authors=["Test Author"],
        language_iso="en",
        kokoro_lang="a",
        year="2026",
        cover_bytes=None,
        cover_ext=".jpg",
        chapters=[ch1, ch2],
        subtitle="A Test",
    )


class SectionEditorTests(unittest.TestCase):
    def test_blocks_roundtrip_and_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            book = _sample_book(Path(td))
            snaps = snapshot_book(book)
            self.assertFalse(is_chapter_edited(book.chapters[0], snaps))

            txt = blocks_to_text(book.chapters[0].blocks)
            self.assertIn("## Chapter One", txt)
            self.assertIn("* * *", txt)

            parsed = text_to_blocks(txt + "\n\nNew paragraph added.")
            self.assertEqual(parsed[0].kind, "heading")
            self.assertEqual(parsed[2].kind, "break")
            self.assertEqual(parsed[-1].text, "New paragraph added.")

            book.chapters[0].blocks = parsed
            self.assertTrue(is_chapter_edited(book.chapters[0], snaps))

    def test_analyze_tts_detects_warnings(self):
        with tempfile.TemporaryDirectory() as td:
            book = _sample_book(Path(td))
            info = analyze_chapter_tts(book.chapters[1].title, book.chapters[1].blocks, lang="a", max_chars=200)
            self.assertGreaterEqual(info["chunks"], 1)
            all_warns = [w for it in info["items"] for w in it["warnings"]]
            self.assertIn("Números", all_warns)
            self.assertIn("MAYÚSCULAS", all_warns)

    def test_clone_isolation_and_sidecar_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            book = _sample_book(Path(td))
            snaps = snapshot_book(book)
            cloned_b, cloned_chs = clone_book_and_chapters(book, book.chapters)
            book.title = "Mutated Original"
            book.chapters[0].blocks[1].text = "Changed"
            self.assertEqual(cloned_b.title, "Demo Book")
            self.assertNotEqual(cloned_chs[0].blocks[1].text, "Changed")

            save_project_edits(book, {1}, snaps)
            reloaded = _sample_book(Path(td))
            res = load_project_edits(reloaded)
            self.assertIsNotNone(res)
            self.assertEqual(reloaded.title, "Mutated Original")
            self.assertEqual(res["selected_indices"], {1})
            self.assertEqual(reloaded.chapters[0].blocks[1].text, "Changed")


class ConverterPauseTests(unittest.TestCase):
    def test_pause_resume_and_cancel_while_paused(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            book = _sample_book(td_path)
            pause_ev = threading.Event()
            cancel_ev = threading.Event()
            states: list[str] = []

            # Pause as soon as first chunk finishes
            class _PausingEngine(_FakeEngine):
                def synthesize(self, text: str, voice: str, lang: str, speed: float = 1.0) -> np.ndarray:
                    out = super().synthesize(text, voice, lang, speed)
                    if len(self.calls) == 1:
                        pause_ev.set()
                    return out

            opts = ConvertOptions(
                output_path=str(td_path / "out.m4b"),
                read_credits=False,
                keep_work_files=True,
            )
            conv = Converter(
                book,
                [book.chapters[0]],
                opts,
                cancel_event=cancel_ev,
                pause_event=pause_ev,
                engine=_PausingEngine(delay=0.01),
                on_detail=lambda d: states.append(str(d.get("state"))),
            )

            err_holder: list[BaseException] = []

            def run_conv():
                try:
                    conv.run()
                except BaseException as exc:
                    err_holder.append(exc)

            t = threading.Thread(target=run_conv, daemon=True)
            t.start()
            # Wait until paused state is reported
            deadline = time.time() + 5.0
            while "paused" not in states and time.time() < deadline:
                time.sleep(0.02)
            self.assertIn("paused", states)
            self.assertTrue(t.is_alive())

            # Cancelling while paused must abort cleanly with CancelledError
            cancel_ev.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())
            self.assertTrue(err_holder and isinstance(err_holder[0], CancelledError))

    def test_cache_key_changes_when_chapter_edited(self):
        with tempfile.TemporaryDirectory() as td:
            book = _sample_book(Path(td))
            opts = ConvertOptions(output_path=str(Path(td) / "out.m4b"))
            conv = Converter(book, book.chapters, opts, engine=_FakeEngine())
            k1 = conv._cache_key("af_heart", book.chapters[0])
            k2_before = conv._cache_key("af_heart", book.chapters[1])
            book.chapters[0].blocks[1].text = "Edited text in chapter one."
            k1_after = conv._cache_key("af_heart", book.chapters[0])
            k2_after = conv._cache_key("af_heart", book.chapters[1])
            self.assertNotEqual(k1, k1_after)
            self.assertEqual(k2_before, k2_after)


class QueueManagerTests(unittest.TestCase):
    def test_hybrid_resource_grouping_and_queue_operations(self):
        with tempfile.TemporaryDirectory() as td:
            book = _sample_book(Path(td))
            qm = QueueManager(engine_factory=lambda _n, _d: _FakeEngine(delay=0.05))
            opts1 = ConvertOptions(output_path=str(Path(td) / "a.m4b"), engine="kokoro", device="cpu", read_credits=False)
            opts2 = ConvertOptions(output_path=str(Path(td) / "b.m4b"), engine="supertonic", device="cpu", read_credits=False)

            j1 = qm.enqueue(book, [book.chapters[0]], opts1)
            j2 = qm.enqueue(book, [book.chapters[1]], opts2)
            j3 = qm.enqueue(book, [book.chapters[0]], opts1)
            self.assertEqual(j1.resource_group(), "cpu")
            self.assertEqual(j2.resource_group(), "cpu")
            self.assertEqual(len(qm.get_jobs()), 3)
            self.assertTrue(qm.move_job(j3.id, -1) or qm.get_job(j3.id).status != "pending")
            qm.shutdown(wait_timeout=3.0)

    def test_sequential_queue_pause_resume_and_complete(self):
        from epub2audiobook.audio import find_ffmpeg
        if not find_ffmpeg():
            self.skipTest("ffmpeg not available")
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            book = _sample_book(td_path)
            done_event = threading.Event()

            def on_upd(_jid):
                jobs = qm.get_jobs()
                if jobs and all(j.status == "done" for j in jobs):
                    done_event.set()

            qm = QueueManager(on_update=on_upd, engine_factory=lambda _n, _d: _FakeEngine(delay=0.04))
            try:
                opts1 = ConvertOptions(output_path=str(td_path / "book1.m4b"), read_credits=False)
                opts2 = ConvertOptions(output_path=str(td_path / "book2.m4b"), read_credits=False)
                j1 = qm.enqueue(book, [book.chapters[0]], opts1)
                j2 = qm.enqueue(book, [book.chapters[1]], opts2)

                time.sleep(0.05)
                if j1.status == "running":
                    self.assertTrue(qm.pause_job(j1.id))
                    self.assertEqual(j1.status, "paused")
                    time.sleep(0.1)
                    self.assertTrue(qm.resume_job(j1.id))

                self.assertTrue(done_event.wait(timeout=20.0), "Queue did not finish in time")
                self.assertTrue(Path(j1.result_path).exists())
                self.assertTrue(Path(j2.result_path).exists())
            finally:
                qm.shutdown(wait_timeout=3.0)


class GuiSmokeTests(unittest.TestCase):
    def test_mainframe_loads_book_edits_split_and_switches_tabs(self):
        import wx
        from epub2audiobook.ui.main_frame import App, MainFrame

        app = App(False)
        frame = MainFrame()
        try:
            with tempfile.TemporaryDirectory() as td:
                book = _sample_book(Path(td))
                frame._book_loaded(book, "en")
                self.assertEqual(frame.list.GetItemCount(), 2)
                frame.list.Select(0)
                frame._sync_inspector_to_selection()
                frame.inspector._switch_mode("tts")
                self.assertIn("Chunk #1", frame.inspector.txt_tts.GetValue())
                frame.inspector._switch_mode("edit")
                frame.inspector.txt_editor.SetValue("Edited part one.\n\nEdited part two.")
                self.assertTrue(is_chapter_edited(book.chapters[0], frame._snapshots))

                # Split chapter at midpoint
                frame._on_inspector_split(
                    book.chapters[0],
                    [Block(kind="para", text="Part 1")],
                    [Block(kind="para", text="Part 2")],
                )
                self.assertEqual(len(book.chapters), 3)
                self.assertEqual(frame.list.GetItemCount(), 3)

                frame.notebook.SetSelection(1)
                frame.queue_panel.refresh()
        finally:
            frame.queue_mgr.shutdown(wait_timeout=1.0)
            frame.Destroy()
            app.ProcessPendingEvents()
            app.ExitMainLoop()


if __name__ == "__main__":
    unittest.main()
