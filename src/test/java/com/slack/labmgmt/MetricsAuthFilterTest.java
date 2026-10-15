package com.slack.labmgmt;

import static org.assertj.core.api.Assertions.assertThat;

import jakarta.servlet.http.HttpServletResponse;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;

/** 6단계 M45(C2·C3): 관리 포트 필터는 인증을 먼저 보고, 허용은 GET 정확 경로 하나뿐이다. */
class MetricsAuthFilterTest {

    private static final String TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    private final MetricsAuthFilter filter = new MetricsAuthFilter(TOKEN);

    private record Result(int status, boolean passed) {}

    private Result call(String method, String uri, String authorization) throws Exception {
        MockHttpServletRequest req = new MockHttpServletRequest(method, uri);
        req.setRequestURI(uri);
        if (authorization != null) {
            req.addHeader("Authorization", authorization);
        }
        MockHttpServletResponse res = new MockHttpServletResponse();
        MockFilterChain chain = new MockFilterChain();
        filter.doFilter(req, res, chain);
        return new Result(res.getStatus(), chain.getRequest() != null);
    }

    @Test
    void 유효한_토큰의_GET은_통과한다() throws Exception {
        Result r = call("GET", "/actuator/prometheus", "Bearer " + TOKEN);
        assertThat(r.passed()).isTrue();
        assertThat(r.status()).isEqualTo(200);
    }

    @Test
    void 인증이_없거나_틀리면_경로와_상관없이_401이다() throws Exception {
        String[] auths = {null, "", "Bearer", "Bearer ", "Bearer wrong", "Basic " + TOKEN, TOKEN,
                "bearer " + TOKEN, "BEARER " + TOKEN, "Bearer  " + TOKEN, "Bearer " + TOKEN + " ", "Bearer " + TOKEN + "x",
                "Bearer " + "a".repeat(10_000)};
        for (String auth : auths) {
            for (String uri : new String[] {"/actuator/prometheus", "/actuator", "/actuator/env", "/"}) {
                Result r = call("GET", uri, auth);
                assertThat(r.status()).as("auth=%s uri=%s", auth == null ? "null" : "len" + auth.length(), uri)
                        .isEqualTo(HttpServletResponse.SC_UNAUTHORIZED);
                assertThat(r.passed()).isFalse();
            }
        }
    }

    @Test
    void 유효한_토큰이어도_허용_경로_말고는_404이고_본문에_닿지_않는다() throws Exception {
        String[] uris = {"/actuator", "/actuator/", "/actuator/health", "/actuator/env", "/actuator/configprops",
                "/actuator/heapdump", "/actuator/threaddump", "/actuator/prometheus/../env", "/actuator/prometheus/",
                "/actuator/%2e%2e/env", "/actuator/prometheus%2f", "/actuator/prometheus;x=1", "/actuator//prometheus",
                "/Actuator/prometheus", "/actuator/prometheus.json", "/"};
        for (String uri : uris) {
            Result r = call("GET", uri, "Bearer " + TOKEN);
            assertThat(r.status()).as(uri).isEqualTo(HttpServletResponse.SC_NOT_FOUND);
            assertThat(r.passed()).as(uri).isFalse();
        }
    }

    @Test
    void 유효한_토큰이어도_GET_말고는_405이다() throws Exception {
        for (String method : new String[] {"POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE"}) {
            Result r = call(method, "/actuator/prometheus", "Bearer " + TOKEN);
            assertThat(r.status()).as(method).isEqualTo(HttpServletResponse.SC_METHOD_NOT_ALLOWED);
            assertThat(r.passed()).as(method).isFalse();
        }
    }

    @Test
    void 리다이렉트를_만들지_않는다() throws Exception {
        for (String uri : new String[] {"/actuator/prometheus/", "/actuator", "/"}) {
            assertThat(call("GET", uri, "Bearer " + TOKEN).status() / 100).isNotEqualTo(3);
            assertThat(call("GET", uri, null).status() / 100).isNotEqualTo(3);
        }
    }
}
