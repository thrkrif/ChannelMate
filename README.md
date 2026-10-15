# slack-lab

Slack 이벤트를 트리거로 AI가 답하는 구조를 단계적으로 만드는 학습용 프로젝트. LLM은 로컬 Ollama(OpenAI 호환 스키마)를 쓰므로 API 비용이 들지 않는다.

**현재 `v0.5.0`(5단계까지 완료).** Slack 멘션 → 수신 API → RabbitMQ → 워커(Ollama, 선택적 RAG) → 스레드 답글. 같은 워커가 CloudWatch(SNS) 형식 알람도 받아 리포트를 올린다. 선택 기능(RAG·알람·요청 분류)은 `.env`로 켠다.

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

- **한 장 요약** → [`docs/RETROSPECTIVE.md`](docs/RETROSPECTIVE.md) (단계별 결론·배운 것·한계)
- **처음 설치한다면** → [`docs/SETUP.md`](docs/SETUP.md) (Slack 앱 생성부터 첫 멘션 응답까지, Manifest 제공)
- n8n으로 알람 API를 부르는 예제 → [`docs/n8n/README.md`](docs/n8n/README.md) (선택 사항)
- 요구사항 → [`.claude/docs/PRD.md`](.claude/docs/PRD.md)
- 구조·결정 기록 → [`.claude/docs/ARCHITECTURE.md`](.claude/docs/ARCHITECTURE.md)
- 작업 계획·진행 → [`.claude/docs/PLAN.md`](.claude/docs/PLAN.md)
- 실험 기록 → [`docs/EXPERIMENT-LOG.md`](docs/EXPERIMENT-LOG.md)
- 에이전트 공통 규칙·명령어 → [`AGENTS.md`](AGENTS.md)

이 저장소는 public이다. 비밀값은 `.env`에만 두고 커밋하지 않는다(`.env.example`에는 키 이름만 있다).
