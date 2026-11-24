"""환경 설정 로딩."""
from __future__ import annotations

import os
import shlex
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv 없이도 동작
    pass


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
HIGHLIGHT_MODEL = os.environ.get("HIGHLIGHT_MODEL", "claude-sonnet-5-5").strip()
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base").strip()

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"

# yt-dlp 인증: 클라우드 IP는 유튜브가 봇으로 차단하는 경우가 많다.
# 브라우저에서 내보낸 cookies.txt 경로를 지정하면 그 쿠키로 접근한다.
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
# 로컬 실행이라면 브라우저에서 바로 읽을 수도 있다: "chrome", "firefox" 등
YTDLP_COOKIES_FROM_BROWSER = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
# 그 밖의 yt-dlp 인자를 공백으로 구분해 넘긴다.
YTDLP_EXTRA_ARGS = os.environ.get("YTDLP_EXTRA_ARGS", "").strip()

MAX_WORKERS = _int("MAX_WORKERS", 2)
MAX_VIDEO_SECONDS = _int("MAX_VIDEO_SECONDS", 5400)

# 클립 길이 범위(초)
MIN_CLIP_SECONDS = 12
MAX_CLIP_SECONDS = 75

# 지원 비율: 이름 -> (가로, 세로)
ASPECT_RATIOS: dict[str, tuple[int, int] | None] = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
    "original": None,
}

CROP_MODES = ("auto", "crop", "blur")


def ytdlp_auth_args() -> list[str]:
    """yt-dlp 공통 인증/추가 인자."""
    args: list[str] = []
    if YTDLP_COOKIES_FILE:
        args += ["--cookies", YTDLP_COOKIES_FILE]
    elif YTDLP_COOKIES_FROM_BROWSER:
        args += ["--cookies-from-browser", YTDLP_COOKIES_FROM_BROWSER]
    if YTDLP_EXTRA_ARGS:
        args += shlex.split(YTDLP_EXTRA_ARGS)
    return args


def llm_enabled() -> bool:
    return bool(ANTHROPIC_API_KEY)


JOBS_DIR.mkdir(parents=True, exist_ok=True)
