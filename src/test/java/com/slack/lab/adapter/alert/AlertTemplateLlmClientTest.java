package com.slack.lab.adapter.alert;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.slack.lab.core.model.ErrorCode;
import com.slack.lab.core.model.ErrorInfo;
import com.slack.lab.core.model.LlmMessage;
import com.slack.lab.core.model.LlmResult;
import com.slack.lab.core.port.LlmClient;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

class AlertTemplateLlmClientTest {

    private ListAppender<ILoggingEvent> appender;
    private final AtomicInteger calls = new AtomicInteger();
    private final AtomicLong lastBudget = new AtomicLong();

    @BeforeEach
    void attach() {
        appender = new ListAppender<>();
        appender.start();
        ((Logger) LoggerFactory.getLogger(AlertTemplateLlmClient.class)).addAppender(appender);
    }

    @AfterEach
    void detach() {
        ((Logger) LoggerFactory.getLogger(AlertTemplateLlmClient.class)).detachAppender(appender);
    }

    private LlmClient delegate(LlmResult result) {
        return (messages, remainingMs) -> {
            calls.incrementAndGet();
            lastBudget.set(remainingMs);
            return result;
        };
    }

    /** 정규화기가 만든 이벤트 본문 그대로(개행·공백이 접히기 전)다. */
    private static String prompt(String rule) {
        var e = new com.slack.lab.core.model.AlertEvent("grafana", "grafana-x", "[grafana:" + rule + "]",
                "규칙=" + rule + " 심각도=critical 역할=worker 값=3 시각=2026-10-15T01:00:00Z", "C", java.util.Map.of());
        return e.toMessageEvent().text();
    }

    private static List<LlmMessage> user(String text) {
        return List.of(LlmMessage.user(text));
    }

    private List<String> fallbackLines() {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).filter(m -> m.contains("알람 템플릿 폴백")).toList();
    }

    @Test
    void 규칙_2와_4는_위임_없이_템플릿이다() {
        var client = new AlertTemplateLlmClient(delegate(new LlmResult.Success("모델 답", 1)));

        for (String rule : List.of("llm_final_failure", "app_unobservable")) {
            var r = client.chat(user(prompt(rule)), 50_000);
            assertThat(r).isInstanceOf(LlmResult.Success.class);
            assertThat(((LlmResult.Success) r).text()).startsWith("[자동 요약]").contains("규칙=" + rule);
        }
        assertThat(calls).hasValue(0);
        assertThat(fallbackLines()).isEmpty();
    }

    @Test
    void 공백이_접힌_입력도_식별한다() {
        var client = new AlertTemplateLlmClient(delegate(new LlmResult.Success("모델 답", 1)));
        String collapsed = prompt("llm_final_failure").replaceAll("\\s+", " ");

        assertThat(((LlmResult.Success) client.chat(user(collapsed), 50_000)).text()).startsWith("[자동 요약]");
        assertThat(calls).hasValue(0);
    }

    @Test
    void 규칙_1과_3은_위임을_한_번만_하고_성공이면_폴백_줄이_없다() {
        var client = new AlertTemplateLlmClient(delegate(new LlmResult.Success("모델 답", 1)));

        var r = client.chat(user(prompt("recovery_needed")), 50_000);

        assertThat(((LlmResult.Success) r).text()).isEqualTo("모델 답");
        assertThat(calls).hasValue(1);
        assertThat(lastBudget.get()).isEqualTo(10_000); // 남은 시간이 넉넉해도 10초가 상한이다
        assertThat(fallbackLines()).isEmpty();
    }

    @Test
    void 남은_시간이_10초보다_짧으면_그_시간을_따른다() {
        var client = new AlertTemplateLlmClient(delegate(new LlmResult.Success("모델 답", 1)));

        client.chat(user(prompt("queue_backlog")), 4_000);

        assertThat(lastBudget.get()).isEqualTo(4_000);
    }

    @Test
    void 위임이_실패하거나_시간_초과면_템플릿과_폴백_줄_한_줄이다() {
        var failed = new AlertTemplateLlmClient(delegate(new LlmResult.Failed(ErrorInfo.of(ErrorCode.LLM_CONNECT_FAILED), 5, true)));
        var r = failed.chat(user(prompt("recovery_needed")), 50_000);
        assertThat(((LlmResult.Success) r).text()).startsWith("[자동 요약]");
        assertThat(fallbackLines()).singleElement().asString().contains("rule=recovery_needed").contains("reason=");

        appender.list.clear();
        var timedOut = new AlertTemplateLlmClient(delegate(new LlmResult.TimedOut(10_000)));
        assertThat(((LlmResult.Success) timedOut.chat(user(prompt("queue_backlog")), 50_000)).text()).startsWith("[자동 요약]");
        assertThat(fallbackLines()).singleElement().asString().contains("reason=timeout");
    }

    @Test
    void 알람이_아니거나_규칙이_4종_밖이면_그대로_위임한다() {
        var client = new AlertTemplateLlmClient(delegate(new LlmResult.Success("그대로", 1)));

        assertThat(((LlmResult.Success) client.chat(user("일반 질문"), 9_000)).text()).isEqualTo("그대로");
        assertThat(((LlmResult.Success) client.chat(user(prompt("zzz_unknown")), 9_000)).text()).isEqualTo("그대로");
        assertThat(calls).hasValue(2);
        assertThat(lastBudget.get()).isEqualTo(9_000);
    }

    @Test
    void 템플릿에_알람_원문_주석_지시가_들어가지_않는다() {
        var client = new AlertTemplateLlmClient(delegate(new LlmResult.Success("x", 1)));
        String hostile = prompt("llm_final_failure").replace("값=3", "값=3 주입 시도 지시");

        String text = ((LlmResult.Success) client.chat(user(hostile), 50_000)).text();

        assertThat(text).doesNotContain("주입").doesNotContain("event_id").doesNotContain("xox");
    }
}
