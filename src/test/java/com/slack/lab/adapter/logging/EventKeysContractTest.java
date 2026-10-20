package com.slack.lab.adapter.logging;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * 판별 문자열이 코어 로그 원문에 그대로 있는지 소스에서 확인한다(N9). 코어 문구가 바뀌면 여기서 먼저 깨져,
 * 사건 키 매핑이 조용히 어긋나(대시보드가 0을 보여주는 일) 지나가지 않는다.
 */
class EventKeysContractTest {

    private static final Path CORE = Path.of("src/main/java/com/slack/lab/core/service");

    private String source(String file) throws Exception {
        return Files.readString(CORE.resolve(file));
    }

    @Test
    void 판별_문자열이_코어_소스_원문에_있다() throws Exception {
        assertThat(source("BacklogReporter.java")).contains("\"적체 스냅샷\"").contains("\"적체 스냅샷 실패 reason={}\"");
        assertThat(source("EventProcessor.java")).contains("\"처리 지표 event_id={}");
        assertThat(source("SlackEventHandler.java")).contains("\"LLM 실패 → 실패 안내로 전환 event_id={}");
        assertThat(source("RetrievalService.java")).contains("\"metric=rag_result result=found");
    }

    @Test
    void 분류표는_p1_metrics의_판별과_같은_결과를_낸다() {
        Map<String, String> expected = new java.util.LinkedHashMap<>();
        expected.put("metric=rag_result result=found elapsed_ms=3", "rag_result");
        expected.put("처리 지표 event_id=E gen=0", "processing_metric");
        expected.put("적체 스냅샷 실패 reason=X", "backlog_unobserved");
        expected.put("적체 스냅샷", "backlog_unobserved");
        expected.put("적체 스냅샷 queue_ready=1", "backlog_snapshot");
        expected.put("LLM 실패 → 실패 안내로 전환 event_id=E", "llm_final_failure");
        expected.put("처리 종료 event_id=E", null);
        expected.forEach((msg, event) -> assertThat(EventKeys.eventOf(msg)).as(msg).isEqualTo(event));
    }

    @Test
    void fields_허용_키에_필수_항목이_모두_있다() {
        assertThat(EventKeys.FIELD_KEYS).contains("queue_ready", "defer", "dead", "retry", "dlq", "recovery",
                "queue_wait_ms", "llm_ms", "send_ms", "answer_ms", "result", "kind", "elapsed_ms", "reason",
                "gen", "retries", "manual_run", "finalized", "negative_interval", "received_at_missing");
        assertThat(EventKeys.FIELD_KEYS).doesNotContain("event_id", "attempt_id", "channel", "question", "error");
    }
}
