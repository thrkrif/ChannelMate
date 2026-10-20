package com.slack.mgmtspike;

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

/**
 * 6단계 M45 스파이크(R2)·통합 시험: 실제 역할 후처리기가 만든 구성에서 관리 컨텍스트가 별도 포트로 뜨고 인증 필터가 걸리는지,
 * 업무 포트는 열리지 않는지(워커·반응), 토큰이 없으면 관리 리스너가 없는지를 본다. 외부 인프라 없이 돈다.
 */
class ManagementServerTest {

    private static final String TOKEN = "0123456789abcdef".repeat(4);
    private static final HttpClient HTTP = HttpClient.newHttpClient();

    private static ConfigurableApplicationContext start(String role, String token) {
        List<String> args = new ArrayList<>(List.of("--app.role=" + role, "--slacklab.management.port=0",
                "--spring.main.banner-mode=off", "--logging.file.name=build/test-logs/mgmt-spike.log"));
        if (token != null) {
            args.add("--slacklab.management.token=" + token);
        }
        return new SpringApplicationBuilder(MgmtSpikeApp.class).run(args.toArray(String[]::new));
    }

    private static int status(int port, String method, String uri, String auth) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + uri))
                .method(method, HttpRequest.BodyPublishers.noBody());
        if (auth != null) {
            b.header("Authorization", auth);
        }
        return HTTP.send(b.build(), HttpResponse.BodyHandlers.discarding()).statusCode();
    }

    private static String body(int port, String auth) throws Exception {
        HttpRequest r = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/actuator/prometheus"))
                .header("Authorization", auth).build();
        return HTTP.send(r, HttpResponse.BodyHandlers.ofString()).body();
    }

    @Test
    void 워커는_업무_포트_없이_관리_포트만_열고_인증이_걸린다() throws Exception {
        try (ConfigurableApplicationContext ctx = start("worker", TOKEN)) {
            Integer mgmt = ctx.getEnvironment().getProperty("local.management.port", Integer.class);
            assertThat(mgmt).isNotNull().isPositive();
            assertThat(ctx.getEnvironment().getProperty("local.server.port", Integer.class)).isEqualTo(-1); // 업무 리스너 없음

            assertThat(status(mgmt, "GET", "/actuator/prometheus", "Bearer " + TOKEN)).isEqualTo(200);
            assertThat(body(mgmt, "Bearer " + TOKEN)).contains("role=\"worker\"").contains("application=\"slack-lab\"");
            for (int i = 0; i < 5; i++) {
                assertThat(status(mgmt, "GET", "/actuator/prometheus", null)).isEqualTo(401);
                assertThat(status(mgmt, "GET", "/actuator/prometheus", "Bearer wrong")).isEqualTo(401);
            }
            assertThat(status(mgmt, "GET", "/actuator/health", "Bearer " + TOKEN)).isEqualTo(404);
            assertThat(status(mgmt, "GET", "/actuator/env", "Bearer " + TOKEN)).isEqualTo(404);
            assertThat(status(mgmt, "POST", "/actuator/prometheus", "Bearer " + TOKEN)).isEqualTo(405);
        }
    }

    @Test
    void 반응도_같은_방식으로_role_태그가_다르다() throws Exception {
        try (ConfigurableApplicationContext ctx = start("reactor", TOKEN)) {
            int mgmt = ctx.getEnvironment().getProperty("local.management.port", Integer.class);
            assertThat(body(mgmt, "Bearer " + TOKEN)).contains("role=\"reactor\"");
        }
    }

    @Test
    void 수신은_업무_포트와_관리_포트가_따로_열리고_업무_포트에는_엔드포인트가_없다() throws Exception {
        try (ConfigurableApplicationContext ctx = new SpringApplicationBuilder(MgmtSpikeApp.class).run(
                "--app.role=receiver", "--server.port=0", "--slacklab.management.port=0",
                "--slacklab.management.token=" + TOKEN, "--spring.main.banner-mode=off",
                "--logging.file.name=build/test-logs/mgmt-spike.log")) {
            int mgmt = ctx.getEnvironment().getProperty("local.management.port", Integer.class);
            int biz = ctx.getEnvironment().getProperty("local.server.port", Integer.class);
            assertThat(mgmt).isNotEqualTo(biz);
            assertThat(status(mgmt, "GET", "/actuator/prometheus", "Bearer " + TOKEN)).isEqualTo(200);
            assertThat(status(biz, "GET", "/actuator/prometheus", "Bearer " + TOKEN)).isEqualTo(404);
            assertThat(status(biz, "GET", "/actuator", null)).isEqualTo(404);
            assertThat(status(biz, "GET", "/actuator/health", null)).isEqualTo(404);
        }
    }

    @Test
    void 토큰이_없으면_관리_리스너가_없고_워커는_웹_서버가_없다() {
        try (ConfigurableApplicationContext ctx = start("worker", null)) {
            assertThat(ctx.getEnvironment().getProperty("local.management.port")).isNull();
            assertThat(ctx.getEnvironment().getProperty("local.server.port")).isNull();
            assertThat(ctx).isNotInstanceOf(org.springframework.web.context.WebApplicationContext.class);
        }
    }

    @Test
    void 토큰이_없는_수신은_엔드포인트를_어디에도_열지_않는다() throws Exception {
        try (ConfigurableApplicationContext ctx = new SpringApplicationBuilder(MgmtSpikeApp.class).run(
                "--app.role=receiver", "--server.port=0", "--slacklab.management.port=0",
                "--spring.main.banner-mode=off", "--logging.file.name=build/test-logs/mgmt-spike.log")) {
            assertThat(ctx.getEnvironment().getProperty("local.management.port")).isNull();
            int biz = ctx.getEnvironment().getProperty("local.server.port", Integer.class);
            assertThat(status(biz, "GET", "/actuator/prometheus", null)).isEqualTo(404);
            assertThat(status(biz, "GET", "/actuator/prometheus", "Bearer " + TOKEN)).isEqualTo(404);
        }
    }

    @Test
    void CLI_역할은_토큰이_있어도_관리_리스너가_없고_기동은_성공한다() {
        for (String role : new String[] {"recovery", "indexer", "evaluator"}) {
            try (ConfigurableApplicationContext ctx = start(role, TOKEN)) {
                assertThat(ctx.getEnvironment().getProperty("local.management.port")).as(role).isNull();
                assertThat(ctx.getEnvironment().getProperty("local.server.port")).as(role).isNull();
                assertThat(ctx).as(role).isNotInstanceOf(org.springframework.web.context.WebApplicationContext.class);
            }
        }
    }
}
