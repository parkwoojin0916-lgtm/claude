# 쇼츠 커터

영상 링크를 넣으면 재밌는 구간을 찾아 쇼츠용 세로 클립으로 잘라주는 웹 서비스.
유튜브·트위치·Vimeo 등 yt-dlp가 지원하는 사이트를 받는다.

![파이프라인](https://img.shields.io/badge/stack-FastAPI%20%2B%20ffmpeg%20%2B%20Whisper%20%2B%20Claude-ff4d6d)

## 어떻게 고르는가 (하이브리드)

두 단계로 나눠서, 싼 신호로 범위를 좁힌 다음 비싼 모델로 판단한다.

**1단계 — 신호 분석 (무료, 빠름)**
전체 영상에 대해 0.5초 단위로 점수를 매긴다.

| 신호 | 의미 | 가중치 |
|---|---|---|
| 음량 급변 | 웃음·환호·고함이 터지는 지점 | 0.30 |
| 평균 음량 | 조용한 설명 구간 배제 | 0.22 |
| 발화 밀도 | 말이 빨라지는 흥분 구간 | 0.20 |
| 장면 전환 | 편집이 몰린 구간 | 0.18 |
| 자막 단서 | `ㅋㅋ`, `대박`, `헐`, `레전드` 등 | 0.30 |

여기에 도입부(구독 요청)와 엔딩은 감점한다. 30초 윈도우를 2초씩 밀면서
점수를 합산하고, 서로 겹치지 않는 상위 후보 10여 개를 남긴다.

**2단계 — Claude 선별 (API 키 있을 때)**
후보들의 자막 전문을 Claude에게 넘겨 실제로 쇼츠가 될 구간만 고르게 한다.
모델은 구간을 고르면서 동시에 이것들을 만든다.

- 경계 조정 (말이 잘리지 않게)
- 쇼츠 제목, 첫 화면 후킹 문구
- 업로드용 캡션과 해시태그
- 그 구간을 고른 이유

각 클립은 다섯 항목으로 나눈 점수를 받는다 — 후킹, 완결성, 재미/가치,
반응, 도달. 총점 하나보다 어디가 약한지 보인다.

API 키가 없으면 1단계 점수만으로 상위 구간을 뽑는다. 동작은 하지만
"왜 재밌는지"는 모르기 때문에 품질 차이가 난다.

**원하는 구간 지정**
"요리하는 장면만", "게스트가 실수하는 부분" 처럼 글로 지정하면 그 주제가
언급된 구간을 1단계에서 가산하고, Claude에게도 같은 요청을 전달한다.
비워두면 전체에서 알아서 고른다.

## 화자 추적 리프레임

가로 영상을 세로로 자를 때 중앙만 쓰면 화면 끝에 있는 화자가 잘린다.
`auto` 모드는 0.25초마다 프레임을 샘플링해 피사체를 찾고, 크롭 창을
시간에 따라 움직인다 (ffmpeg `sendcmd`).

- **얼굴 검출**(YuNet ONNX) — 말하는 사람이 있는 영상에서 가장 정확
- **움직임·엣지 돌출도** — 얼굴이 없는 영상(게임, 풍경, 그래픽)용 폴백
- 미세 떨림은 데드존으로 무시하고, 패닝 속도를 제한해 멀미를 막는다
- 피사체가 거의 안 움직이면 중앙 고정으로 떨어진다 (불필요한 연산 생략)

합성 테스트 기준 추적 오차는 크롭 폭의 약 7%다. 같은 구간을 중앙 고정으로
자르면 피사체가 프레임 밖으로 나가는 구간이 생긴다.

## 전사

유튜브에 자막이 있으면 그걸 쓴다 (빠르고 공짜). 자동 생성 자막의
롤링 중복 줄은 걸러낸다. 자막이 없으면 faster-whisper로 직접 전사한다.

## 설치

```bash
# 시스템 의존성
sudo apt-get install -y ffmpeg fonts-nanum   # 자막 번인에 한글 폰트가 필요
pip install -r requirements.txt

# 화자 추적용 얼굴 검출 모델 (232KB). 없으면 움직임 추적으로만 동작한다.
mkdir -p models && curl -L -o models/yunet.onnx \
  https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx

# 설정
cp .env.example .env
$EDITOR .env          # ANTHROPIC_API_KEY 입력

# 실행
uvicorn app.main:app --reload --port 8000
```

http://localhost:8000 접속.

## 유튜브 봇 차단

클라우드 서버나 데이터센터 IP에서는 유튜브가 `Sign in to confirm you're not a bot`
으로 막는 경우가 많다. 브라우저에서 쿠키를 내보내 지정하면 된다.

```bash
# .env
YTDLP_COOKIES_FILE=/path/to/cookies.txt

# 로컬 머신에서 돌린다면 브라우저에서 바로 읽을 수도 있다
YTDLP_COOKIES_FROM_BROWSER=chrome
```

쿠키 내보내기는 [yt-dlp 위키](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies) 참고.

## 출력 옵션

| 옵션 | 값 |
|---|---|
| 비율 | `9:16` 쇼츠 · `4:5` 피드 · `1:1` 정사각 · `16:9` 가로 · `original` 원본 |
| 여백 처리 | `auto` 화자 추적 · `crop` 중앙 고정 · `blur` 블러 배경 |
| 클립 개수 | 1~8 |
| 클립 길이 | 짧게 15~30초 · 보통 20~60초 · 길게 45~90초 (API로는 초 단위 지정) |
| 원하는 구간 | 글로 지정 (`prompt`). 비우면 전체에서 선별 |
| 자막 번인 | 켜기/끄기 |
| 후킹 문구 | 첫 3.5초에 노란 자막으로 얹기 |

출력은 H.264 / AAC mp4, 30fps, `+faststart`. 바로 업로드할 수 있다.

## API

```bash
# 영상 정보만 확인 (다운로드 없음)
curl -X POST localhost:8000/api/preview \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://youtu.be/VIDEO_ID"}'

# 작업 시작
curl -X POST localhost:8000/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://youtu.be/VIDEO_ID", "aspect": "9:16",
       "crop_mode": "auto", "clip_count": 3, "prompt": "요리하는 장면만"}'

# 진행 상황
curl localhost:8000/api/jobs/JOB_ID

# 클립 받기
curl -O localhost:8000/api/jobs/JOB_ID/clips/clip_01.mp4
```

| 메서드 | 경로 | 설명 |
|---|---|---|
| `GET` | `/api/config` | 지원 비율, LLM 활성 여부 |
| `POST` | `/api/preview` | 제목·길이·자막 유무 확인 |
| `POST` | `/api/jobs` | 작업 생성 (202) |
| `GET` | `/api/jobs` | 작업 목록 |
| `GET` | `/api/jobs/{id}` | 진행률·단계·결과 |
| `POST` | `/api/jobs/{id}/cancel` | 취소 |
| `DELETE` | `/api/jobs/{id}` | 작업과 파일 삭제 |
| `GET` | `/api/jobs/{id}/clips/{file}` | 클립·썸네일 |

## 구조

```
app/
  main.py              FastAPI 라우트
  config.py            환경 설정
  jobs.py              작업 큐, 진행률, 파이프라인 오케스트레이션
  pipeline/
    download.py        yt-dlp 다운로드 / 메타데이터 / 오디오 추출
    transcribe.py      VTT 파싱, faster-whisper 전사
    signals.py         음량 시계열, 장면 전환 감지
    reframe.py         얼굴·움직임 추적 크롭 경로 계산
    candidates.py      슬라이딩 윈도우 점수 → 1차 후보
    select.py          Claude 최종 선별, 신호 기반 폴백
    render.py          ffmpeg 컷 / 비율 변환 / ASS 자막 번인
  static/index.html    프론트엔드 (단일 파일)
models/
  yunet.onnx           YuNet 얼굴 검출 모델 (별도 내려받기)
scripts/
  test_pipeline.py     로컬 영상으로 전체 파이프라인 검증
  test_api.py          API·파싱 단위 테스트
```

## 테스트

```bash
python3 scripts/test_api.py                        # 네트워크 불필요
python3 scripts/test_pipeline.py video.mp4 [subs.vtt]   # 전체 파이프라인
```

## 처리 시간

10분짜리 1080p 영상, 2코어 기준 대략:

| 단계 | 시간 |
|---|---|
| 다운로드 | 20~60초 |
| 전사 (유튜브 자막) | 5초 |
| 전사 (Whisper base) | 2~4분 |
| 신호 분석 | 30~60초 |
| Claude 선별 | 10~20초 |
| 화자 추적 분석 | 클립당 5~15초 |
| 렌더링 | 클립당 10~30초 |

`MAX_WORKERS`로 동시 작업 수를, `MAX_VIDEO_SECONDS`로 허용 길이를 제한한다.

## 배포

### Cloudflare Containers

`Dockerfile`, `wrangler.jsonc`, `worker/index.js`가 준비돼 있다.
Worker가 모든 요청을 컨테이너 안의 FastAPI로 넘기는 구조다.

**Workers Paid 플랜($5/월)이 필요하다.** 무료 플랜에서는 Containers API가
막혀 있다. 이미지 빌드에는 Docker가 돌고 있어야 한다.

```bash
npm install
npx wrangler login
npx wrangler deploy
```

배포되면 `https://shorts-cutter.<서브도메인>.workers.dev` 로 열린다.

API 키와 쿠키는 커밋하지 말고 시크릿으로 넣는다.

```bash
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put YTDLP_COOKIES_FILE   # 컨테이너 안 경로
```

인스턴스 타입은 `standard-2`(1 vCPU / 6GiB / 12GB)로 잡아뒀다. Whisper 전사와
ffmpeg 렌더링을 같이 돌리기 때문에 그 아래로 내리면 느려진다. 유휴 20분 뒤
잠들어 과금이 멈춘다.

**Docker 없이 배포하려면** 저장소를 [Workers Builds](https://developers.cloudflare.com/workers/ci-cd/builds/)에
연결한다. Cloudflare가 서버에서 이미지를 빌드한다.

### 일반 Docker 호스트 (Railway, Fly.io, Render 등)

같은 `Dockerfile`을 그대로 쓴다. 컨테이너 레지스트리가 아니라 Dockerfile을
직접 받는 호스트면 저장소만 연결하면 된다.

```bash
docker build -t shorts-cutter .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -v "$PWD/data:/data" \
  shorts-cutter
```

### 배포해도 유튜브는 막힐 수 있다

클라우드 IP는 유튜브가 봇으로 차단한다. 어디에 올리든 쿠키 없이는
다운로드가 실패한다. 서버에 개인 유튜브 쿠키를 두는 것은 계정 정지
위험이 있으니, 상업 서비스라면 공식 API나 권리 처리된 소스를 쓰는 편이 낫다.

## 저작권

다른 사람의 영상을 잘라 재업로드하는 것은 대부분의 경우 저작권 침해다.
본인 영상, 사용 허가를 받은 영상, 또는 인용·비평 등 공정이용 범위에서 쓸 것.
