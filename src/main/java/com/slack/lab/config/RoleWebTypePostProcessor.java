package com.slack.lab.config;

import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.env.EnvironmentPostProcessor;
import org.springframework.core.env.ConfigurableEnvironment;
import org.springframework.core.env.MapPropertySource;

/**
 * 웹 서버가 필요 없는 역할(워커·반응·복구)은 포트를 열지 않는다. 수신 서버만 ngrok에 노출되게 하고,
 * 복구 CLI가 실수로 공개 엔드포인트가 되지 않게 하려는 것이다(PLAN 2단계 리스크 표).
 *
 * <p>6단계 M45: 지표용 관리 서버(별도 포트)도 여기서 정한다. 토큰이 없으면 어떤 환경변수가 있어도 관리 리스너는 0개이고
 * (그래서 {@code addFirst}로 {@code management.server.port}를 덮는다 — {@code MANAGEMENT_SERVER_PORT}가 relaxed binding으로
 * 토큰 없이 포트를 열 수 없다), 토큰이 있으면 CLI 역할을 뺀 역할에서만 켜진다. 워커·반응은 업무 HTTP 포트가 없으므로
 * 관리 컨텍스트를 띄우려고 서블릿 웹 타입을 켜되 {@code server.port=-1}로 업무 리스너는 열지 않는다.
 */
public class RoleWebTypePostProcessor implements EnvironmentPostProcessor {

    static final String TOKEN_PROPERTY = "slacklab.management.token";
    static final String TOKEN_ENV = "MANAGEMENT_METRICS_TOKEN";
    static final String PORT_PROPERTY = "slacklab.management.port";
    static final String PORT_ENV = "MANAGEMENT_SERVER_PORT";
    static final int DEFAULT_MANAGEMENT_PORT = 9091;

    @Override
    public void postProcessEnvironment(ConfigurableEnvironment env, SpringApplication application) {
        AppRole role = OnRoleCondition.currentRole(env);
        String token = first(env.getProperty(TOKEN_PROPERTY), env.getProperty(TOKEN_ENV));
        String port = first(env.getProperty(PORT_PROPERTY), env.getProperty(PORT_ENV));
        Map<String, Object> derived = derive(role, token, port);
        if (!derived.isEmpty()) {
            env.getPropertySources().addFirst(new MapPropertySource("appRoleWebType", derived));
        }
    }

    /** 역할 × 토큰 유무 × 포트 입력에서 덮어쓸 속성을 도출한다(환경 없이 시험할 수 있게 분리). */
    static Map<String, Object> derive(AppRole role, String token, String portRaw) {
        Map<String, Object> m = new LinkedHashMap<>();
        boolean cli = role == AppRole.RECOVERY || role == AppRole.INDEXER || role == AppRole.EVALUATOR;
        int mgmtPort = -1;
        if (!cli && token != null && !token.isBlank()) {
            mgmtPort = parsePort(portRaw);
        }
        m.put("management.server.port", mgmtPort);
        if (mgmtPort < 0) {
            // 관리 리스너가 없으면 엔드포인트 접근도 모두 끈다(CLI 역할 포함).
            m.put("management.endpoints.access.default", "none");
            m.put("management.endpoint.prometheus.access", "none");
        }
        if (!role.servesHttp()) {
            if (mgmtPort >= 0) {
                m.put("spring.main.web-application-type", "servlet");
                m.put("server.port", -1);
            } else {
                m.put("spring.main.web-application-type", "none");
            }
        }
        return m;
    }

    private static int parsePort(String raw) {
        if (raw == null || raw.isBlank()) {
            return DEFAULT_MANAGEMENT_PORT;
        }
        try {
            int p = Integer.parseInt(raw.trim());
            return p >= 0 && p <= 65535 ? p : DEFAULT_MANAGEMENT_PORT;
        } catch (NumberFormatException e) {
            return DEFAULT_MANAGEMENT_PORT;
        }
    }

    private static String first(String a, String b) {
        return a != null && !a.isBlank() ? a : b;
    }
}
