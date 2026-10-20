# slack-lab

Slack 멘션에 로컬 LLM(Ollama)이 스레드로 답하는 셀프호스트 봇. 큐·워커 분리, 선택형 RAG, 모니터링·알람 포함. LLM은 로컬 Ollama(OpenAI 호환 스키마)를 쓰므로 API 비용이 들지 않는다.

**현재 `v0.6.0`(6단계까지 완료, 검증 범위는 [RELEASE-LIMITS](docs/RELEASE-LIMITS.md) 참고).** Slack 멘션 → 수신 API → RabbitMQ → 워커(Ollama, 선택적 RAG) → 스레드 답글. 같은 워커가 CloudWatch(SNS) 형식 알람도 받아 리포트를 올린다. 선택 기능(RAG·알람·요청 분류)은 `.env`로 켠다.

## 하드웨어 요구사항 (6단계 실측 — 일부 미달·미측정)

켜는 기능에 따라 필요한 자원이 크게 다르다. **AI와 모니터링을 모두 끄면 가장 가볍고**, 모니터링은 선택 프로필(`compose.monitoring.yaml`)이라 켜지 않으면 컨테이너 4개가 아예 뜨지 않는다. 칸마다 근거를 구분한다.

- **[설정 상한]** = compose에 박힌 컨테이너 메모리 상한의 합. 코드가 정한 값이지 사용량이 아니다.
- **[실측]** = `scripts/hw-measure`로 잰 값. 아래 한 대의 장비·한 번의 30분 창에서만 얻었고 다른 환경으로 일반화하지 않는다.
- **[추정 권장]** = 측정 근거가 없는 권장치. 이번 릴리스에는 **없다**(추정을 만들지 않았다).

**측정 환경**: Apple Silicon(arm64, 8코어, 호스트 RAM 16GiB), Docker Desktop VM 약 8.5GiB, 측정 중 IDE·브라우저 등 다른 앱을 그대로 둔 채(앱 종료 요구 없음) 쟀다. 부하는 워밍업 2 · 순차 20 · 동시 5×2 · 정상 5 · 실패 5이고 LLM은 echo(모델 0)다. 측정 시작 때 호스트 스왑이 이미 6.7~11GiB 사용 중이라 스왑 증가는 **환경 한계(원인 미확정)**로 본다. 판정선(스왑 증가 ≤512MiB, Docker VM ≤80%)은 바꾸지 않았다.

| # | 구성 | 컨테이너 메모리 [설정 상한] 호스트 방식 | 앱 컨테이너 방식 | 호스트 스왑 증가 / Docker VM 최고 [실측, 30분 창] |
|---|---|---|---|---|
| 1 | AI 끔(echo), 모니터링 끔 | 896 MiB (RabbitMQ 512 + Postgres 384) | 2,432 MiB (+ 앱 3개 × 512) | 호스트 방식 **+1,092 MiB / 3.9% — 미달(스왑)**. 앱 방식 +309 MiB / 13.2% — 합격 |
| 2 | AI 끔(echo) + 모니터링 | 3,072 MiB (+ 모니터링 4개 합 2,176) | 4,608 MiB | 호스트 방식 **+755 MiB / 7.6% — 미달(스왑), 18분 구간뿐이라 30분 연속 창 아님**. 앱 방식 **+527.4 MiB / 1.78GB(19.6%) — 미달(스왑, 한계 대비 3% 초과)** |
| 3 | Ollama 3b | 896 MiB + 호스트 Ollama(컨테이너 밖 RAM) | 2,432 MiB + 호스트 Ollama | **미측정** |
| 4 | Ollama 7b | 896 MiB + 호스트 Ollama(7b가 더 큼) | 2,432 MiB + 호스트 Ollama | **미측정** |
| 5 | Ollama 3b + 임베딩(bge-m3) RAG | 896 MiB + 호스트 Ollama 모델 2개 | 2,432 MiB + 호스트 Ollama 모델 2개 | **미측정** |
| 6 | 전체(7b + RAG + 모니터링) | 3,072 MiB + 호스트 Ollama 모델 2개 | 4,608 MiB + 호스트 Ollama 모델 2개 | **미측정** |

- 위 [실측] 칸의 재시작·OOM은 0, 누락·중복은 0이었다(행 1·2 모두). 판정이 "미달"인 것은 스왑 증가 한계뿐이다.
- Ollama는 호스트에서 돌아 **Docker VM 할당량 밖의 RAM**을 쓴다. 호스트 JVM(호스트 방식)은 상한이 없다. 행 3~6의 모델 RAM은 측정하지 않았다.
- **Linux·RAM 8GB 같은 다른 환경은 검증하지 않았다(미검증).** 위 값은 Apple Silicon + Docker Desktop 한 대의 기록이다.
- 행 7(클라우드 추론, 로컬 모델 없음)은 7단계에서 추가한다.
- 컨테이너 상한의 모니터링 4개 합은 Alloy 256 + Loki 1,024 + Prometheus 512 + Grafana 384 MiB다.
- **RabbitMQ `mem_limit` 512m은 메모리 압박 시 OOM으로 죽을 수 있다.** 스모크 중 한 번 죽어(`docker start`로 복구) 알람 경유 요청이 503이 됐다. 메모리가 빠듯하면 먼저 점검한다. 이때 앱 쪽 `app_unobservable` 알람이 이 상황에 실제로 반응하는지는 검증하지 않았다.
- 상태별 증거, 알려진 한계와 미검증 목록 → [`docs/RELEASE-LIMITS.md`](docs/RELEASE-LIMITS.md)

- **릴리스 한계(상태별 증거)** → [`docs/RELEASE-LIMITS.md`](docs/RELEASE-LIMITS.md)
- **한 장 요약** → [`docs/RETROSPECTIVE.md`](docs/RETROSPECTIVE.md) (단계별 결론·배운 것·한계)
- **처음 설치한다면** → [`docs/SETUP.md`](docs/SETUP.md) (Slack 앱 생성부터 첫 멘션 응답까지, Manifest 제공)
- n8n으로 알람 API를 부르는 예제 → [`docs/n8n/README.md`](docs/n8n/README.md) (선택 사항)
- 요구사항 → [`.claude/docs/PRD.md`](.claude/docs/PRD.md)
- 구조·결정 기록 → [`.claude/docs/ARCHITECTURE.md`](.claude/docs/ARCHITECTURE.md)
- 작업 계획·진행 → [`.claude/docs/PLAN.md`](.claude/docs/PLAN.md)
- 실험 기록 → [`docs/EXPERIMENT-LOG.md`](docs/EXPERIMENT-LOG.md)
- 에이전트 공통 규칙·명령어 → [`AGENTS.md`](AGENTS.md)

이 저장소는 public이다. 비밀값은 `.env`에만 두고 커밋하지 않는다(`.env.example`에는 키 이름만 있다).
