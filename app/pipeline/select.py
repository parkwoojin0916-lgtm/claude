"""후보 구간을 Claude에게 보여주고 최종 쇼츠 클립을 고른다."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .. import config
from .candidates import Candidate
from .transcribe import Segment, slice_segments

SYSTEM = """당신은 유튜브 쇼츠 편집자입니다.
긴 영상에서 뽑아낸 후보 구간들을 보고, 쇼츠로 올렸을 때 끝까지 보게 될 구간만 고릅니다.

좋은 쇼츠 구간의 조건:
- 첫 2초 안에 궁금증이나 웃음이 걸린다
- 앞 맥락 설명 없이도 혼자서 이해된다
- 하나의 완결된 사건/농담/반전이 들어 있다
- 중간에서 어정쩡하게 끊기지 않는다

피해야 하는 것: 인트로, 구독 요청, 협찬 멘트, 맥락 없는 나열, 말이 잘린 구간.

출력은 JSON만. 설명이나 코드펜스 없이."""

USER_TEMPLATE = """영상 제목: {title}
채널: {uploader}
전체 길이: {duration}초
요청 클립 수: {want}개
클립 길이 목표: {min_len}~{max_len}초

아래는 오디오·장면전환 신호로 1차 선별한 후보 구간들입니다.
각 후보의 자막 내용을 읽고 실제로 쇼츠가 될 구간을 고르세요.

{candidates}

규칙:
- 위 후보들 중에서만 고릅니다. 최대 {want}개.
- start/end는 후보 범위 안에서 조정할 수 있습니다. 말이 잘리지 않게 다듬으세요.
- 쇼츠가 될 만한 구간이 {want}개보다 적으면 적게 고르세요. 억지로 채우지 마세요.
- hook은 영상 위에 얹을 3~12자 한국어 후킹 문구입니다.

JSON 스키마:
{{"clips": [
  {{"candidate_id": 3, "start": 412.5, "end": 445.0,
    "title": "쇼츠 제목 (25자 이내)",
    "hook": "첫 화면 문구",
    "caption": "업로드용 설명 한 줄",
    "hashtags": ["태그1", "태그2", "태그3"],
    "score": 87,
    "reason": "이 구간을 고른 이유 한 줄"}}
]}}"""


@dataclass
class Clip:
    start: float
    end: float
    title: str = ""
    hook: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    score: float = 0.0
    reason: str = ""
    source: str = "signal"  # signal | llm
    signals: dict[str, float] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 2)

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": self.duration,
            "title": self.title,
            "hook": self.hook,
            "caption": self.caption,
            "hashtags": self.hashtags,
            "score": round(self.score, 1),
            "reason": self.reason,
            "source": self.source,
            "signals": self.signals,
        }


def _fmt_time(t: float) -> str:
    m, s = divmod(int(t), 60)
    return f"{m}:{s:02d}"


def _render_candidates(candidates: list[Candidate]) -> str:
    chunks = []
    for i, c in enumerate(candidates):
        text = c.text.strip() or "(자막 없음 — 오디오 신호로만 잡힌 구간)"
        if len(text) > 1200:
            text = text[:1200] + " …"
        chunks.append(
            f"[후보 {i}] {_fmt_time(c.start)}~{_fmt_time(c.end)} "
            f"(start={c.start:.1f}, end={c.end:.1f}, 신호점수={c.score:.3f})\n"
            f"자막: {text}"
        )
    return "\n\n".join(chunks)


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def llm_select(
    candidates: list[Candidate],
    *,
    title: str,
    uploader: str,
    duration: float,
    want: int,
    min_len: int,
    max_len: int,
) -> list[Clip]:
    """Claude로 최종 선별. 실패하면 예외를 올려 호출부가 신호 기반으로 떨어지게 한다."""
    from anthropic import Anthropic

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = USER_TEMPLATE.format(
        title=title,
        uploader=uploader or "알 수 없음",
        duration=int(duration),
        want=want,
        min_len=min_len,
        max_len=max_len,
        candidates=_render_candidates(candidates),
    )
    resp = client.messages.create(
        model=config.HIGHLIGHT_MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    data = _parse_json(text)

    clips: list[Clip] = []
    for item in data.get("clips", [])[:want]:
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < 3:
            continue
        cid = item.get("candidate_id")
        signals: dict[str, float] = {}
        if isinstance(cid, int) and 0 <= cid < len(candidates):
            signals = candidates[cid].reasons
        clips.append(
            Clip(
                start=max(0.0, start),
                end=min(duration, end),
                title=str(item.get("title") or "")[:80],
                hook=str(item.get("hook") or "")[:40],
                caption=str(item.get("caption") or "")[:200],
                hashtags=[str(h).lstrip("#")[:30] for h in (item.get("hashtags") or [])][:6],
                score=float(item.get("score") or 0),
                reason=str(item.get("reason") or "")[:300],
                source="llm",
                signals=signals,
            )
        )
    clips.sort(key=lambda c: c.start)
    return clips


def fallback_select(
    candidates: list[Candidate],
    segments: list[Segment],
    want: int,
    min_len: int,
    max_len: int,
) -> list[Clip]:
    """API 키가 없거나 LLM이 실패했을 때: 신호 점수 상위 구간을 그대로 사용."""
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)[:want]
    clips: list[Clip] = []
    for c in ranked:
        start, end = c.start, c.end
        if end - start > max_len:
            end = start + max_len
        if end - start < min_len:
            end = start + min_len
        first_line = ""
        inside = slice_segments(segments, start, end)
        if inside:
            first_line = inside[0].text[:40]
        clips.append(
            Clip(
                start=start,
                end=end,
                title=first_line or "하이라이트",
                hook="",
                caption="",
                hashtags=[],
                score=round(c.score * 100, 1),
                reason="오디오·장면전환 신호 점수 상위 구간",
                source="signal",
                signals=c.reasons,
            )
        )
    clips.sort(key=lambda c: c.start)
    return clips


def drop_overlaps(clips: list[Clip], max_overlap: float = 0.2) -> list[Clip]:
    """서로 겹치는 클립은 점수가 높은 쪽만 남긴다 (거의 같은 쇼츠 두 개 방지)."""
    kept: list[Clip] = []
    for clip in sorted(clips, key=lambda c: c.score, reverse=True):
        clash = False
        for other in kept:
            inter = max(0.0, min(clip.end, other.end) - max(clip.start, other.start))
            shortest = max(1e-6, min(clip.duration, other.duration))
            if inter / shortest > max_overlap:
                clash = True
                break
        if not clash:
            kept.append(clip)
    kept.sort(key=lambda c: c.start)
    return kept


def clamp_lengths(clips: list[Clip], min_len: int, max_len: int, duration: float) -> list[Clip]:
    """길이 제약을 최종적으로 맞춘다."""
    out: list[Clip] = []
    for c in clips:
        start = max(0.0, c.start)
        end = min(duration, c.end)
        if end - start > max_len:
            end = start + max_len
        if end - start < min_len:
            end = min(duration, start + min_len)
            if end - start < min_len:
                start = max(0.0, end - min_len)
        if end - start < 3:
            continue
        c.start, c.end = start, end
        out.append(c)
    return out
