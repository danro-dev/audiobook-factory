"""Multi-book conversion queue manager with pause/resume and optional hybrid GPU+CPU concurrency."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .audio import fmt_hms
from .converter import CancelledError, ConvertOptions, Converter
from .epub_parser import Book, Chapter
from .section_editor import clone_book_and_chapters
from .tts import make_engine, resolve_device

JobUpdateCb = Callable[[str], None]              # (job_id)
JobLogCb = Callable[[str, str], None]            # (job_id, message)
JobChapterCb = Callable[[str, int, str], None]   # (job_id, chapter_idx, state)

TERMINAL_STATES = {"done", "error", "cancelled"}


@dataclass
class ConversionJob:
    id: str
    book: Book
    chapters: list[Chapter]
    options: ConvertOptions
    status: str = "pending"   # pending | running | paused | done | error | cancelled
    progress: float = 0.0
    status_text: str = "En espera"
    detail_text: str = ""
    realtime_x: float = 0.0
    eta_text: str = ""
    current_chapter_idx: int = 0
    result_path: Optional[str] = None
    error_text: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    pause_event: threading.Event = field(default_factory=threading.Event, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def title(self) -> str:
        return self.book.title or Path(self.book.path).stem

    @property
    def author(self) -> str:
        return self.book.author

    @property
    def output_path(self) -> str:
        return self.options.output_path

    def resource_group(self) -> str:
        """Compute group ('gpu' or 'cpu') used to prevent resource contention in hybrid mode."""
        if (self.options.engine or "").lower() == "supertonic":
            return "cpu"
        return "gpu" if resolve_device(self.options.device) == "cuda" else "cpu"


class QueueManager:
    """Thread-safe queue orchestrating multiple `ConversionJob` conversions."""

    def __init__(
        self,
        on_update: Optional[JobUpdateCb] = None,
        on_log: Optional[JobLogCb] = None,
        on_chapter: Optional[JobChapterCb] = None,
        engine_factory: Optional[Callable[[str, str], object]] = None,
    ):
        self._on_update = on_update or (lambda _jid: None)
        self._on_log = on_log or (lambda _jid, _m: None)
        self._on_chapter = on_chapter or (lambda _jid, _idx, _st: None)
        self._engine_factory = engine_factory or make_engine
        self._lock = threading.RLock()
        self._jobs: list[ConversionJob] = []
        self._threads: dict[str, threading.Thread] = {}
        self._engines: dict[tuple[str, str], object] = {}
        self.hybrid_parallel: bool = False
        self._shutting_down: bool = False

    # ------------------------------------------------------------------ engine pool
    def get_engine(self, engine_name: str, device: str) -> object:
        name = (engine_name or "kokoro").lower()
        dev = "cpu" if name == "supertonic" else (device or "auto").lower()
        key = (name, dev)
        with self._lock:
            eng = self._engines.get(key)
            if eng is None:
                eng = self._engine_factory(name, dev)
                self._engines[key] = eng
            return eng

    def release_engines(self) -> None:
        with self._lock:
            for eng in self._engines.values():
                try:
                    eng.release()  # type: ignore[attr-defined]
                except Exception:
                    pass
            self._engines.clear()

    # ------------------------------------------------------------------ queue control
    def set_hybrid_parallel(self, enabled: bool) -> None:
        with self._lock:
            self.hybrid_parallel = bool(enabled)
        self._dispatch()

    def enqueue(self, book: Book, chapters: list[Chapter], options: ConvertOptions,
                prioritize: bool = False) -> ConversionJob:
        if not chapters:
            raise ValueError("No chapters selected.")
        cloned_book, cloned_chapters = clone_book_and_chapters(book, chapters)
        job = ConversionJob(
            id=uuid.uuid4().hex[:8],
            book=cloned_book,
            chapters=cloned_chapters,
            options=options,
            status_text=f"En espera ({len(cloned_chapters)} secciones)",
        )
        with self._lock:
            if prioritize:
                # Insert right after active jobs
                insert_at = 0
                for idx, j in enumerate(self._jobs):
                    if j.status in ("running", "paused"):
                        insert_at = idx + 1
                self._jobs.insert(insert_at, job)
            else:
                self._jobs.append(job)
        self._on_log(job.id, f"Encolado: '{job.title}' ({len(cloned_chapters)} secciones, {options.engine}/{options.format})")
        self._on_update(job.id)
        self._dispatch()
        return job

    def get_jobs(self) -> list[ConversionJob]:
        with self._lock:
            return list(self._jobs)

    def get_job(self, job_id: str) -> Optional[ConversionJob]:
        with self._lock:
            for j in self._jobs:
                if j.id == job_id:
                    return j
            return None

    def primary_active_job(self) -> Optional[ConversionJob]:
        with self._lock:
            for st in ("running", "paused", "pending"):
                for j in self._jobs:
                    if j.status == st:
                        return j
            return self._jobs[-1] if self._jobs else None

    def pause_job(self, job_id: str) -> bool:
        with self._lock:
            job = self.get_job(job_id)
            if not job or job.status != "running":
                return False
            job.pause_event.set()
            job.status = "paused"
            job.status_text = "Pausando…"
        self._on_update(job_id)
        return True

    def resume_job(self, job_id: str) -> bool:
        with self._lock:
            job = self.get_job(job_id)
            if not job or job.status != "paused":
                return False
            job.pause_event.clear()
            job.status = "running"
            job.status_text = "Reanudando…"
        self._on_update(job_id)
        return True

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            job = self.get_job(job_id)
            if not job or job.status in TERMINAL_STATES:
                return False
            if job.status == "pending":
                job.status = "cancelled"
                job.status_text = "Cancelado"
                job.finished_at = time.time()
                self._on_update(job_id)
                self._dispatch()
                return True
            job.cancel_event.set()
            job.pause_event.clear()
            job.status_text = "Cancelando…"
        self._on_update(job_id)
        return True

    def move_job(self, job_id: str, delta: int) -> bool:
        """Move a pending job up (`delta < 0`) or down (`delta > 0`) in the queue."""
        with self._lock:
            idx = next((i for i, j in enumerate(self._jobs) if j.id == job_id), -1)
            if idx < 0 or self._jobs[idx].status != "pending":
                return False
            new_idx = max(0, min(len(self._jobs) - 1, idx + delta))
            if new_idx == idx or self._jobs[new_idx].status in ("running", "paused"):
                return False
            self._jobs[idx], self._jobs[new_idx] = self._jobs[new_idx], self._jobs[idx]
        self._on_update(job_id)
        return True

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
            job = self.get_job(job_id)
            if not job:
                return False
            if job.status in ("running", "paused"):
                return False
            self._jobs = [j for j in self._jobs if j.id != job_id]
        self._on_update(job_id)
        return True

    def retry_job(self, job_id: str) -> Optional[ConversionJob]:
        with self._lock:
            old = self.get_job(job_id)
            if not old or old.status not in ("error", "cancelled"):
                return None
            return self.enqueue(old.book, old.chapters, old.options)

    def clear_finished(self) -> int:
        with self._lock:
            before = len(self._jobs)
            self._jobs = [j for j in self._jobs if j.status not in TERMINAL_STATES]
            removed = before - len(self._jobs)
        if removed:
            self._on_update("")
        return removed

    def has_active_jobs(self) -> bool:
        with self._lock:
            return any(j.status in ("running", "paused") for j in self._jobs)

    def shutdown(self, wait_timeout: float = 3.0) -> None:
        with self._lock:
            self._shutting_down = True
            for j in self._jobs:
                if j.status in ("running", "paused"):
                    j.cancel_event.set()
                    j.pause_event.clear()
                elif j.status == "pending":
                    j.status = "cancelled"
            threads = list(self._threads.values())
        deadline = time.time() + wait_timeout
        for t in threads:
            rem = max(0.05, deadline - time.time())
            t.join(timeout=rem)
        self.release_engines()

    # ------------------------------------------------------------------ scheduler
    def _dispatch(self) -> None:
        to_start: list[ConversionJob] = []
        with self._lock:
            if self._shutting_down:
                return
            active = [j for j in self._jobs if j.status in ("running", "paused")]
            active_groups = {j.resource_group() for j in active}
            max_workers = 2 if self.hybrid_parallel else 1

            for job in self._jobs:
                if len(active) + len(to_start) >= max_workers:
                    break
                if job.status != "pending":
                    continue
                grp = job.resource_group()
                if grp in active_groups:
                    continue
                active_groups.add(grp)
                job.status = "running"
                job.started_at = time.time()
                job.status_text = "Iniciando…"
                job.pause_event.clear()
                job.cancel_event.clear()
                to_start.append(job)

        for job in to_start:
            t = threading.Thread(target=self._run_job, args=(job,), daemon=True, name=f"job-{job.id}")
            with self._lock:
                self._threads[job.id] = t
            self._on_update(job.id)
            t.start()

    def _run_job(self, job: ConversionJob) -> None:
        def on_progress(frac: float, msg: str) -> None:
            with self._lock:
                job.progress = max(0.0, min(1.0, frac))
                job.status_text = msg
            self._on_update(job.id)

        def on_log(msg: str) -> None:
            self._on_log(job.id, msg)

        def on_chapter(idx: int, state: str) -> None:
            with self._lock:
                job.current_chapter_idx = idx
            self._on_chapter(job.id, idx, state)

        def on_detail(info: dict) -> None:
            with self._lock:
                st = info.get("state")
                if st == "paused" and job.status != "cancelled":
                    job.status = "paused"
                elif st == "running" and job.status == "paused" and not job.pause_event.is_set():
                    job.status = "running"
                if "realtime_x" in info and info["realtime_x"]:
                    job.realtime_x = float(info["realtime_x"])
                if "eta_sec" in info and info["eta_sec"]:
                    job.eta_text = fmt_hms(float(info["eta_sec"]))
                if "chunk_num" in info and info["chunk_num"]:
                    job.detail_text = f"Cap. {info.get('chapter_idx', 0)} • bloque {info['chunk_num']}"
            self._on_update(job.id)

        try:
            engine = self.get_engine(job.options.engine, job.options.device)
            conv = Converter(
                job.book,
                job.chapters,
                job.options,
                on_progress=on_progress,
                on_log=on_log,
                cancel_event=job.cancel_event,
                engine=engine,
                on_chapter=on_chapter,
                pause_event=job.pause_event,
                on_detail=on_detail,
            )
            result = conv.run()
            with self._lock:
                job.status = "done"
                job.progress = 1.0
                job.result_path = result
                job.status_text = f"Listo: {Path(result).name}"
                job.finished_at = time.time()
            self._on_log(job.id, f"Completado -> {result}")
        except CancelledError:
            with self._lock:
                job.status = "cancelled"
                job.status_text = "Cancelado (caché conservada)"
                job.finished_at = time.time()
            self._on_log(job.id, "Cancelado por el usuario.")
        except Exception as e:
            with self._lock:
                job.status = "error"
                job.error_text = f"{type(e).__name__}: {e}"
                job.status_text = f"Error: {e}"
                job.finished_at = time.time()
            self._on_log(job.id, f"ERROR: {type(e).__name__}: {e}")
        finally:
            with self._lock:
                self._threads.pop(job.id, None)
            self._on_update(job.id)
            self._dispatch()
