"""유튜브 → 쇼츠 클립 서비스 API."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, field_validator

from . import config
from .jobs import JobOptions, store
from .pipeline import download

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Shorts Cutter", version="0.1.0")


class CreateJobRequest(BaseModel):
    url: str
    aspect: str = "9:16"
    crop_mode: str = "crop"
    clip_count: int = Field(default=3, ge=1, le=8)
    min_seconds: int = Field(default=20, ge=5, le=120)
    max_seconds: int = Field(default=60, ge=10, le=180)
    burn_subs: bool = True
    show_hook: bool = True
    keep_source: bool = False
    prompt: str = Field(default="", max_length=300)

    @field_validator("aspect")
    @classmethod
    def _check_aspect(cls, v: str) -> str:
        if v not in config.ASPECT_RATIOS:
            raise ValueError(
                f"지원하지 않는 비율입니다. 가능: {', '.join(config.ASPECT_RATIOS)}"
            )
        return v

    @field_validator("crop_mode")
    @classmethod
    def _check_crop(cls, v: str) -> str:
        if v not in config.CROP_MODES:
            raise ValueError(f"crop_mode는 {config.CROP_MODES} 중 하나여야 합니다.")
        return v

    def to_options(self) -> JobOptions:
        min_s, max_s = self.min_seconds, self.max_seconds
        if min_s > max_s:
            min_s, max_s = max_s, min_s
        return JobOptions(
            aspect=self.aspect,
            crop_mode=self.crop_mode,
            clip_count=self.clip_count,
            min_seconds=min_s,
            max_seconds=max_s,
            burn_subs=self.burn_subs,
            show_hook=self.show_hook,
            keep_source=self.keep_source,
            prompt=self.prompt.strip(),
        )


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/static/{filename}")
def static_file(filename: str) -> FileResponse:
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="잘못된 경로입니다.")
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="없는 파일입니다.")
    return FileResponse(path)


@app.get("/api/config")
def get_config() -> dict:
    return {
        "aspect_ratios": list(config.ASPECT_RATIOS),
        "crop_modes": list(config.CROP_MODES),
        "llm_enabled": config.llm_enabled(),
        "highlight_model": config.HIGHLIGHT_MODEL if config.llm_enabled() else None,
        "whisper_model": config.WHISPER_MODEL,
        "max_video_seconds": config.MAX_VIDEO_SECONDS,
    }


@app.post("/api/preview")
def preview(req: CreateJobRequest) -> dict:
    """다운로드 전에 영상 정보만 확인."""
    try:
        meta = download.probe(download.normalize_url(req.url))
    except download.DownloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "id": meta.video_id,
        "title": meta.title,
        "uploader": meta.uploader,
        "duration": meta.duration,
        "thumbnail": meta.thumbnail,
        "has_subtitles": bool(meta.subtitle_langs),
        "subtitle_langs": meta.subtitle_langs[:12],
        "too_long": meta.duration > config.MAX_VIDEO_SECONDS,
    }


@app.post("/api/jobs", status_code=202)
def create_job(req: CreateJobRequest) -> dict:
    try:
        url = download.normalize_url(req.url)
    except download.DownloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = store.create(url, req.to_options())
    return job.public()


@app.get("/api/jobs")
def list_jobs(limit: int = 30) -> dict:
    return {"jobs": [j.public() for j in store.list(limit=limit)]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="없는 작업입니다.")
    return job.public()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    if not store.cancel(job_id):
        raise HTTPException(status_code=409, detail="취소할 수 없는 작업입니다.")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    if not store.delete(job_id):
        raise HTTPException(status_code=404, detail="없는 작업입니다.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/clips/{filename}")
def get_clip(job_id: str, filename: str):
    path = store.clip_path(job_id, filename)
    if not path:
        raise HTTPException(status_code=404, detail="없는 파일입니다.")
    media = "video/mp4" if path.suffix == ".mp4" else "image/jpeg"
    return FileResponse(path, media_type=media, filename=path.name)


@app.get("/api/health")
def health() -> dict:
    try:
        download.ensure_tools()
        tools_ok = True
        tools_error = None
    except download.DownloadError as exc:
        tools_ok = False
        tools_error = str(exc)
    return {"ok": tools_ok, "tools_error": tools_error, "llm": config.llm_enabled()}
