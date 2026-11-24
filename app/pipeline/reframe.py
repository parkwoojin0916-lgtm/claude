"""화자 추적 자동 리프레임.

가로 영상을 세로로 자를 때 중앙만 쓰면 화면 끝에 있는 화자가 잘린다.
프레임을 샘플링해 피사체 위치를 찾고, 흔들리지 않게 다듬은 뒤
ffmpeg sendcmd로 crop 창을 시간에 따라 움직인다.

피사체 탐지는 두 단계다.
  1) YuNet 얼굴 검출 — 말하는 사람이 있는 영상에서 가장 정확하다.
  2) 움직임·엣지 돌출도 — 얼굴이 없는 영상(게임, 풍경, 그래픽)용 폴백.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "models" / "yunet.onnx"

# 분석 샘플링 주기(초). 촘촘할수록 정확하지만 느리다.
SAMPLE_INTERVAL = 0.25
# 출력 명령 주기(초). 이 간격으로 보간해 내보낸다.
COMMAND_INTERVAL = 0.1
# 이 비율(크롭 폭 대비)보다 적게 움직이면 카메라를 고정한다. 미세 떨림 방지.
DEAD_ZONE = 0.06
# 한 번에 움직일 수 있는 최대 속도 (크롭 폭 대비 초당 비율)
MAX_PAN_SPEED = 0.55


@dataclass
class CropPath:
    """시간에 따른 크롭 창 위치."""

    crop_w: int
    crop_h: int
    # (시각, x), (시각, y) 키프레임
    keys: list[tuple[float, float, float]]
    method: str  # face | motion | static
    face_ratio: float = 0.0  # 얼굴이 검출된 샘플 비율


def _load_detector(size: tuple[int, int]):
    import cv2

    if not MODEL_PATH.is_file():
        return None
    try:
        return cv2.FaceDetectorYN.create(str(MODEL_PATH), "", size, 0.6, 0.3, 5000)
    except Exception:  # noqa: BLE001
        return None


def _face_center(detector, frame) -> tuple[float, float, float] | None:
    """가장 크고 확신도 높은 얼굴의 중심. (x, y, 점수) 또는 None."""
    h, w = frame.shape[:2]
    detector.setInputSize((w, h))
    try:
        _, faces = detector.detect(frame)
    except Exception:  # noqa: BLE001
        return None
    if faces is None or len(faces) == 0:
        return None

    best, best_weight = None, -1.0
    for f in faces:
        fx, fy, fw, fh, score = f[0], f[1], f[2], f[3], f[14]
        # 큰 얼굴 + 높은 확신도를 우선. 화면 중앙에 가까우면 약간 가산.
        center_bias = 1.0 - abs((fx + fw / 2) / w - 0.5)
        weight = fw * fh * float(score) * (0.75 + 0.25 * center_bias)
        if weight > best_weight:
            best_weight, best = weight, (fx + fw / 2, fy + fh / 2, float(score))
    return best


def _motion_center(prev_gray, gray) -> tuple[float, float] | None:
    """프레임 차이 + 엣지로 '눈길이 가는' 지점을 추정.

    전체 무게중심을 쓰면 배경의 미세한 압축 노이즈가 넓은 면적을 차지해
    작은 피사체를 압도한다. 상위 분위수만 남긴 뒤 무게중심을 구한다.
    """
    import cv2
    import numpy as np

    if prev_gray is None:
        return None
    diff = cv2.absdiff(gray, prev_gray)
    edges = cv2.Laplacian(gray, cv2.CV_16S, ksize=3)
    saliency = diff.astype(np.float32) * 2.0 + np.abs(edges).astype(np.float32) * 0.4
    saliency = cv2.GaussianBlur(saliency, (0, 0), sigmaX=9)

    peak = float(saliency.max())
    if peak < 1.0:
        return None  # 정지 화면

    # 상위 10%만 남긴다. 그래도 비면 문턱을 낮춘다.
    for q in (90.0, 75.0, 50.0):
        thresh = float(np.percentile(saliency, q))
        masked = np.where(saliency >= max(thresh, peak * 0.25), saliency, 0.0)
        total = float(masked.sum())
        if total > 1e-3:
            break
    else:
        return None

    h, w = masked.shape
    xs = np.arange(w, dtype=np.float32)
    ys = np.arange(h, dtype=np.float32)
    cx = float((masked.sum(axis=0) * xs).sum() / total)
    cy = float((masked.sum(axis=1) * ys).sum() / total)
    return cx, cy


def _smooth(values: list[float], window: int = 7) -> list[float]:
    """이동 평균. 샘플 노이즈를 없앤다."""
    if len(values) <= 2:
        return values[:]
    half = max(1, window // 2)
    out: list[float] = []
    for i in range(len(values)):
        lo, hi = max(0, i - half), min(len(values), i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def _apply_dead_zone(values: list[float], span: float) -> list[float]:
    """작은 흔들림은 무시하고, 큰 이동은 속도를 제한해 따라간다."""
    if not values:
        return values
    dead = span * DEAD_ZONE
    max_step = span * MAX_PAN_SPEED * SAMPLE_INTERVAL
    out = [values[0]]
    for target in values[1:]:
        cur = out[-1]
        delta = target - cur
        if abs(delta) < dead:
            out.append(cur)  # 미세한 흔들림은 무시하고 고정
            continue
        # 움직이기로 했으면 목표까지 따라간다. 데드존만큼 빼면 꾸준히 움직이는
        # 피사체를 영영 못 따라잡고 계속 뒤처진다. 속도만 제한한다.
        delta = max(-max_step, min(max_step, delta))
        out.append(cur + delta)
    return out


def compute_crop_path(
    video: Path,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    target_w: int,
    target_h: int,
) -> CropPath | None:
    """구간 내 피사체를 따라가는 크롭 경로를 계산한다."""
    try:
        import cv2
    except ImportError:
        return None

    # 목표 비율을 덮는 최대 크롭 창
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if abs(src_ratio - target_ratio) < 0.01:
        return None  # 비율이 같으면 패닝할 여지가 없다
    if src_ratio > target_ratio:
        crop_h = src_h
        crop_w = int(round(src_h * target_ratio))
    else:
        crop_w = src_w
        crop_h = int(round(src_w / target_ratio))
    crop_w = min(src_w, crop_w - crop_w % 2)
    crop_h = min(src_h, crop_h - crop_h % 2)

    slack_x, slack_y = src_w - crop_w, src_h - crop_h
    if slack_x <= 1 and slack_y <= 1:
        return None

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None

    detector = _load_detector((src_w, src_h))
    times: list[float] = []
    cxs: list[float] = []
    cys: list[float] = []
    face_hits = 0
    prev_gray = None

    t = start
    try:
        while t < end:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, frame = cap.read()
            if not ok:
                break
            # 분석은 축소본으로 — 충분히 정확하고 훨씬 빠르다
            scale = 640 / max(frame.shape[1], 1)
            small = (
                cv2.resize(frame, (0, 0), fx=scale, fy=scale)
                if scale < 1.0
                else frame
            )
            sh, sw = small.shape[:2]

            center = None
            if detector is not None:
                face = _face_center(detector, small)
                if face:
                    face_hits += 1
                    center = (face[0], face[1])
            if center is None:
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                center = _motion_center(prev_gray, gray)
                prev_gray = gray
            else:
                prev_gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            if center is not None:
                times.append(t)
                cxs.append(center[0] / sw * src_w)
                cys.append(center[1] / sh * src_h)
            t += SAMPLE_INTERVAL
    finally:
        cap.release()

    if len(times) < 2:
        return None

    face_ratio = face_hits / max(1, len(times))
    method = "face" if face_ratio >= 0.3 else "motion"

    # 피사체 중심 → 크롭 좌상단
    raw_x = [min(max(cx - crop_w / 2, 0), slack_x) for cx in cxs]
    raw_y = [min(max(cy - crop_h / 2, 0), slack_y) for cy in cys]

    # 얼굴은 보통 화면 위쪽에 둬야 보기 좋다: 세로 여유가 있으면 조금 위로
    if slack_y > 1 and method == "face":
        raw_y = [min(max(y - crop_h * 0.06, 0), slack_y) for y in raw_y]

    xs = _apply_dead_zone(_smooth(raw_x), crop_w) if slack_x > 1 else [0.0] * len(raw_x)
    ys = _apply_dead_zone(_smooth(raw_y), crop_h) if slack_y > 1 else [0.0] * len(raw_y)

    keys = [
        (round(t - start, 3), round(min(max(x, 0), slack_x), 1),
         round(min(max(y, 0), slack_y), 1))
        for t, x, y in zip(times, xs, ys)
    ]

    # 거의 안 움직이면 고정 크롭이 낫다 (sendcmd 오버헤드 제거)
    if max(x for _, x, _ in keys) - min(x for _, x, _ in keys) < crop_w * 0.02 and \
       max(y for _, _, y in keys) - min(y for _, _, y in keys) < crop_h * 0.02:
        method = "static"

    return CropPath(crop_w=crop_w, crop_h=crop_h, keys=keys, method=method,
                    face_ratio=round(face_ratio, 3))


def write_sendcmd(path: CropPath, out_file: Path) -> Path:
    """크롭 경로를 ffmpeg sendcmd 파일로. 키프레임 사이는 선형 보간한다."""
    lines: list[str] = []
    keys = path.keys
    if not keys:
        raise ValueError("빈 크롭 경로")

    def at(t: float) -> tuple[float, float]:
        if t <= keys[0][0]:
            return keys[0][1], keys[0][2]
        if t >= keys[-1][0]:
            return keys[-1][1], keys[-1][2]
        for (t0, x0, y0), (t1, x1, y1) in zip(keys, keys[1:]):
            if t0 <= t <= t1:
                span = max(1e-6, t1 - t0)
                r = (t - t0) / span
                return x0 + (x1 - x0) * r, y0 + (y1 - y0) * r
        return keys[-1][1], keys[-1][2]

    duration = keys[-1][0]
    steps = int(duration / COMMAND_INTERVAL) + 1
    last: tuple[int, int] | None = None
    for i in range(steps):
        t = i * COMMAND_INTERVAL
        x, y = at(t)
        xi, yi = int(round(x)), int(round(y))
        if last == (xi, yi):
            continue  # 값이 그대로면 명령을 생략한다
        last = (xi, yi)
        lines.append(f"{t:.2f} crop x {xi}, crop y {yi};")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_file
