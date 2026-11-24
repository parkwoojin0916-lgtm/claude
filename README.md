# 쇼츠 커터

유튜브 영상 링크를 넣으면 재밌는 구간을 찾아 쇼츠용 세로 클립으로 잘라주는 웹 서비스.

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

API 키가 없으면 1단계 점수만으로 상위 구간을 뽑는다. 동작은 하지만
"왜 재밌는지"는 모르기 때문에 품질 차이가 난다.

## 전사

유튜브에 자막이 있으면 그걸 쓴다 (빠르고 공짜). 자동 생성 자막의
롤링 중복 줄은 걸러낸다. 자막이 없으면 faster-whisper로 직접 전사한다.

## 설치

```bash
# 시스템 의존성
sudo apt-get install -y ffmpeg fonts-nanum   # 자막 번인에 한글 폰트가 필요
pip install -r requirements.txt

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
| 여백 처리 | `crop` 꽉 채워 자르기 · `blur` 블러 배경 위에 얹기 |
| 클립 개수 | 1~8 |
| 클립 길이 | 5~180초 범위에서 최소/최대 지정 |
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
  -d '{"url": "https://youtu.be/VIDEO_ID", "aspect": "9:16", "clip_count": 3}'

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
    candidates.py      슬라이딩 윈도우 점수 → 1차 후보
    select.py          Claude 최종 선별, 신호 기반 폴백
    render.py          ffmpeg 컷 / 비율 변환 / ASS 자막 번인
  static/index.html    프론트엔드 (단일 파일)
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
| 렌더링 | 클립당 10~30초 |

`MAX_WORKERS`로 동시 작업 수를, `MAX_VIDEO_SECONDS`로 허용 길이를 제한한다.

## 저작권

다른 사람의 영상을 잘라 재업로드하는 것은 대부분의 경우 저작권 침해다.
본인 영상, 사용 허가를 받은 영상, 또는 인용·비평 등 공정이용 범위에서 쓸 것.
