# 아키텍처 이미지 생성 기록

- 생성일: 2026-10-22
- 도구: 내장 `image_gen` (`imagegen` 스킬 기본 모드)
- 결과: `architecture.png` — README용 현재 구현 개요
- 기준: compose 두 파일, Alloy 설정, RabbitMQ 발행·반응 소비 코드, 아키텍처 문서, n8n 예제
- 최초 생성본의 반응 큐·Slack 출력 연결을 수정했다. RAG 경로는 설명으로 단순화했다. 검증 범위는 `../RELEASE-LIMITS.md`를 따른다.

## 구조 수정 프롬프트

```text
Use case: infographic-diagram. Edit the provided architecture infographic into an accurate, simplified diagram. Keep clean white background, typography, software icons, landscape proportions, title "slack-lab", and optional monitoring band. Replace the CENTRAL GRAPH completely with the following easy-to-render layout. Accuracy of arrow endpoints is mandatory.
MAIN upper area: ONE large light panel "Spring Boot · Java 21" containing Receiver at left, Worker at upper right, Reactor below Worker. RabbitMQ is BETWEEN Receiver and the two consumers, but OUTSIDE Spring panel: easiest omit the Spring group border entirely and add subtitle "Receiver / Worker / Reactor: Spring Boot · Java 21".
Row 1: Slack -> ngrok -> Receiver -> RabbitMQ -> Worker -> a separate box "Slack replies".
Row 2: DIRECT branch from RabbitMQ downward then right -> Reactor -> a separate box "Slack reactions". There is NO Worker -> Reactor connection. There is NO Reactor -> Ollama connection. Slack replies and Slack reactions are OUTPUT ENDPOINTS; no confusing loop arrows back to initial Slack.
Row 3 below worker: Worker <-> Postgres; Worker <-> Ollama. ONE Postgres box labeled "Postgres" with subtitle "Processing state / dedup" and "pgvector · optional RAG". ONE Ollama box with subtitle "Host · local inference". The Worker->Slack replies arrow originates at Worker, NOT Postgres. Reactor->Slack reactions originates at Reactor.
OPTIONAL RAG: avoid extra graph edges: small text note underneath Worker "Optional RAG: Ollama embeddings + Postgres pgvector". This accurately describes functionality without complicated lines.
TOP LEFT optional alert inputs: CloudWatch / SNS -> Receiver label "/alerts/cloudwatch"; Grafana -> Receiver label "/alerts/grafana"; Grafana -> n8n (optional relay) -> Receiver as alternative dashed route. No arrow from any alert box to RabbitMQ directly.
MONITORING band: keep Application roles -> JSON log files -> Alloy -> Loki -> Grafana. Replace the metrics line with Application roles -> Prometheus -> Grafana label "Metrics · :9091 · Bearer"; use SMALL NOTE "Prometheus scrapes the application directly" so metrics data direction is clear. The monitoring Grafana and input Grafana are the same service, add "(same Grafana)" to input box if space.
Remove old wrong arrows completely, including lines from Postgres to Slack and from Reactor to Ollama. No crossed lines. Grouping optional features with dashed borders is sufficient. Legend "Solid: data flow · Dashed: optional route". Keep footer "Implementation overview · validation scope: docs/RELEASE-LIMITS.md". Do not add future systems, guarantees, credentials, real channel IDs or local machine paths.
```

## 최종 선 표기 수정 프롬프트

```text
Use case: infographic-diagram. Precise edit of this architecture diagram: change ONLY the two short vertical bidirectional connectors from Worker to Postgres and from Worker to Ollama from dashed blue strokes to SOLID blue strokes, preserving arrowheads at both ends. These are core runtime dependencies and must not look like the dashed optional n8n route. Preserve absolutely everything else including all text, layout, icons, arrows, dimensions, white background and colors. No other changes.
```
