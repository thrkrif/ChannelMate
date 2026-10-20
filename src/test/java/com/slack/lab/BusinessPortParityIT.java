package com.slack.lab;

import static org.assertj.core.api.Assertions.assertThat;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.context.ConfigurableApplicationContext;
import org.testcontainers.containers.RabbitMQContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

/**
 * 6단계 M45(C2 3·6): 관리 토큰을 켜도 업무 포트(수신 8080에 해당)의 경로별 상태 코드·응답이 토큰이 없을 때와 같다.
 * 업무 포트의 /actuator/**는 어느 쪽이든 404다.
 */
@Testcontainers
class BusinessPortParityIT {

    @Container
    static final RabbitMQContainer RABBIT = new RabbitMQContainer("rabbitmq:3.13-alpine");

    private static final String TOKEN = "0123456789abcdef".repeat(4);
    private static final HttpClient HTTP = HttpClient.newHttpClient();

    private static ConfigurableApplicationContext receiver(String token) {
        List<String> args = new ArrayList<>(List.of("--app.role=receiver", "--server.port=0", "--slacklab.management.port=0",
                "--slack.signing-secret=s", "--slack.bot-token=t", "--llm.model=m", "--llm.client=echo",
                "--rabbitmq.host=" + RABBIT.getHost(), "--rabbitmq.port=" + RABBIT.getAmqpPort(),
                "--rabbitmq.events-queue=parity.events", "--logging.file.name=build/test-logs/parity.log"));
        if (token != null) {
            args.add("--slacklab.management.token=" + token);
        }
        return new SpringApplicationBuilder(SlackLabApplication.class).run(args.toArray(String[]::new));
    }

    private static String probe(int port, String method, String path) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + path));
        b.method(method, method.equals("POST") ? HttpRequest.BodyPublishers.ofString("{}") : HttpRequest.BodyPublishers.noBody());
        HttpResponse<String> r = HTTP.send(b.build(), HttpResponse.BodyHandlers.ofString());
        // 404 본문의 시각은 요청마다 달라 마스킹한다.
        return method + " " + path + " -> " + r.statusCode() + " " + r.body().replaceAll("\"timestamp\":\"[^\"]*\"", "\"timestamp\":\"-\"");
    }

    private static List<String> probeAll(int port) throws Exception {
        List<String> out = new ArrayList<>();
        for (String[] p : new String[][] {{"GET", "/health"}, {"POST", "/slack/events"}, {"POST", "/alerts/cloudwatch"},
                {"GET", "/actuator"}, {"GET", "/actuator/prometheus"}, {"GET", "/actuator/health"}}) {
            out.add(probe(port, p[0], p[1]));
        }
        return out;
    }

    @Test
    void 토큰_유무와_상관없이_업무_포트_응답이_같다() throws Exception {
        List<String> without;
        try (ConfigurableApplicationContext ctx = receiver(null)) {
            assertThat(ctx.getEnvironment().getProperty("local.management.port")).isNull();
            without = probeAll(ctx.getEnvironment().getProperty("local.server.port", Integer.class));
        }
        List<String> with;
        try (ConfigurableApplicationContext ctx = receiver(TOKEN)) {
            assertThat(ctx.getEnvironment().getProperty("local.management.port", Integer.class)).isPositive();
            with = probeAll(ctx.getEnvironment().getProperty("local.server.port", Integer.class));
        }
        assertThat(with).isEqualTo(without);
        assertThat(without).anyMatch(l -> l.startsWith("GET /actuator/prometheus -> 404"));
        assertThat(without).anyMatch(l -> l.startsWith("GET /health -> 200"));
    }

    @Test
    void 위험한_관리_설정은_기동을_거부하고_토큰을_출력하지_않는다() {
        // 관리 포트 = 업무 포트(server.port), 관리 포트 8080, 토큰 형식 오류 — 각각 기동 실패
        String bad = "g".repeat(64);
        for (String[] extra : new String[][] {
                {"--server.port=18099", "--slacklab.management.port=18099", "--slacklab.management.token=" + TOKEN},
                {"--slacklab.management.port=8080", "--slacklab.management.token=" + TOKEN},
                {"--slacklab.management.token=" + bad}}) {
            List<String> args = new ArrayList<>(List.of("--app.role=receiver", "--server.port=0", "--slacklab.management.port=0",
                    "--slack.signing-secret=s", "--slack.bot-token=t", "--llm.model=m", "--llm.client=echo",
                    "--rabbitmq.host=" + RABBIT.getHost(), "--rabbitmq.port=" + RABBIT.getAmqpPort(),
                    "--rabbitmq.events-queue=parity.events", "--logging.file.name=build/test-logs/parity.log"));
            for (String e : extra) {
                String key = e.substring(0, e.indexOf('=') + 1);
                args.removeIf(a -> a.startsWith(key));
                args.add(e);
            }
            org.assertj.core.api.Assertions.assertThatThrownBy(
                    () -> new SpringApplicationBuilder(SlackLabApplication.class).run(args.toArray(String[]::new)).close())
                    .hasStackTraceContaining("관리 서버 설정 위반")
                    .satisfies(t -> {
                        StringBuilder all = new StringBuilder();
                        for (Throwable c = t; c != null; c = c.getCause()) {
                            all.append(c.getMessage());
                        }
                        assertThat(all.toString()).doesNotContain(TOKEN).doesNotContain(bad);
                    });
        }
    }
}
