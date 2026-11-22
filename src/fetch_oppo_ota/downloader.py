"""Multi-connection, resumable, parallel OTA download engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from .api import DOWNLOAD_USER_AGENT, OxygenUpdaterApi
from .models import (
    DownloadJob,
    DownloadStatus,
    SegmentState,
    UpdateData,
    build_ota_filename,
)

MIN_SEGMENT_SIZE = 1024 * 1024  # 1 MB minimum to split across connections
CHUNK_SIZE = 64 * 1024  # 64 KB read chunks
STATE_SAVE_INTERVAL = 1.0  # seconds
MAX_RETRIES_PER_SEGMENT = 10


def _verify_md5_sync(path: Path, expected_md5: str) -> tuple[bool, str]:
    hasher = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    actual = hasher.hexdigest().lower()
    return actual == expected_md5.lower(), actual


class TokenBucket:
    """Async token-bucket rate limiter supporting live rate updates (0 = unlimited)."""

    def __init__(self, rate_bps: int = 0) -> None:
        self._rate_bps = max(0, int(rate_bps))
        self._tokens = float(self._rate_bps)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate_bps(self) -> int:
        return self._rate_bps

    def set_rate(self, rate_bps: int) -> None:
        self._rate_bps = max(0, int(rate_bps))
        self._tokens = min(self._tokens, float(self._rate_bps))
        self._last = time.monotonic()

    async def acquire(self, num_bytes: int) -> None:
        if self._rate_bps <= 0 or num_bytes <= 0:
            return
        async with self._lock:
            rate = self._rate_bps
            if rate <= 0:
                return
            now = time.monotonic()
            elapsed = max(0.0, now - self._last)
            self._last = now
            self._tokens = min(float(rate), self._tokens + elapsed * rate)
            self._tokens -= num_bytes
            wait_time = -self._tokens / rate if self._tokens < 0 else 0.0
        if wait_time > 0:
            await asyncio.sleep(wait_time)


class DownloadManager:
    """Manages parallel, multi-connection, resumable OTA downloads."""

    def __init__(
        self,
        api: OxygenUpdaterApi,
        *,
        max_parallel: int = 3,
        default_connections: int = 8,
        global_speed_limit_bps: int = 0,
        per_file_speed_limit_bps: int = 0,
        on_update: Callable[[DownloadJob], None] | None = None,
    ) -> None:
        self.api = api
        self.max_parallel = max(1, max_parallel)
        self.default_connections = max(1, default_connections)
        self.global_speed_limit_bps = max(0, global_speed_limit_bps)
        self.per_file_speed_limit_bps = max(0, per_file_speed_limit_bps)
        self.on_update = on_update

        self._slot_cond = asyncio.Condition()
        self._active_jobs = 0
        self._global_limiter = TokenBucket(self.global_speed_limit_bps)
        self._job_limiters: dict[str, TokenBucket] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}

        self._jobs: dict[str, DownloadJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pause_flags: dict[str, bool] = {}
        self._cancel_flags: dict[str, bool] = {}

        self._http = httpx.AsyncClient(
            headers={"User-Agent": DOWNLOAD_USER_AGENT},
            timeout=httpx.Timeout(connect=15.0, read=20.0, write=20.0, pool=20.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=128,
                max_keepalive_connections=64,
            ),
        )

    async def update_settings(
        self,
        *,
        max_parallel: int | None = None,
        default_connections: int | None = None,
        global_speed_limit_bps: int | None = None,
        per_file_speed_limit_bps: int | None = None,
    ) -> None:
        if default_connections is not None:
            self.default_connections = max(1, default_connections)
        if global_speed_limit_bps is not None:
            self.global_speed_limit_bps = max(0, global_speed_limit_bps)
            self._global_limiter.set_rate(self.global_speed_limit_bps)
        if per_file_speed_limit_bps is not None:
            self.per_file_speed_limit_bps = max(0, per_file_speed_limit_bps)
            for limiter in self._job_limiters.values():
                limiter.set_rate(self.per_file_speed_limit_bps)
        if max_parallel is not None:
            self.max_parallel = max(1, max_parallel)
            async with self._slot_cond:
                self._slot_cond.notify_all()

    async def aclose(self) -> None:
        for job_id, task in list(self._tasks.items()):
            if not task.done():
                self._pause_flags[job_id] = True
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        await self._http.aclose()

    @property
    def jobs(self) -> list[DownloadJob]:
        return list(self._jobs.values())

    def get_job(self, job_id: str) -> DownloadJob | None:
        return self._jobs.get(job_id)

    def _notify(self, job: DownloadJob) -> None:
        if self.on_update:
            self.on_update(job)

    def enqueue(
        self,
        *,
        device_id: int,
        device_name: str,
        method_id: int,
        method_name: str,
        update_data: UpdateData,
        output_dir: Path,
        model_number: str = "",
        connections: int | None = None,
        verify_md5: bool = True,
    ) -> DownloadJob:
        output_dir.mkdir(parents=True, exist_ok=True)
        filename = build_ota_filename(
            model_number,
            update_data,
            fallback_prefix=f"ota_{device_id}_{method_id}",
        )
        output_path = output_dir / filename

        job_id = uuid.uuid4().hex[:8]
        job = DownloadJob(
            job_id=job_id,
            device_id=device_id,
            device_name=device_name,
            method_id=method_id,
            method_name=method_name,
            update_data=update_data,
            output_path=output_path,
            model_number=model_number,
            connections=max(1, connections or self.default_connections),
            verify_md5=verify_md5,
            total_bytes=update_data.download_size,
            url=update_data.download_url,
        )
        self._jobs[job_id] = job
        self._job_limiters[job_id] = TokenBucket(self.per_file_speed_limit_bps)
        self._refresh_locks[job_id] = asyncio.Lock()
        self._pause_flags[job_id] = False
        self._cancel_flags[job_id] = False
        self._start_job_task(job)
        return job

    def _start_job_task(self, job: DownloadJob) -> None:
        job.status = DownloadStatus.QUEUED
        job.error = None
        self._pause_flags[job.job_id] = False
        self._cancel_flags[job.job_id] = False
        self._notify(job)
        task = asyncio.create_task(self._run_job(job), name=f"dl-{job.job_id}")
        self._tasks[job.job_id] = task

    def pause(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        if job.status in (
            DownloadStatus.QUEUED,
            DownloadStatus.FETCHING_URL,
            DownloadStatus.DOWNLOADING,
        ):
            self._pause_flags[job_id] = True
            task = self._tasks.get(job_id)
            if task and not task.done():
                task.cancel()
            job.status = DownloadStatus.PAUSED
            job.speed_bps = 0.0
            job.eta_seconds = None
            job.active_connections = 0
            self._save_state(job)
            self._notify(job)

    def resume(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        if job.status in (DownloadStatus.PAUSED, DownloadStatus.FAILED):
            self._start_job_task(job)

    def cancel(self, job_id: str, delete_partial: bool = False) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        self._cancel_flags[job_id] = True
        task = self._tasks.get(job_id)
        if task and not task.done():
            task.cancel()
        job.status = DownloadStatus.CANCELLED
        job.speed_bps = 0.0
        job.eta_seconds = None
        job.active_connections = 0
        if delete_partial:
            for p in (job.part_path, job.state_path):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        self._notify(job)

    def pause_all(self) -> None:
        for job_id in list(self._jobs):
            self.pause(job_id)

    def resume_all(self) -> None:
        for job_id in list(self._jobs):
            self.resume(job_id)

    async def wait_all(self) -> None:
        while True:
            pending = [t for t in self._tasks.values() if not t.done()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)

    # ── State Persistence ──────────────────────────────────────────────

    def _save_state(self, job: DownloadJob) -> None:
        if not job.segments:
            return
        payload = {
            "version": 1,
            "device_id": job.device_id,
            "method_id": job.method_id,
            "ota_version_number": job.update_data.ota_version_number,
            "md5sum": job.update_data.md5sum,
            "total_bytes": job.total_bytes,
            "segments": [s.to_dict() for s in job.segments],
        }
        tmp = job.state_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, job.state_path)
        except OSError:
            pass

    def _load_state(self, job: DownloadJob) -> bool:
        if not job.state_path.exists() or not job.part_path.exists():
            return False
        try:
            data = json.loads(job.state_path.read_text(encoding="utf-8"))
            if data.get("version") != 1:
                return False
            saved_total = int(data.get("total_bytes") or 0)
            if job.total_bytes > 0 and saved_total != job.total_bytes:
                return False
            if (
                job.update_data.ota_version_number
                and data.get("ota_version_number")
                and data["ota_version_number"] != job.update_data.ota_version_number
            ):
                return False
            segments = [SegmentState.from_dict(s) for s in data.get("segments", [])]
            if not segments:
                return False
            job.total_bytes = saved_total
            job.segments = segments
            job.downloaded_bytes = sum(s.downloaded for s in segments)
            return True
        except Exception:
            return False

    # ── Core Execution ─────────────────────────────────────────────────

    async def _probe_url(self, url: str) -> tuple[int, bool]:
        """Return (content_length, supports_ranges)."""
        head_length = 0
        try:
            resp = await self._http.head(url)
            if resp.status_code < 400:
                head_length = int(resp.headers.get("Content-Length") or 0)
                accept_ranges = resp.headers.get("Accept-Ranges", "").lower()
                if head_length > 0 and accept_ranges == "bytes":
                    return head_length, True
        except httpx.HTTPError:
            pass

        # Always verify Range support via 1-byte GET if HEAD didn't confirm it
        async with self._http.stream(
            "GET", url, headers={"Range": "bytes=0-0"}
        ) as resp:
            if resp.status_code == 206:
                cr = resp.headers.get("Content-Range", "")
                if "/" in cr:
                    total_str = cr.rsplit("/", 1)[1]
                    if total_str.isdigit():
                        return int(total_str), True
                if head_length > 0:
                    return head_length, True
            length = int(resp.headers.get("Content-Length") or head_length or 0)
            return length, False

    async def _refresh_url(self, job: DownloadJob) -> str:
        lock = self._refresh_locks.setdefault(job.job_id, asyncio.Lock())
        async with lock:
            fresh = await self.api.get_fresh_download_url(
                job.device_id, job.method_id, job.update_data
            )
            if fresh:
                job.url = fresh
                return fresh
            if job.url:
                return job.url
            raise RuntimeError("No download URL available from server")

    async def _acquire_slot(self) -> None:
        async with self._slot_cond:
            while self._active_jobs >= self.max_parallel:
                await self._slot_cond.wait()
            self._active_jobs += 1

    async def _release_slot(self) -> None:
        async with self._slot_cond:
            self._active_jobs = max(0, self._active_jobs - 1)
            self._slot_cond.notify_all()

    async def _run_job(self, job: DownloadJob) -> None:
        acquired = False
        try:
            await self._acquire_slot()
            acquired = True
            if self._pause_flags.get(job.job_id) or self._cancel_flags.get(
                job.job_id
            ):
                return

            # 1. Refresh signed CDN URL
            job.status = DownloadStatus.FETCHING_URL
            self._notify(job)
            url = await self._refresh_url(job)

            # 2. Probe size & range support
            probed_size, supports_ranges = await self._probe_url(url)
            if probed_size > 0:
                job.total_bytes = probed_size
            job.supports_ranges = supports_ranges

            # 3. Resume existing state or initialize segments
            resumed = self._load_state(job)
            if not resumed:
                if (
                    job.supports_ranges
                    and job.total_bytes >= MIN_SEGMENT_SIZE
                    and job.connections > 1
                ):
                    num_conns = min(
                        job.connections, max(1, job.total_bytes // MIN_SEGMENT_SIZE)
                    )
                    seg_size = job.total_bytes // num_conns
                    segments: list[SegmentState] = []
                    for i in range(num_conns):
                        start = i * seg_size
                        end = (
                            job.total_bytes - 1
                            if i == num_conns - 1
                            else (i + 1) * seg_size - 1
                        )
                        segments.append(SegmentState(index=i, start=start, end=end))
                    job.segments = segments
                else:
                    end = max(0, job.total_bytes - 1)
                    job.segments = [SegmentState(index=0, start=0, end=end)]
                job.downloaded_bytes = 0

            # 4. Open & pre-size .part file
            fd = os.open(job.part_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                if job.total_bytes > 0:
                    os.ftruncate(fd, job.total_bytes)

                job.status = DownloadStatus.DOWNLOADING
                self._notify(job)

                # Speed / progress monitor
                monitor_stop = asyncio.Event()
                monitor_task = asyncio.create_task(
                    self._monitor_progress(job, monitor_stop)
                )

                try:
                    if job.supports_ranges and len(job.segments) > 1:
                        seg_sem = asyncio.Semaphore(job.connections)

                        async def _bounded_segment(seg: SegmentState) -> None:
                            async with seg_sem:
                                await self._download_segment(job, seg, fd)

                        await asyncio.gather(
                            *(
                                _bounded_segment(seg)
                                for seg in job.segments
                                if not seg.is_complete
                            )
                        )
                    else:
                        await self._download_single(job, job.segments[0], fd)
                finally:
                    monitor_stop.set()
                    await monitor_task
                    self._save_state(job)
            finally:
                os.close(fd)

            # 5. Move .part -> final file
            os.replace(job.part_path, job.output_path)
            job.state_path.unlink(missing_ok=True)
            job.speed_bps = 0.0
            job.eta_seconds = 0.0
            job.active_connections = 0

            # 6. Optional MD5 verification
            if job.verify_md5 and job.update_data.md5sum:
                job.status = DownloadStatus.VERIFYING
                self._notify(job)
                ok, actual = await asyncio.to_thread(
                    _verify_md5_sync, job.output_path, job.update_data.md5sum
                )
                if not ok:
                    raise RuntimeError(
                        f"MD5 mismatch (expected {job.update_data.md5sum}, got {actual})"
                    )

            job.status = DownloadStatus.COMPLETED
            self._notify(job)

        except asyncio.CancelledError:
            if self._cancel_flags.get(job.job_id):
                job.status = DownloadStatus.CANCELLED
            else:
                job.status = DownloadStatus.PAUSED
            job.speed_bps = 0.0
            job.eta_seconds = None
            job.active_connections = 0
            self._save_state(job)
            self._notify(job)
        except Exception as exc:
            job.status = DownloadStatus.FAILED
            job.error = str(exc)
            job.speed_bps = 0.0
            job.eta_seconds = None
            job.active_connections = 0
            self._save_state(job)
            self._notify(job)
        finally:
            if acquired:
                await self._release_slot()

    async def _download_segment(
        self, job: DownloadJob, seg: SegmentState, fd: int
    ) -> None:
        retries = 0
        while not seg.is_complete:
            if self._pause_flags.get(job.job_id) or self._cancel_flags.get(job.job_id):
                raise asyncio.CancelledError

            start = seg.current_offset
            end = seg.end
            headers = {"Range": f"bytes={start}-{end}"}
            url = job.url or ""

            job.active_connections += 1
            bytes_this_attempt = 0
            try:
                async with self._http.stream("GET", url, headers=headers) as resp:
                    if resp.status_code in (403, 410):
                        # Signed CDN URL likely expired; refresh and retry
                        await self._refresh_url(job)
                        retries += 1
                        if retries > MAX_RETRIES_PER_SEGMENT:
                            resp.raise_for_status()
                        await asyncio.sleep(min(1.5 * retries, 8.0))
                        continue

                    if resp.status_code not in (200, 206):
                        resp.raise_for_status()

                    async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                        if self._pause_flags.get(job.job_id) or self._cancel_flags.get(
                            job.job_id
                        ):
                            raise asyncio.CancelledError
                        if not chunk:
                            continue
                        remaining = seg.size - seg.downloaded
                        if remaining <= 0:
                            break
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        await self._global_limiter.acquire(len(chunk))
                        job_limiter = self._job_limiters.get(job.job_id)
                        if job_limiter:
                            await job_limiter.acquire(len(chunk))
                        offset = seg.current_offset
                        os.pwrite(fd, chunk, offset)
                        seg.downloaded += len(chunk)
                        job.downloaded_bytes += len(chunk)
                        bytes_this_attempt += len(chunk)
                        retries = 0

                # Stream ended cleanly before segment completed
                if not seg.is_complete and bytes_this_attempt == 0:
                    retries += 1
                    if retries > MAX_RETRIES_PER_SEGMENT:
                        raise RuntimeError("Server closed connection without sending data")
                    if retries >= 2:
                        try:
                            await self._refresh_url(job)
                        except Exception:
                            pass
                    await asyncio.sleep(min(1.5 * retries, 8.0))
            except asyncio.CancelledError:
                raise
            except Exception:
                retries += 1
                if retries > MAX_RETRIES_PER_SEGMENT:
                    raise
                if retries >= 2:
                    try:
                        await self._refresh_url(job)
                    except Exception:
                        pass
                await asyncio.sleep(min(1.5 * retries, 8.0))
            finally:
                job.active_connections = max(0, job.active_connections - 1)

    async def _download_single(
        self, job: DownloadJob, seg: SegmentState, fd: int
    ) -> None:
        """Fallback for servers that don't advertise Range support."""
        url = job.url or ""
        seg.downloaded = 0
        job.downloaded_bytes = 0
        job.active_connections += 1
        try:
            async with self._http.stream("GET", url) as resp:
                resp.raise_for_status()
                if job.total_bytes <= 0:
                    job.total_bytes = int(resp.headers.get("Content-Length") or 0)
                offset = 0
                async for chunk in resp.aiter_bytes(CHUNK_SIZE):
                    if self._pause_flags.get(job.job_id) or self._cancel_flags.get(
                        job.job_id
                    ):
                        raise asyncio.CancelledError
                    if not chunk:
                        continue
                    await self._global_limiter.acquire(len(chunk))
                    job_limiter = self._job_limiters.get(job.job_id)
                    if job_limiter:
                        await job_limiter.acquire(len(chunk))
                    os.pwrite(fd, chunk, offset)
                    offset += len(chunk)
                    seg.downloaded = offset
                    job.downloaded_bytes = offset
        finally:
            job.active_connections = max(0, job.active_connections - 1)

    async def _monitor_progress(
        self, job: DownloadJob, stop_event: asyncio.Event
    ) -> None:
        last_bytes = job.downloaded_bytes
        last_time = time.monotonic()
        last_save = last_time
        ema_speed = 0.0

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.5)
            except TimeoutError:
                pass

            now = time.monotonic()
            dt = max(now - last_time, 1e-3)
            cur_bytes = job.downloaded_bytes
            inst_speed = max(0.0, (cur_bytes - last_bytes) / dt)
            ema_speed = inst_speed if ema_speed == 0.0 else (0.3 * inst_speed + 0.7 * ema_speed)

            job.speed_bps = ema_speed
            if ema_speed > 0 and job.total_bytes > cur_bytes:
                job.eta_seconds = (job.total_bytes - cur_bytes) / ema_speed
            else:
                job.eta_seconds = None

            last_bytes = cur_bytes
            last_time = now

            if now - last_save >= STATE_SAVE_INTERVAL:
                self._save_state(job)
                last_save = now

            self._notify(job)
