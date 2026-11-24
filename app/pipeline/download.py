"""yt-dlp로 영상 메타데이터/영상/오디오/자막을 가져온다."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?"
    r"(youtube\.com/(watch\?|shorts/|live/|embed/)|youtu\.be/)",
    re.IGNORECASE,
)


class DownloadError(RuntimeError):
    pass


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise DownloadError("URL이 비어 있습니다.")
    if not YOUTUBE_RE.match(url):
        raise DownloadError("유튜브 영상 URL만 지원합니다.")
    if not url.startswith("http"):
        url = "https://" + url
    return url


@dataclass
class VideoMeta:
    video_id: str
    title: str
    duration: float
    uploader: str = ""
    thumbnail: str = ""
    description: str = ""
    # 업로더가 올린 자막(있으면 전사 생략 가능)
    subtitle_langs: list[str] = field(default_factory=list)


def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def probe(url: str) -> VideoMeta:
    """다운로드 없이 메타데이터만 읽는다."""
    proc = _run(
        ["yt-dlp", "--no-warnings", *config.ytdlp_auth_args(),
         "--skip-download", "--dump-single-json", url],
        timeout=180,
    )
    if proc.returncode != 0:
        raise DownloadError(_friendly_error(proc.stderr))
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"메타데이터를 읽지 못했습니다: {exc}") from exc

    if info.get("_type") == "playlist":
        raise DownloadError("플레이리스트가 아닌 개별 영상 URL을 넣어주세요.")

    subs = sorted(
        set(list(info.get("subtitles") or {}) + list(info.get("automatic_captions") or {}))
    )
    return VideoMeta(
        video_id=info.get("id") or "",
        title=info.get("title") or "제목 없음",
        duration=float(info.get("duration") or 0),
        uploader=info.get("uploader") or info.get("channel") or "",
        thumbnail=info.get("thumbnail") or "",
        description=(info.get("description") or "")[:4000],
        subtitle_langs=subs,
    )


def download_video(url: str, workdir: Path) -> Path:
    """1080p 이하 mp4로 받는다."""
    workdir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(workdir / "source.%(ext)s")
    proc = _run(
        [
            "yt-dlp",
            "--no-warnings",
            *config.ytdlp_auth_args(),
            "--no-playlist",
            "-f",
            "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
            "--merge-output-format",
            "mp4",
            "-o",
            out_tpl,
            url,
        ]
    )
    if proc.returncode != 0:
        raise DownloadError(_friendly_error(proc.stderr))

    for candidate in sorted(workdir.glob("source.*")):
        if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            return candidate
    raise DownloadError("영상 파일을 찾지 못했습니다.")


def download_subtitles(url: str, workdir: Path, langs: str = "ko,en") -> Path | None:
    """업로더 자막 또는 자동 생성 자막을 vtt로 받는다. 없으면 None."""
    workdir.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            "yt-dlp",
            "--no-warnings",
            *config.ytdlp_auth_args(),
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            langs,
            "--sub-format",
            "vtt",
            "-o",
            str(workdir / "subs.%(ext)s"),
            url,
        ],
        timeout=300,
    )
    if proc.returncode != 0:
        return None
    files = sorted(workdir.glob("subs*.vtt"))
    return files[0] if files else None


def extract_audio(video_path: Path, out_path: Path) -> Path:
    """16kHz 모노 wav로 변환 (전사·신호 분석용)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(out_path),
        ]
    )
    if proc.returncode != 0:
        raise DownloadError(f"오디오 추출 실패: {proc.stderr[-400:]}")
    return out_path


def video_dimensions(video_path: Path) -> tuple[int, int]:
    proc = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(video_path),
        ],
        timeout=120,
    )
    try:
        w, h = (int(x) for x in proc.stdout.strip().split(",")[:2])
        return w, h
    except (ValueError, IndexError):
        return 1920, 1080


def _friendly_error(stderr: str) -> str:
    text = (stderr or "").strip()
    low = text.lower()
    if "private video" in low:
        return "비공개 영상입니다."
    if "members-only" in low:
        return "멤버십 전용 영상입니다."
    if "age" in low and "confirm" in low:
        return "연령 제한 영상이라 받을 수 없습니다."
    if "video unavailable" in low or "not available" in low:
        return "영상을 볼 수 없습니다 (삭제되었거나 지역 제한)."
    if "sign in to confirm" in low or "not a bot" in low:
        return (
            "유튜브가 이 서버를 봇으로 보고 차단했습니다. "
            "브라우저에서 내보낸 cookies.txt를 YTDLP_COOKIES_FILE 환경변수로 지정하거나, "
            "로컬에서 YTDLP_COOKIES_FROM_BROWSER=chrome 으로 실행하세요."
        )
    tail = text.splitlines()[-1] if text else "알 수 없는 오류"
    return f"다운로드 실패: {tail[:300]}"


def ensure_tools() -> None:
    for tool in ("yt-dlp", "ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise DownloadError(f"{tool} 가 설치되어 있지 않습니다.")
