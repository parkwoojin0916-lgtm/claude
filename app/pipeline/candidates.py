"""신호 기반 1차 후보 구간 추출."""
from __future__ import annotations

from dataclasses import dataclass, field

from .signals import Signals
from .transcribe import Segment, slice_segments

# 웃음·감탄·리액션이 자막에 드러나는 단서
LAUGH_HINTS = (
    "ㅋㅋ", "ㅎㅎ", "하하", "웃음", "폭소", "빵 터", "빵터", "미쳤", "대박", "진짜?",
    "헐", "와", "레전드", "소름", "충격", "어이가", "말도 안", "아니 이게",
    "[laughter]", "[laughs]", "laughing", "haha", "lol", "wow", "oh my god",
    "insane", "crazy", "unbelievable", "no way", "what the",
)
QUESTION_HINTS = ("?", "왜", "어떻게", "뭐야", "진짜", "why", "how", "what if")


@dataclass
class Candidate:
    start: float
    end: float
    score: float
    reasons: dict[str, float] = field(default_factory=dict)
    text: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


def _normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-6:
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _window_scores(
    signals: Signals, segments: list[Segment]
) -> tuple[list[float], list[float], list[float], list[float]]:
    """윈도우별 (음량, 음량급변, 장면전환밀도, 발화밀도) 정규화 점수."""
    n = len(signals.loudness)
    w = signals.window

    loud = _normalize([max(v, -70.0) for v in signals.loudness])

    burst = [0.0] * n
    for i in range(1, n):
        burst[i] = max(0.0, signals.loudness[i] - signals.loudness[i - 1])
    burst = _normalize(burst)

    cuts = [0.0] * n
    for t in signals.scene_cuts:
        idx = int(t / w)
        if 0 <= idx < n:
            cuts[idx] += 1.0
    cuts = _normalize(cuts)

    speech = [0.0] * n
    for seg in segments:
        chars = len(seg.text)
        span = max(seg.end - seg.start, 0.1)
        density = chars / span
        i0, i1 = int(seg.start / w), min(n - 1, int(seg.end / w))
        for i in range(max(0, i0), i1 + 1):
            speech[i] = max(speech[i], density)
    speech = _normalize(speech)

    return loud, burst, cuts, speech


def _text_bonus(text: str) -> tuple[float, dict[str, float]]:
    low = text.lower()
    laugh = sum(1 for k in LAUGH_HINTS if k in low or k in text)
    question = sum(1 for k in QUESTION_HINTS if k in low or k in text)
    bonus = min(1.0, laugh * 0.25) * 0.8 + min(1.0, question * 0.15) * 0.3
    return bonus, {"laugh_hits": float(laugh), "question_hits": float(question)}


def _snap_to_speech(
    start: float, end: float, segments: list[Segment], duration: float
) -> tuple[float, float]:
    """말이 잘리지 않게 문장 경계로 당긴다."""
    inside = slice_segments(segments, start, end)
    if not inside:
        return max(0.0, start), min(duration, end)

    first, last = inside[0], inside[-1]
    # 시작은 걸친 문장의 처음까지 최대 3초만 앞으로
    new_start = first.start if start - first.start <= 3.0 else start
    # 끝은 걸친 문장이 끝날 때까지 최대 4초만 뒤로
    new_end = last.end if last.end - end <= 4.0 else end
    return max(0.0, new_start), min(duration, new_end)


def find_candidates(
    signals: Signals,
    segments: list[Segment],
    target_seconds: float = 32.0,
    max_candidates: int = 14,
    step: float = 2.0,
) -> list[Candidate]:
    """슬라이딩 윈도우로 점수를 매겨 서로 겹치지 않는 후보를 돌려준다."""
    duration = signals.duration
    if duration <= target_seconds * 1.2:
        start, end = _snap_to_speech(0.0, min(duration, target_seconds), segments, duration)
        text = " ".join(s.text for s in slice_segments(segments, start, end))
        return [Candidate(start, end, 1.0, {"short_video": 1.0}, text)]

    loud, burst, cuts, speech = _window_scores(signals, segments)
    n = len(loud)
    w = signals.window
    if n == 0:
        return []

    win = max(1, int(target_seconds / w))
    scored: list[Candidate] = []
    stride = max(1, int(step / w))

    # 도입부/엔딩은 인트로·구독요청이 많아 약하게 감점
    for i in range(0, max(1, n - win), stride):
        sl = slice(i, i + win)
        s_loud = sum(loud[sl]) / win
        s_burst = sum(burst[sl]) / win
        s_cuts = sum(cuts[sl]) / win
        s_speech = sum(speech[sl]) / win

        start_t, end_t = i * w, (i + win) * w
        text = " ".join(s.text for s in slice_segments(segments, start_t, end_t))
        bonus, hits = _text_bonus(text)

        base = (
            s_loud * 0.22
            + s_burst * 0.30
            + s_cuts * 0.18
            + s_speech * 0.20
            + bonus * 0.30
        )
        position_penalty = 0.0
        if end_t < 25:
            position_penalty = 0.12
        elif start_t > duration - 20:
            position_penalty = 0.10

        reasons = {
            "loudness": round(s_loud, 3),
            "loudness_burst": round(s_burst, 3),
            "scene_cuts": round(s_cuts, 3),
            "speech_density": round(s_speech, 3),
            "text_bonus": round(bonus, 3),
            **hits,
        }
        scored.append(
            Candidate(start_t, end_t, round(base - position_penalty, 4), reasons, text)
        )

    scored.sort(key=lambda c: c.score, reverse=True)

    picked: list[Candidate] = []
    min_len = max(5.0, target_seconds * 0.5)
    max_len = target_seconds * 1.8
    for cand in scored:
        if len(picked) >= max_candidates:
            break
        start, end = _snap_to_speech(cand.start, cand.end, segments, duration)
        # 스냅 때문에 너무 짧아졌으면 목표 길이까지 되돌린다
        if end - start < min_len:
            end = min(duration, start + target_seconds)
            if end - start < min_len:
                start = max(0.0, end - target_seconds)
        if end - start > max_len:
            end = start + max_len
        cand.start, cand.end = start, end
        # 스냅된 좌표로 겹침을 판정한다
        if any(_overlap(cand, p) > 0.35 for p in picked):
            continue
        cand.text = " ".join(s.text for s in slice_segments(segments, start, end))
        picked.append(cand)

    picked.sort(key=lambda c: c.start)
    return picked


def _overlap(a: Candidate, b: Candidate) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    shortest = max(1e-6, min(a.duration, b.duration))
    return inter / shortest
