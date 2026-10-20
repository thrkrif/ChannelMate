package com.slack.lab.adapter.logging;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 코어가 남기는 한국어 로그 원문을 로그 저장소가 질의할 사건 키로 바꾸는 매핑 한 곳이다. 코어는 건드리지 않고(ADR-9 경계),
 * 판별 문자열은 {@code scripts/p1-metrics}가 쓰는 것과 같아야 한다 — 두 곳이 어긋나면 대시보드와 집계기가 같은 로그를 다르게 센다
 * ({@code EventKeysContractTest}가 코어 소스 원문과 대조한다).
 */
public final class EventKeys {

    /** {@code fields}에 넣어도 되는 키. event_id·attempt_id·질문·채널은 어떤 경우에도 넣지 않는다. */
    public static final Set<String> FIELD_KEYS = Set.of(
            "queue_ready", "defer", "dead", "retry", "dlq", "recovery",
            "queue_wait_ms", "llm_ms", "send_ms", "answer_ms",
            "result", "kind", "elapsed_ms", "reason",
            "gen", "retries", "manual_run", "finalized", "negative_interval", "received_at_missing",
            "final_attempt");

    private static final Pattern KV = Pattern.compile("(\\w+)=(\\S+)");
    private static final Pattern NUMBER = Pattern.compile("-?\\d+");
    private static final Pattern ENUM = Pattern.compile("[A-Za-z_][A-Za-z0-9_-]*");

    private EventKeys() {}

    /** 사건 키(없으면 null). p1-metrics의 판별 순서와 같다. */
    public static String eventOf(String msg) {
        if (msg == null) {
            return null;
        }
        if (msg.contains("metric=rag_result")) {
            return "rag_result";
        }
        if (msg.contains("처리 지표")) {
            return "processing_metric";
        }
        if (msg.contains("적체 스냅샷 실패")) {
            return "backlog_unobserved";
        }
        if (msg.contains("적체 스냅샷 ")) {
            return "backlog_snapshot";
        }
        if (msg.contains("적체 스냅샷")) {
            return "backlog_unobserved"; // 키가 하나도 없는 줄: 관측 실패로 본다
        }
        if (msg.contains("LLM 실패 → 실패 안내로 전환")) {
            return "llm_final_failure";
        }
        return null;
    }

    /** 허용 키만, 값이 숫자·불리언·열거값일 때만 담는다. 부분 스냅샷의 빠진 키는 만들지 않는다(0 치환 금지). */
    public static Map<String, Object> fieldsOf(String event, String msg) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (event == null || msg == null) {
            return out;
        }
        Matcher m = KV.matcher(msg);
        while (m.find()) {
            String key = m.group(1);
            String value = m.group(2);
            if (!FIELD_KEYS.contains(key) || out.containsKey(key)) {
                continue;
            }
            if ("backlog_unobserved".equals(event) && !"reason".equals(key)) {
                continue;
            }
            Object typed = typed(key, value);
            if (typed != null) {
                out.put(key, typed);
            }
        }
        return out;
    }

    private static final Set<String> BOOLEAN_KEYS = Set.of(
            "manual_run", "finalized", "negative_interval", "received_at_missing", "final_attempt");
    private static final Set<String> ENUM_KEYS = Set.of("result", "kind", "reason");

    /** 키마다 정해진 형태의 값만 통과한다 — 숫자 키에 열거값이 오는 식의 어긋난 줄은 버린다. */
    private static Object typed(String key, String value) {
        if (BOOLEAN_KEYS.contains(key)) {
            return "true".equals(value) || "false".equals(value) ? (Object) Boolean.parseBoolean(value) : null;
        }
        if (ENUM_KEYS.contains(key)) {
            return ENUM.matcher(value).matches() ? value : null;
        }
        if (NUMBER.matcher(value).matches()) {
            try {
                return Long.parseLong(value);
            } catch (NumberFormatException e) {
                return null;
            }
        }
        return null;
    }
}
