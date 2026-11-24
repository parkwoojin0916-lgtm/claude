"""작업 큐: 백그라운드 스레드에서 파이프라인을 돌리고 진행률을 추적한다."""
from __future__ import annotations

import json
import shutil
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .pipeline import candidates as cand_mod
from .pipeline import download, render, select, signals, transcribe

STAGES = {
    "queued": "대기 중",
    "probing": "영상 정보 확인",
    "downloading": "영상 다운로드",
    "transcribing": "음성 전사",
    "analyzing": "하이라이트 신호 분석",
    "selecting": "클립 선별",
    "rendering": "쇼츠 렌더링",
    "done": "완료",
    "failed": "실패",
    "canceled": "취소됨",
}


@dataclass
class JobOptions:
    aspect: str = "9:16"
    crop_mode: str = "crop"
    clip_count: int = 3
    min_seconds: int = 20
    max_seconds: int = 60
    burn_subs: bool = True
    show_hook: bool = True
    keep_source: bool = False


@dataclass
class Job:
    id: str
    url: str
    options: JobOptions
    stage: str = "queued"
    progress: float = 0.0
    message: str = "대기 중"
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    video: dict[str, Any] = field(default_factory=dict)
    clips: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    canceled: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "options": asdict(self.options),
            "stage": self.stage,
            "stage_label": STAGES.get(self.stage, self.stage),
            "progress": round(self.progress, 3),
            "message": self.message,
            "created_at": self.created_at,
            "video": self.video,
            "clips": self.clips,
            "diagnostics": self.diagnostics,
            "error": self.error,
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max(1, config.MAX_WORKERS))

    def create(self, url: str, options: JobOptions) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(id=job_id, url=url, options=options)
        with self._lock:
            self._jobs[job_id] = job
        self._pool.submit(self._run_guarded, job_id)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 30) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.stage in {"done", "failed", "canceled"}:
            return False
        job.canceled = True
        return True

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if not job:
            return False
        shutil.rmtree(config.JOBS_DIR / job_id, ignore_errors=True)
        return True

    # ---- 파이프라인 ----

    def _update(
        self, job: Job, stage: str | None = None, progress: float | None = None,
        message: str | None = None,
    ) -> None:
        if stage:
            job.stage = stage
            job.message = message or STAGES.get(stage, stage)
        if message:
            job.message = message
        if progress is not None:
            job.progress = max(0.0, min(1.0, progress))

    def _check_cancel(self, job: Job) -> None:
        if job.canceled:
            raise Canceled()

    def _run_guarded(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        try:
            self._run(job)
        except Canceled:
            self._update(job, "canceled", job.progress, "사용자가 취소했습니다.")
        except download.DownloadError as exc:
            job.error = str(exc)
            self._update(job, "failed", job.progress, str(exc))
        except Exception as exc:  # noqa: BLE001
            job.error = f"{type(exc).__name__}: {exc}"
            job.diagnostics["traceback"] = traceback.format_exc()[-3000:]
            self._update(job, "failed", job.progress, job.error)

    def _run(self, job: Job) -> None:
        opts = job.options
        workdir = config.JOBS_DIR / job.id
        clips_dir = workdir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)

        download.ensure_tools()

        # 1) 메타데이터
        self._update(job, "probing", 0.02)
        url = download.normalize_url(job.url)
        meta = download.probe(url)
        if meta.duration <= 0:
            raise download.DownloadError("라이브 스트림이나 길이를 알 수 없는 영상은 지원하지 않습니다.")
        if meta.duration > config.MAX_VIDEO_SECONDS:
            raise download.DownloadError(
                f"영상이 너무 깁니다 ({int(meta.duration // 60)}분). "
                f"최대 {config.MAX_VIDEO_SECONDS // 60}분까지 처리합니다."
            )
        job.video = {
            "id": meta.video_id,
            "title": meta.title,
            "uploader": meta.uploader,
            "duration": meta.duration,
            "thumbnail": meta.thumbnail,
            "url": url,
        }
        self._check_cancel(job)

        # 2) 다운로드
        self._update(job, "downloading", 0.08)
        source = download.download_video(url, workdir)
        src_size = download.video_dimensions(source)
        wav = download.extract_audio(source, workdir / "audio.wav")
        self._check_cancel(job)
        self._update(job, "downloading", 0.30, "다운로드 완료")

        # 3) 전사 — 유튜브 자막 우선
        self._update(job, "transcribing", 0.32)
        segments: list[transcribe.Segment] = []
        transcript_source = "none"
        vtt = download.download_subtitles(url, workdir)
        if vtt:
            segments = transcribe.parse_vtt(vtt)
            if segments:
                transcript_source = f"youtube:{vtt.name}"
        if not segments:
            self._update(job, "transcribing", 0.34, "유튜브 자막 없음 → Whisper로 전사")

            def on_progress(ratio: float) -> None:
                self._check_cancel(job)
                self._update(job, progress=0.34 + ratio * 0.26)

            segments, lang = transcribe.whisper_transcribe(wav, on_progress=on_progress)
            transcript_source = f"whisper:{config.WHISPER_MODEL}:{lang}"
        job.diagnostics["transcript_source"] = transcript_source
        job.diagnostics["segment_count"] = len(segments)
        self._check_cancel(job)

        # 4) 신호 분석
        self._update(job, "analyzing", 0.62)
        sig = signals.collect(source, wav, meta.duration)
        job.diagnostics["scene_cut_count"] = len(sig.scene_cuts)
        self._check_cancel(job)

        target_len = (opts.min_seconds + opts.max_seconds) / 2
        cands = cand_mod.find_candidates(
            sig,
            segments,
            target_seconds=target_len,
            max_candidates=max(8, opts.clip_count * 4),
        )
        job.diagnostics["candidate_count"] = len(cands)
        if not cands:
            raise download.DownloadError("하이라이트로 쓸 구간을 찾지 못했습니다.")

        # 5) 최종 선별
        self._update(job, "selecting", 0.70)
        chosen: list[select.Clip] = []
        if config.llm_enabled():
            try:
                chosen = select.llm_select(
                    cands,
                    title=meta.title,
                    uploader=meta.uploader,
                    duration=meta.duration,
                    want=opts.clip_count,
                    min_len=opts.min_seconds,
                    max_len=opts.max_seconds,
                )
                job.diagnostics["selection"] = "llm"
            except Exception as exc:  # noqa: BLE001
                job.diagnostics["llm_error"] = f"{type(exc).__name__}: {exc}"
        if not chosen:
            chosen = select.fallback_select(
                cands, segments, opts.clip_count, opts.min_seconds, opts.max_seconds
            )
            job.diagnostics.setdefault("selection", "signal")
        chosen = select.clamp_lengths(
            chosen, opts.min_seconds, opts.max_seconds, meta.duration
        )
        chosen = select.drop_overlaps(chosen)[: opts.clip_count]
        if not chosen:
            raise download.DownloadError("쓸 만한 하이라이트 구간이 없습니다.")
        self._check_cancel(job)

        # 6) 렌더링
        self._update(job, "rendering", 0.76)
        results: list[dict[str, Any]] = []
        total = max(1, len(chosen))
        for i, clip in enumerate(chosen, start=1):
            self._check_cancel(job)
            self._update(
                job,
                progress=0.76 + (i - 1) / total * 0.22,
                message=f"쇼츠 렌더링 {i}/{total}",
            )
            out_name = f"clip_{i:02d}.mp4"
            out_path = clips_dir / out_name
            render.render_clip(
                source,
                out_path,
                clip.start,
                clip.end,
                aspect=opts.aspect,
                crop_mode=opts.crop_mode,
                segments=segments,
                burn_subs=opts.burn_subs,
                hook=clip.hook if opts.show_hook else "",
                src_size=src_size,
                workdir=workdir,
            )
            thumb = render.make_thumbnail(out_path, clips_dir / f"clip_{i:02d}.jpg")
            info = clip.to_dict()
            info.update(
                {
                    "index": i,
                    "file": out_name,
                    "size_bytes": out_path.stat().st_size,
                    "download_url": f"/api/jobs/{job.id}/clips/{out_name}",
                    "thumbnail_url": (
                        f"/api/jobs/{job.id}/clips/{thumb.name}" if thumb else None
                    ),
                    "youtube_url": f"{job.video['url']}&t={int(clip.start)}s"
                    if "watch?v=" in job.video["url"]
                    else job.video["url"],
                }
            )
            results.append(info)
            job.clips = results

        # 7) 정리
        if not opts.keep_source:
            source.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
            for leftover in workdir.glob("subs*.vtt"):
                leftover.unlink(missing_ok=True)
        for ass in workdir.glob("*.ass"):
            ass.unlink(missing_ok=True)

        (workdir / "result.json").write_text(
            json.dumps(job.public(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._update(job, "done", 1.0, f"쇼츠 {len(results)}개 완성")

    def clip_path(self, job_id: str, filename: str) -> Path | None:
        if "/" in filename or ".." in filename:
            return None
        path = config.JOBS_DIR / job_id / "clips" / filename
        return path if path.is_file() else None


class Canceled(Exception):
    pass


store = JobStore()
