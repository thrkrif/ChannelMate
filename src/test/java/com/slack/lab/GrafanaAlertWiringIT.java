package com.slack.lab;

import static org.assertj.core.api.Assertions.assertThat;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.sun.net.httpserver.HttpServer;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.client.TestRestTemplate;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.containers.RabbitMQContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * Grafana 알람의 종단 배선(M47): 실제 {@code AlertController} HTTP → RabbitMQ → 워커 핸들러 → 실제 {@code SlackClient} → 가짜 Slack
 * 서버. 알림기를 목으로 바꾸지 않는다 — 데코레이터(@Primary)가 실제 경로에서 일하는지가 이 시험의 대상이다. RAG를 켜고 임베딩
 * 서버는 항상 500으로 둬서, 알람이 임베딩을 우회하는지(요청 0)와 일반 멘션은 검색을 시도하는지(안내 푸터)를 가른다.
 */
@Testcontainers
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT, properties = {
        "app.role=all", "llm.client=echo", "llm.model=m", "alert.secret=alertsecret", "alert.channel=C-ALERT",
        "slack.signing-secret=testsecret", "slack.bot-token=xoxb-test", "recovery.auto-check.enabled=false",
        "rabbitmq.eventsQueue=grafana.wiring.events", "rag.enabled=true", "rag.embedding-model=bge-m3",
        "rag.embedding-dimension=1024", "rag.verify-on-startup=false"})
class GrafanaAlertWiringIT {

    @Container
    static final RabbitMQContainer RABBIT = new RabbitMQContainer("rabbitmq:3.13-alpine");

    @Container
    static final PostgreSQLContainer<?> PG = new PostgreSQLContainer<>(TestImages.POSTGRES);

    /** chat.postMessage 본문(JSON 문자열)을 모은다. 응답은 모드에 따라 성공 또는 명확한 실패. */
    static final List<String> POSTED = new CopyOnWriteArrayList<>();
    static final AtomicInteger EMBED_REQUESTS = new AtomicInteger();
    static final AtomicReference<String> SLACK_REPLY = new AtomicReference<>("{\"ok\":true,\"ts\":\"1.1\"}");
    static HttpServer slackStub;
    static HttpServer embedStub;

