# slack-lab

Slack 이벤트를 트리거로 AI가 답하는 구조를 단계적으로 만드는 학습용 프로젝트. LLM은 로컬 Ollama(OpenAI 호환 스키마)를 쓰므로 API 비용이 들지 않는다.

**현재 `v0.5.0`(5단계까지 완료).** Slack 멘션 → 수신 API → RabbitMQ → 워커(Ollama, 선택적 RAG) → 스레드 답글. 같은 워커가 CloudWatch(SNS) 형식 알람도 받아 리포트를 올린다. 선택 기능(RAG·알람·요청 분류)은 `.env`로 켠다.

- **한 장 요약** → [`docs/RETROSPECTIVE.md`](docs/RETROSPECTIVE.md) (단계별 결론·배운 것·한계)
- **처음 설치한다면** → [`docs/SETUP.md`](docs/SETUP.md) (Slack 앱 생성부터 첫 멘션 응답까지, Manifest 제공)
- n8n으로 알람 API를 부르는 예제 → [`docs/n8n/README.md`](docs/n8n/README.md) (선택 사항)
- 요구사항 → [`.claude/docs/PRD.md`](.claude/docs/PRD.md)
- 구조·결정 기록 → [`.claude/docs/ARCHITECTURE.md`](.claude/docs/ARCHITECTURE.md)
- 작업 계획·진행 → [`.claude/docs/PLAN.md`](.claude/docs/PLAN.md)
- 실험 기록 → [`docs/EXPERIMENT-LOG.md`](docs/EXPERIMENT-LOG.md)
- 에이전트 공통 규칙·명령어 → [`AGENTS.md`](AGENTS.md)

이 저장소는 public이다. 비밀값은 `.env`에만 두고 커밋하지 않는다(`.env.example`에는 키 이름만 있다).
