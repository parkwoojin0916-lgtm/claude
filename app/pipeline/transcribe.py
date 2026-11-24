"""전사: 유튜브 자막 우선, 없으면 faster-whisper."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from .. import config


@dataclass
class Segment:
    start: float
    end: float
    text: str


_TS = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})"
)
_TAG = re.compile(r"<[^>]+>")


def _secs(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_vtt(path: Path) -> list[Segment]:
    """VTT를 Segment 목록으로. 자동 자막의 중복 줄은 제거한다."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    segments: list[Segment] = []
    current: tuple[float, float] | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, buffer
        if current and buffer:
            text = " ".join(buffer).strip()
            if text:
                segments.append(Segment(current[0], current[1], text))
        current, buffer = None, []

    for line in raw.splitlines():
        m = _TS.search(line)
        if m:
            flush()
            current = (_secs(*m.groups()[:4]), _secs(*m.groups()[4:]))
            continue
        if current is None:
            continue
        clean = html.unescape(_TAG.sub("", line)).strip()
        if clean and clean not in buffer:
            buffer.append(clean)
    flush()

    return _dedupe(segments)


def _dedupe(segments: list[Segment]) -> list[Segment]:
    """자동 자막은 앞 문장을 반복해서 내보낸다. 직전 텍스트에 포함되면 버린다."""
    out: list[Segment] = []
    for seg in segments:
        if out:
            prev = out[-1]
            if seg.text == prev.text:
                prev.end = max(prev.end, seg.end)
                continue
            if seg.text in prev.text:
                continue
            # 자동 자막은 같은 문장을 조금씩 늘려가며 반복한다.
            # 바로 이어지는 큐가 앞 문장을 포함하면 더 긴 쪽으로 갱신한다.
            if prev.text in seg.text and seg.start <= prev.end + 0.5:
                prev.text = seg.text
                prev.end = max(prev.end, seg.end)
                continue
        out.append(seg)
    return out


def _read_wav_mono16k(path: Path):
    """16kHz 모노 wav를 numpy float32 배열로 읽는다.

    faster-whisper에 경로를 넘기면 PyAV로 디코딩하는데, PyAV 버전에 따라
    깨지는 경우가 있다. 어차피 ffmpeg로 규격을 맞춰 뽑아둔 파일이므로
    직접 읽어서 배열로 넘긴다.
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as wf:
        if wf.getframerate() != 16000 or wf.getnchannels() != 1:
            raise ValueError("16kHz 모노 wav가 아닙니다.")
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        raise ValueError(f"지원하지 않는 샘플 폭: {width}바이트")
    data = np.frombuffer(raw, dtype=dtype)
    if dtype is np.uint8:
        return ((data.astype(np.float32) - 128.0) / 128.0)
    return data.astype(np.float32) / float(np.iinfo(dtype).max)


def whisper_transcribe(
    wav_path: Path, model_size: str | None = None, on_progress=None
) -> tuple[list[Segment], str]:
    """faster-whisper 전사. (세그먼트, 감지 언어) 반환."""
    from faster_whisper import WhisperModel

    size = model_size or config.WHISPER_MODEL
    model = WhisperModel(size, device="cpu", compute_type="int8")

    try:
        audio = _read_wav_mono16k(wav_path)
    except Exception:  # noqa: BLE001 — 읽기 실패 시 경로를 그대로 넘긴다
        audio = str(wav_path)

    raw_segments, info = model.transcribe(
        audio, vad_filter=True, beam_size=1, condition_on_previous_text=False
    )

    segments: list[Segment] = []
    total = float(getattr(info, "duration", 0) or 0)
    for seg in raw_segments:
        text = (seg.text or "").strip()
        if text:
            segments.append(Segment(float(seg.start), float(seg.end), text))
        if on_progress and total:
            on_progress(min(1.0, float(seg.end) / total))
    return segments, getattr(info, "language", "") or ""


def transcript_text(segments: list[Segment]) -> str:
    return " ".join(s.text for s in segments)


def slice_segments(segments: list[Segment], start: float, end: float) -> list[Segment]:
    """구간과 겹치는 세그먼트만."""
    return [s for s in segments if s.end > start and s.start < end]
