package com.slack.lab.adapter.alert;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.slack.lab.core.model.AlertEvent;
import com.slack.lab.core.port.AlertNormalizer;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.time.temporal.ChronoUnit;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * {@link GrafanaAlertSplitter}가 나눈 단건 본문(공통 {@code externalURL} + 경보 1건)을 알람 이벤트로 바꾼다. 리포트 대상은
 * firing이고 등록된 규칙 4종뿐이다 — resolved·{@code DatasourceNoData}/{@code DatasourceError}·미등록 규칙은 건별로 무시한다
 * (복구 알림이 새 리포트를 만들면 안 되고, 데이터 소스 오류는 규칙 ④가 따로 다룬다). 필수값이 틀리면 예외로 전체 요청을 거부한다.
 *
 * <p>본문에는 알람 원문(주석·설명·generatorURL·valueString)을 쓰지 않는다. 고정 형식의 한 줄만 만든다: 값은 화이트리스트·숫자·
 * ISO 시각이라 프롬프트 주입 표면이 없고, 공백이 없어 공백 접힘 뒤에도 토큰 경계가 유지된다.
 */
public class GrafanaAlertNormalizer implements AlertNormalizer {

    private static final Map<String, String> SEVERITY = Map.of(
            GrafanaAlertMarker.RECOVERY_NEEDED, "critical",
            GrafanaAlertMarker.LLM_FINAL_FAILURE, "warning",
            GrafanaAlertMarker.QUEUE_BACKLOG, "warning",
            GrafanaAlertMarker.APP_UNOBSERVABLE, "critical");
    private static final Map<String, Set<String>> ROLES = Map.of(
            GrafanaAlertMarker.RECOVERY_NEEDED, Set.of("worker", "all"),
            GrafanaAlertMarker.LLM_FINAL_FAILURE, Set.of("-"),
            GrafanaAlertMarker.QUEUE_BACKLOG, Set.of("worker", "all"),
            GrafanaAlertMarker.APP_UNOBSERVABLE, Set.of("all", "receiver", "worker", "reactor"));
    /** 규칙의 조건 표현식(refId)이다. 다른 refId의 값은 조건과 무관해 쓰지 않는다. */
    private static final String CONDITION_REF = "C";

    private final ObjectMapper mapper;
    private final String channel;

    public GrafanaAlertNormalizer(ObjectMapper mapper, String channel) {
        this.mapper = mapper;
        this.channel = channel;
    }

    @Override
    public String source() {
        return "grafana";
    }

    @Override
    public Optional<AlertEvent> normalize(Map<String, String> headers, byte[] body) {
        JsonNode root;
        try {
            root = mapper.readTree(body);
        } catch (java.io.IOException e) {
            throw new IllegalArgumentException("invalid_json");
        }
        if (root == null || !root.isObject()) {
            throw new IllegalArgumentException("not_json_object");
        }
        String install = GrafanaAlertSplitter.installId(root.get("externalURL"));
        JsonNode alerts = root.get("alerts");
        if (alerts == null || !alerts.isArray() || alerts.size() != 1 || !alerts.get(0).isObject()) {
            throw new IllegalArgumentException("not_single_alert");
        }
        JsonNode alert = alerts.get(0);

        String status = alert.path("status").asText("");
        if (!status.equals("firing") && !status.equals("resolved")) {
            throw new IllegalArgumentException("invalid_status");
        }
        JsonNode labels = alert.path("labels");
        if (!labels.isObject()) {
            throw new IllegalArgumentException("missing_labels");
        }
        if (labels.path("alertname").asText("").startsWith("Datasource")) {
            return Optional.empty(); // DatasourceNoData/DatasourceError — 규칙 ④·적체 규칙의 몫이다
        }
        String rule = labels.path("slack_lab_rule").asText("");
        if (rule.isBlank()) {
            throw new IllegalArgumentException("missing_rule");
        }
        if (!GrafanaAlertMarker.RULES.contains(rule) || status.equals("resolved")) {
            return Optional.empty();
        }
        String severity = labels.path("severity").asText("");
        if (!SEVERITY.get(rule).equals(severity)) {
            throw new IllegalArgumentException("invalid_severity");
        }
        String role = labels.path("role").asText("");
        if (!ROLES.get(rule).contains(role)) {
            throw new IllegalArgumentException("invalid_role");
        }
        Instant startsAt = startsAt(alert.path("startsAt").asText(""));
        String value = value(alert.path("values"));

        String title = "[grafana:" + rule + "]";
        String text = "규칙=" + rule + " 심각도=" + severity + " 역할=" + role + " 값=" + value + " 시각=" + startsAt;
        String key = "grafana-" + CloudWatchAlertNormalizer.sha("grafana|" + install + "|" + rule + "|" + role + "|" + startsAt);
        Map<String, String> attrs = new LinkedHashMap<>();
        attrs.put("rule", rule);
        attrs.put("severity", severity);
        attrs.put("role", role);
        return Optional.of(new AlertEvent(source(), key, title, text, channel, attrs));
    }

    private static Instant startsAt(String raw) {
        try {
            return OffsetDateTime.parse(raw).toInstant().truncatedTo(ChronoUnit.SECONDS);
        } catch (DateTimeParseException e) {
            throw new IllegalArgumentException("invalid_starts_at");
        }
    }

    /** 조건 refId의 유한한 숫자만. 정수면 정수로, 아니면 소수 셋째 자리까지. 없거나 숫자가 아니면 {@code -}. */
    private static String value(JsonNode values) {
        JsonNode v = values.isObject() ? values.get(CONDITION_REF) : null;
        if (v == null || !v.isNumber() || !Double.isFinite(v.asDouble())) {
            return "-";
        }
        double d = v.asDouble();
        return d == Math.rint(d) && Math.abs(d) < 1e15 ? Long.toString((long) d) : String.format(java.util.Locale.ROOT, "%.3f", d);
    }
}
