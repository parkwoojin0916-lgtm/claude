"""ffmpeg로 뽑아내는 저수준 신호: 음량 변화, 장면 전환."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

WINDOW_SECONDS = 0.5

_RMS_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-inf)")
_PTS_RE = re.compile(r"pts_time:([\d.]+)")
_SHOWINFO_PTS_RE = re.compile(r"pts_time:([\d.]+)")


@dataclass
class Signals:
    duration: float
    window: float
    # 윈도우별 RMS 음량(dB). 무음은 -90.
    loudness: list[float]
    # 장면 전환 시각(초)
    scene_cuts: list[float]

    def loudness_at(self, t: float) -> float:
        idx = int(t / self.window)
        if 0 <= idx < len(self.loudness):
            return self.loudness[idx]
        return -90.0


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def loudness_series(wav_path: Path, window: float = WINDOW_SECONDS) -> list[float]:
    """0.5초 단위 RMS 음량(dB) 시계열."""
    nsamples = max(1, int(16000 * window))
    proc = _run(
        [
            "ffmpeg",
            "-nostats",
            "-i",
            str(wav_path),
            "-af",
            f"asetnsamples=n={nsamples}:p=0,"
            "astats=metadata=1:reset=1,"
            "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
            "-f",
            "null",
            "-",
        ],
        timeout=1800,
    )
    values: list[float] = []
    for line in proc.stdout.splitlines():
        m = _RMS_RE.search(line)
        if not m:
            continue
        raw = m.group(1)
        values.append(-90.0 if raw == "-inf" else float(raw))
    return values


def scene_cuts(video_path: Path, duration: float, threshold: float = 0.35) -> list[float]:
    """장면 전환 시각. 긴 영상은 저해상도·저fps로 샘플링해 비용을 줄인다."""
    fps = 3 if duration <= 2400 else 2
    proc = _run(
        [
            "ffmpeg",
            "-nostats",
            "-loglevel",
            "info",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps},scale=160:-2,select='gt(scene,{threshold})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ],
        timeout=3600,
    )
    cuts: list[float] = []
    for line in proc.stderr.splitlines():
        if "showinfo" not in line:
            continue
        m = _SHOWINFO_PTS_RE.search(line)
        if m:
            cuts.append(float(m.group(1)))
    return cuts


def collect(video_path: Path, wav_path: Path, duration: float) -> Signals:
    loud = loudness_series(wav_path)
    try:
        cuts = scene_cuts(video_path, duration)
    except subprocess.TimeoutExpired:
        cuts = []
    return Signals(
        duration=duration, window=WINDOW_SECONDS, loudness=loud, scene_cuts=cuts
    )
