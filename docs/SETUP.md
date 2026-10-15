# 설치 가이드 (처음부터 첫 멘션 응답까지)

Slack 앱 설정 누락(스코프·이벤트 구독·채널 초대)은 "첫 멘션이 조용히 무응답"으로 나타나 원인을 찾기 어렵다. 이 가이드는 그 실수를 줄이려고 순서를 고정한다. **Slack 화면 조작(앱 생성·설치·채널 초대·URL 등록)은 사람이 직접 한다.**

준비물: Java 21, Docker, [Ollama](https://ollama.com), [ngrok](https://ngrok.com)(또는 같은 일을 하는 터널), Slack 워크스페이스(앱을 만들 권한).

## 하드웨어 요구사항 (6단계 초안 — 실측 대기)

켜는 기능에 따라 필요한 자원이 크게 다르다. **AI와 모니터링을 모두 끄면 가장 가볍고**, 모니터링은 선택 프로필(`compose.monitoring.yaml`)이라 켜지 않으면 컨테이너 4개가 아예 뜨지 않는다. 아래 표의 칸은 근거를 구분해서 적는다: **[설정 상한]** = compose에 박힌 컨테이너 메모리 상한의 합(코드가 보장하는 값), **[실측]** = `scripts/hw-measure`로 잰 값(6단계 M49에서 채운다, 지금은 비어 있음), **[추정 권장]** = 실측 전의 추정(아직 없음). 실측 전 값을 사실처럼 읽지 않는다.

| # | 구성 | 호스트 방식(인프라만 컨테이너) 컨테이너 메모리 [설정 상한] | 앱 컨테이너 방식 컨테이너 메모리 [설정 상한] | 호스트 RAM·디스크·Docker VM [실측] |
|---|---|---|---|---|
| 1 | AI 끔(echo), 모니터링 끔 | 896 MiB (RabbitMQ 512 + Postgres 384) | 2,432 MiB (+ 앱 3개 × 512) | 실측 대기 |
| 2 | AI 끔(echo) + 모니터링 | 3,072 MiB (+ 모니터링 4개 합 2,176) | 4,608 MiB | 실측 대기 |
| 3 | Ollama 3b | 896 MiB + 호스트 Ollama(컨테이너 밖 RAM) | 2,432 MiB + 호스트 Ollama | 실측 대기 |
| 4 | Ollama 7b | 896 MiB + 호스트 Ollama(7b가 더 큼) | 2,432 MiB + 호스트 Ollama | 실측 대기 |
| 5 | Ollama 3b + 임베딩(bge-m3) RAG | 896 MiB + 호스트 Ollama 모델 2개 | 2,432 MiB + 호스트 Ollama 모델 2개 | 실측 대기 |
| 6 | 전체(7b + RAG + 모니터링) | 3,072 MiB + 호스트 Ollama 모델 2개 | 4,608 MiB + 호스트 Ollama 모델 2개 | 실측 대기 |

- Ollama는 호스트에서 돌아 **Docker VM 할당량 밖의 RAM**을 쓴다. 호스트 JVM(방식 1)은 상한이 없다.
- 행 7(클라우드 추론, 로컬 모델 없음)은 7단계에서 추가한다.
- Linux·8GB 같은 다른 환경의 값은 실측 전이라 적지 않는다(미검증).
- 컨테이너 상한은 모니터링 4개(Alloy 256 + Loki 1,024 + Prometheus 512 + Grafana 384 MiB) 기준이다.

## 1. 앱 생성 — Manifest로

1. <https://api.slack.com/apps> → **Create New App** → **From a manifest** → 워크스페이스 선택.
2. [`slack-app-manifest.json`](slack-app-manifest.json)의 내용을 JSON 탭에 붙여넣는다. 선언되어 있는 것:
   - 봇 스코프 4개: `app_mentions:read`(멘션 수신), `chat:write`(답글), `reactions:write`(👀 반응), `channels:history`(스레드 문맥 조회, 공개 채널)
   - 봇 이벤트 1개: `app_mention`
   - Request URL은 자리표시자(`your-ngrok-host.example.com`)다. **4단계에서 실제 주소로 바꾼다.**
3. 만들어지지 않거나 이벤트 구독이 보이지 않으면: Manifest에서 `settings.event_subscriptions` 블록을 지우고 생성한 뒤, **Event Subscriptions**를 켜고 봇 이벤트 `app_mention`을 직접 추가한다(4단계에서 같이 한다).

## 2. 설치

**Install App** → **Install to Workspace** → 허용. 설치하면 봇 토큰이 발급된다.

> 스코프를 나중에 추가·변경하면 **앱을 다시 설치(Reinstall)** 해야 토큰에 반영된다. 설치하지 않으면 `missing_scope`로 실패한다.

## 3. 토큰 입력과 점검

1. `.env.example`을 `.env`로 복사한다(`.env`는 커밋하지 않는다).
2. 값을 채운다. 위치:
   - `SLACK_SIGNING_SECRET` — **Basic Information → App Credentials → Signing Secret**
   - `SLACK_BOT_TOKEN` — **OAuth & Permissions → Bot User OAuth Token**(`xoxb`로 시작)
   - `POSTGRES_PASSWORD` — 직접 정한 값
   - `LLM_MODEL` — `ollama list`로 확인한 모델 ID(추측하지 않는다)
   - `SLACK_TEST_CHANNEL` — 왕복 확인용 채널 ID(채널 이름을 우클릭 → 링크 복사 끝의 `C…`)
3. 점검한다:
   ```bash
   scripts/env-check
   ```
   값은 출력하지 않고 키 이름과 사유만 알려준다. 오류가 있으면 고치고 다시 실행한다. 선택 기능(RAG·알람·분류)을 켰다면 그 조합도 함께 점검한다.

## 4. 기동과 URL 검증

```bash
ollama serve                                      # 별도 터미널 (모델이 없으면 ollama pull <모델>)
docker compose up -d rabbitmq postgres            # 큐·작업 상태
set -a && source .env && set +a && ./gradlew bootRun
curl localhost:8080/health                        # 200
ngrok http 8080                                   # 별도 터미널, 나온 https 주소를 복사
```

1. Slack 앱 → **Event Subscriptions** → Request URL을 `https://<현재-ngrok-host>/slack/events`로 바꾸면 **Verified**가 떠야 한다. 서명 검증은 서버가 하므로 서버가 떠 있어야 한다.
2. **Subscribe to bot events**에 `app_mention`이 있는지 확인하고 저장한다. **Enable Events**가 켜져 있어야 한다.

> ngrok 무료 주소는 **재시작할 때마다 바뀐다.** 바뀌면 Request URL을 다시 등록하고 Verified를 확인한다.

## 5. 채널 초대와 확인

1. 답을 받을 채널에서 `/invite @slack-lab`(봇을 초대). 초대하지 않으면 이벤트가 오지 않는다.
   알람 수신(`ALERT_SECRET`·`ALERT_CHANNEL`)을 켰다면 **`ALERT_CHANNEL` 채널에도** 봇을 초대한다. 초대하지 않으면 리포트 전송이 `not_in_channel`로 실패한다. 같은 이름의 앱이 둘이면 App ID로 새 앱을 구분해 초대한다.
2. 채널에서 `@slack-lab 안녕` 처럼 멘션한다.
3. 기대 결과: 멘션에 **👀 반응 1개**, **스레드 답글 1개**. 첫 호출은 모델 적재로 느릴 수 있다.

## 모니터링 지표 켜기 (선택, 6단계)

기본 구성에서는 꺼져 있고, 안 켜도 앱은 5단계와 똑같이 동작한다. 지표(Prometheus 형식)를 내보내려면 `.env`에 아래 두 키를 **직접** 넣는다. 값은 이 저장소 어디에도 커밋하지 않는다(`.githooks/pre-commit`이 값이 있는 줄을 막는다).

- `MANAGEMENT_METRICS_TOKEN`: `openssl rand -hex 32`로 만든 64자리 16진수. `ALERT_SECRET`·Slack 비밀·Grafana 비밀번호와 **다른 값**이어야 한다.
- `GRAFANA_ADMIN_PASSWORD`: Grafana 관리자 비밀번호(8자 이상, `admin` 같은 기본값 금지).

넣은 뒤 `scripts/env-check --monitoring [--mode host|app]`로 형식을 점검한다(값은 출력하지 않는다). 토큰이 있으면 앱이 **별도 관리 포트 9091**에서 `GET /actuator/prometheus`를 `Authorization: Bearer <토큰>`으로만 내보낸다. 호스트에서 `bootRun`하면 `127.0.0.1`에만 열리고, 컨테이너(`--profile app`)는 9091을 호스트에 게시하지 않는다. 토큰을 지우면 관리 포트는 열리지 않는다. 8080(수신)과 같은 포트로 설정하면 기동이 거부된다.

모니터링 도구가 만드는 비밀 파일(토큰 회전 시험 등)은 저장소 **밖** `~/.slack-lab-secrets`(권한 600)에 둔다. 저장소가 iCloud 같은 동기화 폴더 안에 있을 수 있어 `.runtime/` 아래에는 비밀을 두지 않는다. 위치를 바꾸려면 `SLACK_LAB_SECRET_DIR`을 지정한다.

## 막혔을 때

| 증상 | 확인 |
|---|---|
| 멘션해도 아무 일도 없다 | 봇을 채널에 초대했는가 / Event Subscriptions의 Request URL이 Verified인가 / `app_mention` 구독·Enable Events / ngrok 주소가 바뀌지 않았는가 / 서버 로그에 요청이 도착하는가 |
| `missing_scope` | 스코프 추가 뒤 Reinstall 했는가 |
| 비공개 채널에서 스레드 문맥이 안 읽힌다 | 봇 스코프에 `groups:history`를 추가하고 **재설치**, 그 채널에 봇 초대 |
| Request URL이 Verified되지 않는다 | 서버가 떠 있는가 / `SLACK_SIGNING_SECRET`이 이 앱의 값인가 / 주소 끝이 `/slack/events`인가 |
| 알람 리포트가 채널에 안 올라온다(로그에 `not_in_channel`) | `ALERT_CHANNEL` 채널에도 **새 앱을 초대**했는가. 답변 채널과 알람 채널이 다르면 둘 다 초대한다. 실패한 알람은 DLQ에 남으므로 `scripts/recovery list`로 확인한다 |
| 기동이 바로 실패한다 | `scripts/env-check` 먼저. RAG를 켰다면 `RAG_EMBEDDING_MODEL`·`RAG_EMBEDDING_DIMENSION` |

## 다른 앱으로 바꿨다가 돌아가기

앱마다 서명 시크릿·봇 토큰이 다르다. 앱을 바꾸면 `.env`의 `SLACK_SIGNING_SECRET`과 `SLACK_BOT_TOKEN`을 함께 그 앱의 값으로 바꾸고 서버를 **재시작**한다(환경변수는 기동 때 읽는다). 원래 앱으로 돌아갈 때도 두 값을 함께 되돌리고, 그 앱의 Request URL이 지금 터널 주소인지 확인한다. 이전 앱 쪽 Event Subscriptions는 꺼 두면 두 앱이 같은 채널에 중복으로 답하지 않는다.

## 이 가이드가 하지 않는 것

도구 설치(Java·Docker·Ollama·ngrok), 모델 내려받기, 터널 자동화, 고정 도메인 설정은 다루지 않는다. 운영 체제와 계정마다 달라서 자동화하지 않기로 했다(PLAN 5단계).