    static {
        try {
            slackStub = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
            slackStub.createContext("/", ex -> {
                String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
                if (ex.getRequestURI().getPath().endsWith("chat.postMessage")) {
                    POSTED.add(body);
                }
                byte[] b = (ex.getRequestURI().getPath().endsWith("chat.postMessage") ? SLACK_REPLY.get() : "{\"ok\":true}")
                        .getBytes(StandardCharsets.UTF_8);
                ex.getResponseHeaders().add("Content-Type", "application/json");
                ex.sendResponseHeaders(200, b.length);
                try (var out = ex.getResponseBody()) {
                    out.write(b);
                }
            });
            slackStub.start();
            embedStub = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
            embedStub.createContext("/", ex -> {
                ex.getRequestBody().readAllBytes();
                EMBED_REQUESTS.incrementAndGet();
                ex.sendResponseHeaders(500, -1);
                ex.close();
            });
            embedStub.start();
        } catch (java.io.IOException e) {
            throw new IllegalStateException(e);
        }
    }

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("rabbitmq.host", RABBIT::getHost);
        r.add("rabbitmq.port", RABBIT::getAmqpPort);
        r.add("postgres.url", PG::getJdbcUrl);
        r.add("postgres.username", PG::getUsername);
        r.add("postgres.password", PG::getPassword);
        r.add("slack.base-url", () -> "http://127.0.0.1:" + slackStub.getAddress().getPort());
        r.add("rag.embedding-base-url", () -> "http://127.0.0.1:" + embedStub.getAddress().getPort() + "/v1");
    }

    @AfterAll
    static void stop() {
        slackStub.stop(0);
        embedStub.stop(0);
    }

    @Autowired
    TestRestTemplate rest;

    JdbcTemplate jdbc;

    @BeforeEach
    void reset() {
        POSTED.clear();
        EMBED_REQUESTS.set(0);
        SLACK_REPLY.set("{\"ok\":true,\"ts\":\"1.1\"}");
        jdbc = new JdbcTemplate(new org.springframework.jdbc.datasource.DriverManagerDataSource(PG.getJdbcUrl(),
                PG.getUsername(), PG.getPassword()));
    }

    private static String alert(String rule, String severity, String role, String startsAt) {
        return "{\"status\":\"firing\",\"labels\":{\"alertname\":\"x\",\"slack_lab_rule\":\"" + rule + "\",\"severity\":\"" + severity
                + "\",\"role\":\"" + role + "\"},\"startsAt\":\"" + startsAt + "\",\"values\":{\"C\":4}}";
    }

    private static String grafanaBody(String... alerts) {
        return "{\"externalURL\":\"http://localhost:3000/\",\"alerts\":[" + String.join(",", alerts) + "]}";
    }

    private int postGrafana(String body) {
        HttpHeaders h = new HttpHeaders();
        h.setContentType(MediaType.APPLICATION_JSON);
        h.set("X-Alert-Secret", "alertsecret");
        return rest.postForEntity("/alerts/grafana", new HttpEntity<>(body, h), String.class).getStatusCode().value();
    }

    private List<String> awaitPosted(int n, long timeoutMs) throws InterruptedException {
        long end = System.currentTimeMillis() + timeoutMs;
        while (POSTED.size() < n && System.currentTimeMillis() < end) {
            Thread.sleep(100);
        }
        return List.copyOf(POSTED);
    }

    private ListAppender<ILoggingEvent> attach(Class<?> type) {
        var appender = new ListAppender<ILoggingEvent>();
        appender.start();
        ((Logger) LoggerFactory.getLogger(type)).addAppender(appender);
        return appender;
    }

    @Test
    void 규칙_네_개_알람은_리포트_네_개_규칙2와4는_템플릿_검색_우회_푸터_없음() throws Exception {
        var rag = attach(com.slack.lab.core.service.RetrievalService.class);
        try {
            String t = "2026-10-15T0" + (System.nanoTime() % 9) + ":00:00Z";
            assertThat(postGrafana(grafanaBody(
                    alert("recovery_needed", "critical", "worker", t),
                    alert("llm_final_failure", "warning", "-", t),
                    alert("queue_backlog", "warning", "all", t),
                    alert("app_unobservable", "critical", "receiver", t)))).isEqualTo(200);

            List<String> posted = awaitPosted(4, 20_000);

            assertThat(posted).hasSize(4);
            // ①③은 echo(원본 LLM) 위임, ②④는 템플릿 — echo가 아예 안 불렸음을 본문으로 가른다
            assertThat(posted.stream().filter(b -> b.contains("[자동 요약]")).count()).isEqualTo(2);
            assertThat(posted.stream().filter(b -> b.contains("echo:")).count()).isEqualTo(2);
            assertThat(posted).allSatisfy(b -> assertThat(b)
                    .doesNotContain("관련 문서 근거를 찾지 못해").doesNotContain("문서 검색을 완료하지 못해").doesNotContain("참고 문서"));
            assertThat(EMBED_REQUESTS).hasValue(0);
            assertThat(rag.list.stream().filter(e -> e.getFormattedMessage().contains("metric=rag_result")).count()).isZero();
            // 규칙 ②(role "-")도 200이었고 리포트가 1개 있다
            assertThat(posted.stream().filter(b -> b.contains("llm_final_failure")).count()).isEqualTo(1);
        } finally {
            ((Logger) LoggerFactory.getLogger(com.slack.lab.core.service.RetrievalService.class)).detachAppender(rag);
        }
    }

    @Test
    void 일반_멘션은_검색을_시도하고_안내_푸터가_붙으며_rag_result_한_줄이다() throws Exception {
        var rag = attach(com.slack.lab.core.service.RetrievalService.class);
        try {
            String body = "{\"type\":\"event_callback\",\"event_id\":\"EvGraf1\",\"authorizations\":[{\"user_id\":\"UBOT\"}],"
                    + "\"event\":{\"type\":\"app_mention\",\"channel\":\"C1\",\"user\":\"U1\",\"text\":\"<@UBOT> 메모리 누수가 뭐야\","
                    + "\"ts\":\"200.1\"}}";
            String ts = String.valueOf(System.currentTimeMillis() / 1000);
            HttpHeaders h = new HttpHeaders();
            h.setContentType(MediaType.APPLICATION_JSON);
            h.set("X-Slack-Request-Timestamp", ts);
            h.set("X-Slack-Signature", sign(body, ts));
            assertThat(rest.postForEntity("/slack/events", new HttpEntity<>(body, h), String.class).getStatusCode().value())
                    .isEqualTo(200);

            List<String> posted = awaitPosted(1, 20_000);

            assertThat(posted).hasSize(1);
            assertThat(posted.get(0)).contains("문서 검색을 완료하지 못해");
            assertThat(EMBED_REQUESTS.get()).isGreaterThanOrEqualTo(1);
            assertThat(rag.list.stream().filter(e -> e.getFormattedMessage().contains("metric=rag_result")).count()).isEqualTo(1);
        } finally {
            ((Logger) LoggerFactory.getLogger(com.slack.lab.core.service.RetrievalService.class)).detachAppender(rag);
        }
    }

    @Test
    void 발신이_명확히_실패하면_완료로_표시되지_않고_재시도_경로로_간다() throws Exception {
        SLACK_REPLY.set("{\"ok\":false,\"error\":\"channel_not_found\"}");
        String t = "2026-11-0" + (1 + System.nanoTime() % 9) + "T00:00:00Z";
        assertThat(postGrafana(grafanaBody(alert("queue_backlog", "warning", "worker", t)))).isEqualTo(200);

        awaitPosted(1, 20_000);
        Thread.sleep(1_500);

        List<String> states = jdbc.queryForList("SELECT state FROM processing_state WHERE event_id LIKE 'alert-grafana-%'",
                String.class);
        assertThat(states).isNotEmpty();
        assertThat(states).doesNotContain("COMPLETED");
    }

    static String sign(String body, String timestamp) throws Exception {
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec("testsecret".getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
        byte[] h = mac.doFinal(("v0:" + timestamp + ":" + body).getBytes(StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder("v0=");
        for (byte b : h) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }
}
