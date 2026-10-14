package com.slack.lab.adapter.logging;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.spi.LoggingEvent;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import org.springframework.mock.env.MockEnvironment;

/** 6단계 M44(C1): 한 줄 JSON, 필수 필드, 한국어 원문 보존, 예외 한 줄, 금지 정보 없음. */
class SlackLabJsonFormatterTest {

    private static final ObjectMapper JSON = new ObjectMapper();
    private final SlackLabJsonFormatter formatter = new SlackLabJsonFormatter(new MockEnvironment().withProperty("app.role", "worker"));
    private final Logger logger = ((LoggerContext) LoggerFactory.getILoggerFactory()).getLogger("com.slack.lab.Sample");

    private JsonNode parse(String line) throws Exception {
        assertThat(line).endsWith("}\n");
        assertThat(line.substring(0, line.length() - 1)).doesNotContain("\n");
        return JSON.readTree(line);
    }

    private String format(Level level, String msg, Throwable t, Object... args) {
        return formatter.format(new LoggingEvent(Logger.class.getName(), logger, level, msg, t, args));
    }

    @Test
    void 필수_필드가_모두_있고_한국어는_이스케이프되지_않는다() throws Exception {
        String line = format(Level.INFO, "수신 완료 {}", null, "값");
        JsonNode n = parse(line);

        assertThat(n.get("v").asInt()).isEqualTo(1);
        assertThat(n.get("ts").asText()).matches("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z");
        assertThat(n.get("level").asText()).isEqualTo("INFO");
        assertThat(n.get("role").asText()).isEqualTo("worker");
        assertThat(n.get("logger").asText()).isEqualTo("com.slack.lab.Sample");
        assertThat(n.get("msg").asText()).isEqualTo("수신 완료 값");
        assertThat(line).contains("수신 완료 값").doesNotContain("\\uc218");
        assertThat(n.has("event")).isFalse();
        assertThat(n.has("exc")).isFalse();
    }

    @Test
    void 예외도_한_줄이고_개행은_이스케이프된다() throws Exception {
        String line = format(Level.ERROR, "실패", new IllegalStateException("줄1\n줄2"));
        JsonNode n = parse(line);

        assertThat(n.get("exc").asText()).startsWith("java.lang.IllegalStateException: ").contains("줄1").contains("줄2");
    }

    @Test
    void 처리_지표_줄은_사건_키와_허용_필드만_담고_event_id는_fields에_없다() throws Exception {
        String msg = "처리 지표 event_id=Ev123 attempt_id=a-1 gen=0 retries=1 manual_run=false result=Delivered kind=answer "
                + "finalized=true queue_wait_ms=12 llm_ms=3400 send_ms=80 answer_ms=3600 received_at_missing=false negative_interval=false";
        JsonNode n = parse(format(Level.INFO, msg, null));

        assertThat(n.get("event").asText()).isEqualTo("processing_metric");
        JsonNode f = n.get("fields");
        assertThat(f.get("llm_ms").asLong()).isEqualTo(3400);
        assertThat(f.get("result").asText()).isEqualTo("Delivered");
        assertThat(f.get("manual_run").asBoolean()).isFalse();
        assertThat(f.get("gen").asLong()).isZero();
        assertThat(f.has("event_id")).isFalse();
        assertThat(f.has("attempt_id")).isFalse();
        // 원문 msg는 기존 동작으로 보존한다
        assertThat(n.get("msg").asText()).contains("event_id=Ev123");
    }

    @Test
    void 적체_스냅샷은_있는_키만_담고_빠진_키를_0으로_채우지_않는다() throws Exception {
        JsonNode full = parse(format(Level.INFO, "적체 스냅샷 queue_ready=3 defer=0 dead=0 retry=1 dlq=0 recovery=2", null));
        JsonNode partial = parse(format(Level.INFO, "적체 스냅샷 queue_ready=3 dlq=1", null));

        assertThat(full.get("event").asText()).isEqualTo("backlog_snapshot");
        assertThat(full.get("fields").get("recovery").asLong()).isEqualTo(2);
        assertThat(partial.get("event").asText()).isEqualTo("backlog_snapshot");
        assertThat(partial.get("fields").has("recovery")).isFalse();
        assertThat(partial.get("fields").has("defer")).isFalse();
    }

    @Test
    void 관측_실패_두_줄은_backlog_unobserved이고_reason만_담는다() throws Exception {
        JsonNode failed = parse(format(Level.WARN, "적체 스냅샷 실패 reason=SQLException", null));
        JsonNode keyless = parse(format(Level.INFO, "적체 스냅샷", null));

        assertThat(failed.get("event").asText()).isEqualTo("backlog_unobserved");
        assertThat(failed.get("fields").get("reason").asText()).isEqualTo("SQLException");
        assertThat(failed.get("fields").size()).isEqualTo(1);
        assertThat(keyless.get("event").asText()).isEqualTo("backlog_unobserved");
        assertThat(keyless.has("fields")).isFalse();
    }

    @Test
    void LLM_최종_실패_줄은_error_본문을_fields에_넣지_않는다() throws Exception {
        JsonNode n = parse(format(Level.WARN, "LLM 실패 → 실패 안내로 전환 event_id=Ev1 attempt_id=a error=질문 본문 final_attempt=true", null));

        assertThat(n.get("event").asText()).isEqualTo("llm_final_failure");
        assertThat(n.get("fields").has("error")).isFalse();
        assertThat(n.get("fields").get("final_attempt").asBoolean()).isTrue();
    }

    @Test
    void rag_result_줄은_found_none_unavailable과_reason을_담는다() throws Exception {
        JsonNode n = parse(format(Level.INFO, "metric=rag_result result=unavailable reason=embedding_timeout elapsed_ms=5000", null));

        assertThat(n.get("event").asText()).isEqualTo("rag_result");
        assertThat(n.get("fields").get("reason").asText()).isEqualTo("embedding_timeout");
        assertThat(n.get("fields").get("elapsed_ms").asLong()).isEqualTo(5000);
    }

    @Test
    void 허용_키라도_값이_숫자_불리언_열거값이_아니면_버린다() throws Exception {
        JsonNode n = parse(format(Level.INFO, "metric=rag_result result=found reason=질문/본문 elapsed_ms=abc", null));

        assertThat(n.get("fields").has("reason")).isFalse();
        assertThat(n.get("fields").has("elapsed_ms")).isFalse();
        assertThat(n.get("fields").get("result").asText()).isEqualTo("found");
    }

    @Test
    void 따옴표_역슬래시_제어문자가_있어도_JSON이_유효하다() throws Exception {
        JsonNode n = parse(format(Level.INFO, "따옴표 \" 역슬래시 \\ 탭\t 제어\u0001", null));

        assertThat(n.get("msg").asText()).isEqualTo("따옴표 \" 역슬래시 \\ 탭\t 제어\u0001");
    }
}
