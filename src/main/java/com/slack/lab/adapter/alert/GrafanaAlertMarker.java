package com.slack.lab.adapter.alert;

import com.slack.lab.core.model.AlertEvent;
import java.util.Optional;
import java.util.Set;

/**
 * Grafana 알람이 만든 메시지 이벤트인지 알아보는 한 곳. 정규화기가 {@code title}을 {@code [grafana:<rule_id>]}로 고정해 두므로,
 * 코어가 알람임을 모르는 채로 설정 계층의 데코레이터(템플릿 LLM·검색 우회)가 같은 접두로 식별한다. 본문 어디에서나 사람이 접두를
 * 흉내 낼 수 있어 식별은 "외부 전송·비용 없는 우회"일 때만 안전하다(PLAN M47 위험).
 */
public final class GrafanaAlertMarker {

    /** 규칙 ID 4종(Grafana 규칙 파일의 {@code slack_lab_rule} 레이블과 같다). */
    public static final String RECOVERY_NEEDED = "recovery_needed";
    public static final String LLM_FINAL_FAILURE = "llm_final_failure";
    public static final String QUEUE_BACKLOG = "queue_backlog";
    public static final String APP_UNOBSERVABLE = "app_unobservable";
    public static final Set<String> RULES = Set.of(RECOVERY_NEEDED, LLM_FINAL_FAILURE, QUEUE_BACKLOG, APP_UNOBSERVABLE);

    /** 공백이 접힌 프롬프트(질문 텍스트)에서 이 접두로 시작하면 정규화기가 만든 Grafana 알람이다. */
    static final String PREFIX = AlertEvent.INSTRUCTION + " " + AlertEvent.ALARM_OPEN + " [grafana:";

    private GrafanaAlertMarker() {}

    /** @return 규칙 ID. Grafana 알람이 아니거나 규칙이 4종 밖이면 비어 있다. */
    public static Optional<String> ruleOf(String promptText) {
        if (promptText == null) {
            return Optional.empty();
        }
        String t = collapse(promptText);
        if (!t.startsWith(PREFIX)) {
            return Optional.empty();
        }
        int end = t.indexOf(']', PREFIX.length());
        if (end < 0) {
            return Optional.empty();
        }
        String rule = t.substring(PREFIX.length(), end);
        return RULES.contains(rule) ? Optional.of(rule) : Optional.empty();
    }

    /** {@code SlackMessageEvent.promptText()}가 하는 공백 접힘과 같은 결과를 얻기 위한 보수적 접기. */
    static String collapse(String s) {
        return s.strip().replaceAll("\\s+", " ");
    }
}
