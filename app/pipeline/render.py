"""클립 자르기 + 비율 변환 + 자막 번인."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from . import reframe
from .transcribe import Segment, slice_segments

SUB_FONT = "NanumSquare"
HOOK_FONT = "NanumSquareRound"


class RenderError(RuntimeError):
    pass


@dataclass
class RenderResult:
    path: Path
    # 자동 리프레임이 무엇을 했는지 (방식, 얼굴 검출 비율, 키프레임 수)
    reframe: dict[str, object] = field(default_factory=dict)


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def _wrap(text: str, limit: int) -> str:
    """ASS 줄바꿈(\\N) 삽입. 한 줄 limit자 기준."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= limit:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return "\\N".join(lines[:3])


def build_ass(
    segments: list[Segment],
    clip_start: float,
    clip_end: float,
    width: int,
    height: int,
    hook: str = "",
    out_path: Path | None = None,
) -> Path:
    """클립 상대시간 기준 ASS 자막 파일 생성."""
    assert out_path is not None
    font_size = max(28, int(height * 0.045))
    hook_size = max(34, int(height * 0.058))
    margin_v = int(height * 0.12)
    # 한글 글자폭은 폰트 크기의 약 0.74배, 좌우 6%씩 여백을 뺀다
    wrap_limit = max(10, int(width * 0.88 / (font_size * 0.74)))

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{SUB_FONT},{font_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,1,0,0,0,100,100,0,0,1,{max(3, font_size // 9)},2,2,{int(width * 0.06)},{int(width * 0.06)},{margin_v},1
Style: Hook,{HOOK_FONT},{hook_size},&H0000F0FF,&H00FFFFFF,&H00101010,&H80000000,1,0,0,0,100,100,0,0,1,{max(4, hook_size // 8)},3,8,{int(width * 0.06)},{int(width * 0.06)},{int(height * 0.08)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    if hook:
        lines.append(
            f"Dialogue: 0,{_ass_time(0)},{_ass_time(min(3.5, clip_end - clip_start))},"
            f"Hook,,0,0,0,,{_ass_escape(hook)}"
        )

    for seg in slice_segments(segments, clip_start, clip_end):
        start = max(0.0, seg.start - clip_start)
        end = min(clip_end - clip_start, seg.end - clip_start)
        if end - start < 0.2:
            continue
        text = _wrap(_ass_escape(seg.text.strip()), wrap_limit)
        if not text:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Sub,,0,0,0,,{text}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def _video_filter(
    target: tuple[int, int] | None,
    src_w: int,
    src_h: int,
    crop_mode: str,
    ass_path: Path | None,
    sendcmd_file: Path | None = None,
    crop_path: "reframe.CropPath | None" = None,
) -> str:
    parts: list[str] = []

    if target is None:
        parts.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    else:
        tw, th = target
        if crop_mode == "auto" and sendcmd_file and crop_path:
            # 피사체를 따라 crop 창을 움직인다. 시작 위치는 첫 키프레임.
            x0, y0 = int(crop_path.keys[0][1]), int(crop_path.keys[0][2])
            parts.append(f"sendcmd=f='{_escape_path(sendcmd_file)}'")
            parts.append(f"crop={crop_path.crop_w}:{crop_path.crop_h}:{x0}:{y0}")
            parts.append(f"scale={tw}:{th}")
            if ass_path:
                parts.append(f"ass='{_escape_path(ass_path)}'")
            return ",".join(parts)
        if crop_mode == "blur":
            # 배경: 꽉 채워 블러, 전경: 비율 유지해 fit
            bg = (
                f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                f"crop={tw}:{th},gblur=sigma=28,eq=brightness=-0.08[bg]"
            )
            fg = f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=decrease[fg]"
            chain = f"{bg};{fg};[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
            if ass_path:
                chain += f";[v]ass='{_escape_path(ass_path)}'[vout]"
            else:
                chain += ";[v]null[vout]"
            return chain
        parts.append(
            f"scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th}"
        )

    if ass_path:
        parts.append(f"ass='{_escape_path(ass_path)}'")
    return ",".join(parts)


def _escape_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def render_clip(
    source: Path,
    out_path: Path,
    start: float,
    end: float,
    *,
    aspect: str = "9:16",
    crop_mode: str = "crop",
    segments: list[Segment] | None = None,
    burn_subs: bool = True,
    hook: str = "",
    src_size: tuple[int, int] = (1920, 1080),
    workdir: Path | None = None,
) -> RenderResult:
    """구간을 잘라 지정 비율의 쇼츠 파일로 렌더링."""
    target = config.ASPECT_RATIOS.get(aspect, config.ASPECT_RATIOS["9:16"])
    out_w, out_h = target if target else src_size
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ass_path: Path | None = None
    if burn_subs and segments:
        base = workdir or out_path.parent
        ass_path = build_ass(
            segments, start, end, out_w, out_h, hook=hook,
            out_path=base / f"{out_path.stem}.ass",
        )
    elif hook:
        base = workdir or out_path.parent
        ass_path = build_ass(
            [], start, end, out_w, out_h, hook=hook,
            out_path=base / f"{out_path.stem}.ass",
        )

    crop_path = None
    sendcmd_file = None
    reframe_info: dict[str, object] = {}
    if crop_mode == "auto" and target is not None:
        try:
            crop_path = reframe.compute_crop_path(
                source, start, end, src_size[0], src_size[1], out_w, out_h
            )
        except Exception as exc:  # noqa: BLE001 — 추적 실패는 중앙 크롭으로 떨어진다
            reframe_info["error"] = f"{type(exc).__name__}: {exc}"
        if crop_path and crop_path.method != "static":
            base = workdir or out_path.parent
            sendcmd_file = reframe.write_sendcmd(
                crop_path, base / f"{out_path.stem}.cmd"
            )
            reframe_info = {
                "method": crop_path.method,
                "face_ratio": crop_path.face_ratio,
                "keyframes": len(crop_path.keys),
            }
        else:
            # 피사체가 거의 안 움직이거나 추적 실패 → 중앙 크롭으로 떨어진다
            reframe_info.setdefault(
                "method", crop_path.method if crop_path else "no_subject"
            )
            reframe_info["fell_back_to"] = "center_crop"
            crop_mode = "crop"
            crop_path = None

    vf = _video_filter(
        target, src_size[0], src_size[1], crop_mode, ass_path,
        sendcmd_file=sendcmd_file, crop_path=crop_path,
    )
    uses_complex = crop_mode == "blur" and target is not None

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(source),
    ]
    if uses_complex:
        cmd += ["-filter_complex", vf, "-map", "[vout]", "-map", "0:a?"]
    else:
        cmd += ["-vf", vf]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-r",
        "30",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(out_path),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=1800)
    if proc.returncode != 0 or not out_path.exists():
        raise RenderError(f"렌더링 실패: {proc.stderr[-500:]}")
    return RenderResult(path=out_path, reframe=reframe_info)


def make_thumbnail(clip_path: Path, out_path: Path, at: float = 1.0) -> Path | None:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{at:.2f}",
            "-i",
            str(clip_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=360:-2",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return out_path if proc.returncode == 0 and out_path.exists() else None
