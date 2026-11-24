"""유튜브 없이 전체 파이프라인을 돌려보는 통합 테스트.

다운로드 단계만 로컬 파일로 바꿔치기하고, 전사·신호분석·선별·렌더링·
작업 큐·API 응답까지 실제 코드로 확인한다.

    python3 scripts/test_pipeline.py <video.mp4> [subs.vtt]
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import jobs  # noqa: E402
from app.pipeline import download  # noqa: E402


def install_local_source(video: Path, subs: Path | None) -> None:
    """download 모듈의 네트워크 함수를 로컬 파일로 대체."""
    import subprocess

    duration = float(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )

    def fake_probe(url: str) -> download.VideoMeta:
        return download.VideoMeta(
            video_id="local", title=f"[로컬] {video.name}", duration=duration,
            uploader="테스트", subtitle_langs=["ko"] if subs else [],
        )

    def fake_download(url: str, workdir: Path) -> Path:
        workdir.mkdir(parents=True, exist_ok=True)
        dest = workdir / f"source{video.suffix}"
        shutil.copy(video, dest)
        return dest

    def fake_subs(url: str, workdir: Path, langs: str = "ko,en") -> Path | None:
        if not subs:
            return None
        dest = workdir / "subs.ko.vtt"
        shutil.copy(subs, dest)
        return dest

    download.probe = fake_probe                 # type: ignore[assignment]
    download.download_video = fake_download     # type: ignore[assignment]
    download.download_subtitles = fake_subs     # type: ignore[assignment]
    download.normalize_url = lambda u: u        # type: ignore[assignment]

    jobs.download.probe = fake_probe            # type: ignore[attr-defined]
    jobs.download.download_video = fake_download
    jobs.download.download_subtitles = fake_subs
    jobs.download.normalize_url = lambda u: u


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    video = Path(sys.argv[1]).resolve()
    subs = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    if not video.is_file():
        print(f"영상이 없습니다: {video}")
        return 2

    install_local_source(video, subs)

    opts = jobs.JobOptions(aspect="9:16", crop_mode="auto", clip_count=2,
                           min_seconds=15, max_seconds=45)
    job = jobs.store.create("https://youtube.com/watch?v=local", opts)
    print(f"작업 생성: {job.id}")

    last = None
    started = time.time()
    while job.stage not in {"done", "failed", "canceled"}:
        if time.time() - started > 900:
            print("시간 초과")
            return 1
        state = (job.stage, round(job.progress, 2), job.message)
        if state != last:
            print(f"  [{job.progress * 100:5.1f}%] {job.stage_label if False else job.stage:12s} {job.message}")
            last = state
        time.sleep(0.4)

    print(f"\n최종 상태: {job.stage}")
    if job.error:
        print(f"오류: {job.error}")
        print(job.diagnostics.get("traceback", ""))
        return 1

    print(f"진단: {json.dumps(job.diagnostics, ensure_ascii=False)}")
    print(f"\n클립 {len(job.clips)}개:")
    ok = True
    for c in job.clips:
        path = jobs.store.clip_path(job.id, c["file"])
        exists = path.is_file() if path else False
        ok &= exists
        print(f"  #{c['index']} {c['start']:.1f}~{c['end']:.1f}s ({c['duration']}s) "
              f"{c['size_bytes'] / 1048576:.1f}MB  파일={'있음' if exists else '없음!'}")
        print(f"      제목: {c['title']}")
        print(f"      근거: {c['reason']}")
        if c.get("reframe"):
            print(f"      리프레임: {c['reframe']}")

    # 겹침 검사
    spans = sorted((c["start"], c["end"]) for c in job.clips)
    for a, b in zip(spans, spans[1:]):
        if b[0] < a[1]:
            print(f"  ⚠ 클립이 겹칩니다: {a} / {b}")
            ok = False

    print("\n통과" if ok and job.clips else "\n실패")
    return 0 if ok and job.clips else 1


if __name__ == "__main__":
    raise SystemExit(main())
