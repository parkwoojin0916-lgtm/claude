"""API 표면과 LLM 응답 파싱 테스트 (네트워크/API 키 불필요)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.main import app  # noqa: E402
from app.pipeline import select  # noqa: E402
from app.pipeline.candidates import Candidate  # noqa: E402

client = TestClient(app)
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(name)


print("API 라우트")
r = client.get("/")
check("GET / 가 HTML을 돌려준다", r.status_code == 200 and "쇼츠 커터" in r.text)

r = client.get("/api/config")
check("GET /api/config", r.status_code == 200 and "9:16" in r.json()["aspect_ratios"])

r = client.get("/api/health")
check("GET /api/health", r.status_code == 200 and r.json()["ok"] is True)

r = client.get("/api/jobs")
check("GET /api/jobs", r.status_code == 200 and "jobs" in r.json())

print("\n입력 검증")
r = client.post("/api/jobs", json={"url": "https://example.com/video"})
check("유튜브가 아닌 URL은 400", r.status_code == 400, str(r.status_code))

r = client.post("/api/jobs", json={"url": ""})
check("빈 URL은 400", r.status_code == 400, str(r.status_code))

r = client.post("/api/jobs", json={"url": "https://youtu.be/x", "aspect": "3:7"})
check("잘못된 비율은 422", r.status_code == 422, str(r.status_code))

r = client.post("/api/jobs", json={"url": "https://youtu.be/x", "crop_mode": "zoom"})
check("잘못된 crop_mode는 422", r.status_code == 422, str(r.status_code))

r = client.post("/api/jobs", json={"url": "https://youtu.be/x", "clip_count": 99})
check("clip_count 범위 초과는 422", r.status_code == 422, str(r.status_code))

r = client.get("/api/jobs/없는작업")
check("없는 작업은 404", r.status_code == 404, str(r.status_code))

r = client.get("/api/jobs/abc/clips/..%2F..%2Fetc%2Fpasswd")
check("경로 탈출 시도 차단", r.status_code in (400, 404), str(r.status_code))

r = client.get("/static/../config.py")
check("정적 파일 경로 탈출 차단", r.status_code in (400, 404), str(r.status_code))

print("\n옵션 정규화")
from app.main import CreateJobRequest  # noqa: E402

opts = CreateJobRequest(url="https://youtu.be/x", min_seconds=60, max_seconds=20).to_options()
check("min/max가 뒤집혀 들어오면 교정", opts.min_seconds == 20 and opts.max_seconds == 60,
      f"{opts.min_seconds}/{opts.max_seconds}")

print("\nLLM 응답 파싱")
cands = [Candidate(10.0, 40.0, 0.8, {"loudness": 0.9}, "재밌는 부분"),
         Candidate(100.0, 130.0, 0.6, {"loudness": 0.5}, "다른 부분")]

FAKE = '''```json
{"clips": [
  {"candidate_id": 0, "start": 12.0, "end": 38.0, "title": "레전드 장면",
   "hook": "이거 실화?", "caption": "설명 한 줄", "hashtags": ["#쇼츠", "웃긴영상"],
   "score": 91, "reason": "반전이 명확함"}
]}
```'''


class FakeBlock:
    type = "text"
    text = FAKE


class FakeResp:
    content = [FakeBlock()]


class FakeMessages:
    def create(self, **kwargs):
        return FakeResp()


class FakeClient:
    def __init__(self, **kwargs):
        self.messages = FakeMessages()


with patch("anthropic.Anthropic", FakeClient), patch.object(config, "ANTHROPIC_API_KEY", "test"):
    clips = select.llm_select(cands, title="t", uploader="u", duration=200,
                              want=3, min_len=20, max_len=60)

check("코드펜스가 붙은 JSON을 파싱한다", len(clips) == 1, f"{len(clips)}개")
if clips:
    c = clips[0]
    check("start/end 반영", c.start == 12.0 and c.end == 38.0)
    check("제목/후킹/캡션 반영", c.title == "레전드 장면" and c.hook == "이거 실화?")
    check("해시태그의 # 제거", c.hashtags == ["쇼츠", "웃긴영상"], str(c.hashtags))
    check("source=llm", c.source == "llm")
    check("후보의 신호 점수 연결", c.signals.get("loudness") == 0.9)

print("\n길이 제약 / 겹침 제거")
long_clip = select.Clip(start=0, end=200, score=50)
out = select.clamp_lengths([long_clip], 20, 60, 300)
check("최대 길이로 자른다", out[0].duration == 60, f"{out[0].duration}s")

short_clip = select.Clip(start=10, end=12, score=50)
out = select.clamp_lengths([short_clip], 20, 60, 300)
check("최소 길이까지 늘린다", out[0].duration == 20, f"{out[0].duration}s")

overlapping = [select.Clip(start=0, end=30, score=90),
               select.Clip(start=20, end=50, score=50),
               select.Clip(start=100, end=130, score=70)]
kept = select.drop_overlaps(overlapping)
check("겹치는 클립 중 점수 높은 쪽만 남긴다",
      len(kept) == 2 and kept[0].start == 0 and kept[1].start == 100,
      str([(c.start, c.end) for c in kept]))

print("\n자막 파싱 (중복 제거)")
from app.pipeline.transcribe import parse_vtt  # noqa: E402
import tempfile  # noqa: E402

vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
안녕하세요

00:00:03.000 --> 00:00:05.000
안녕하세요 반갑습니다

00:00:05.000 --> 00:00:07.000
<c>오늘은</c> 좋은 날
"""
with tempfile.NamedTemporaryFile("w", suffix=".vtt", delete=False, encoding="utf-8") as f:
    f.write(vtt)
    tmp = Path(f.name)
segs = parse_vtt(tmp)
tmp.unlink()
check("자동 자막 중복 줄을 합친다", len(segs) == 2, f"{len(segs)}개: {[s.text for s in segs]}")
check("HTML 태그 제거", all("<" not in s.text for s in segs))

print(f"\n{'전부 통과' if not failures else f'실패 {len(failures)}건: ' + ', '.join(failures)}")
raise SystemExit(1 if failures else 0)
