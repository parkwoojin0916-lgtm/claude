# 쇼츠 커터 — Cloudflare Containers / 일반 Docker 호스트 공용 이미지
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HF_HOME=/opt/hf

# ffmpeg: 컷·리프레임·자막 번인
# fonts-nanum: 한글 자막 번인에 필요 (없으면 네모로 깨진다)
# libgl1/libglib2.0-0: opencv 런타임 의존
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fonts-nanum libgl1 libglib2.0-0 curl ca-certificates \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 화자 추적용 얼굴 검출 모델 (232KB)
RUN mkdir -p /app/models && curl -fsSL -o /app/models/yunet.onnx \
    https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx

# Whisper 모델을 미리 받아둔다. 첫 요청에서 받으면 수 분이 날아간다.
ARG WHISPER_MODEL=base
ENV WHISPER_MODEL=${WHISPER_MODEL}
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

COPY app ./app

RUN mkdir -p /data/jobs
EXPOSE 8000

# 컨테이너 1개당 동시 작업 수. 인스턴스 vCPU에 맞춰 조절한다.
ENV MAX_WORKERS=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
