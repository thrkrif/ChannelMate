package com.slack.lab.core.service;

import static com.slack.lab.core.service.RagTestFakes.config;
import static com.slack.lab.core.service.RagTestFakes.embedding;
import static com.slack.lab.core.service.RagTestFakes.hit;
import static com.slack.lab.core.service.RagTestFakes.okEmbedding;
import static com.slack.lab.core.service.RagTestFakes.storeReturning;
import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.slack.lab.config.AlertAwareRetrievalService;
import com.slack.lab.core.model.AlertEvent;
import com.slack.lab.core.model.EmbeddingResult;
import com.slack.lab.core.model.Retrieval;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

/** 알람 우회: 임베딩 0, {@code rag_result} 0줄. 비알람은 호출당 정확히 1줄(5단계 계약 유지). */
class AlertAwareRetrievalServiceTest {

    private static String alertPrompt() {
        return new AlertEvent("grafana", "grafana-x", "[grafana:queue_backlog]",
                "규칙=queue_backlog 심각도=warning 역할=worker 값=3 시각=2026-10-15T01:00:00Z", "C", java.util.Map.of())
                .toMessageEvent().text().replaceAll("\\s+", " ");
    }

    private List<String> metricLines(ListAppender<ILoggingEvent> appender) {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).filter(m -> m.contains("metric=rag_result")).toList();
    }

    @Test
    void 그라파나_알람은_임베딩_없이_NoRelevant이고_rag_result_줄이_없다() {
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        ((Logger) LoggerFactory.getLogger(RetrievalService.class)).addAppender(appender);
        try {
            var embeds = new AtomicInteger();
            var svc = new AlertAwareRetrievalService(embedding(t -> {
                embeds.incrementAndGet();
                return new EmbeddingResult.Success(new float[] {1f}, 1);
            }), storeReturning(hit("A", "제목", "본문", 0.9)), config());

            assertThat(svc.retrieve(alertPrompt(), 50_000)).isInstanceOf(Retrieval.NoRelevant.class);
            assertThat(embeds).hasValue(0);
            assertThat(metricLines(appender)).isEmpty();
        } finally {
            ((Logger) LoggerFactory.getLogger(RetrievalService.class)).detachAppender(appender);
        }
    }

    @Test
    void 비알람과_CloudWatch_알람은_기존대로_정확히_한_줄이다() {
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        ((Logger) LoggerFactory.getLogger(RetrievalService.class)).addAppender(appender);
        try {
            var svc = new AlertAwareRetrievalService(okEmbedding(), storeReturning(hit("A", "제목", "본문", 0.9)), config());
            String cloudWatch = new AlertEvent("cloudwatch", "k", "web-cpu", "임계치 초과", "C", java.util.Map.of()).toMessageEvent().text();

            assertThat(svc.retrieve("메모리 누수 원인이 뭐야", 50_000)).isInstanceOf(Retrieval.Found.class);
            assertThat(svc.retrieve(cloudWatch, 50_000)).isInstanceOf(Retrieval.Found.class);

            assertThat(metricLines(appender)).hasSize(2);
        } finally {
            ((Logger) LoggerFactory.getLogger(RetrievalService.class)).detachAppender(appender);
        }
    }
}
