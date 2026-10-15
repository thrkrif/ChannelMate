package com.slack.lab.adapter.alert;

import com.slack.lab.core.model.LlmMessage;
import com.slack.lab.core.model.LlmResult;
import com.slack.lab.core.port.LlmClient;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Grafana 알람 리포트가 모델에 막히지 않게 하는 데코레이터. 알람은 "LLM이 죽었다"·"앱이 안 보인다" 같은 순간에 오므로, 모델이
 * 필요한 리포트가 모델 때문에 안 나가면 알람의 의미가 없다. 규칙 ②(LLM 최종 실패)와 ④(앱 관측 불가)는 모델을 부르지 않고 고정
 * 템플릿으로 답한다(②는 모델이 원인이라 순환, ④는 원인 분석에 줄 정보가 없다). ①③은 모델을 한 번 불러 보고, 실패하거나 시간이
 * 지나면 템플릿으로 내려간다. 이 경로는 {@code llm_final_failure}를 남기지 않아 ②와 순환하지 않는다.
 *
 * <p>호출 수의 단위는 처리 시도(핸들러 실행) 한 번이다 — 이 클래스가 원본 {@code chat()}을 부르는 횟수는 최대 1이다. 원본이 그 안에서
 * 보내는 HTTP 요청 수(언어 재질문 등)는 어댑터의 정책이다. 알람 경로만 재질문을 막는 정책은 어댑터가 알람을 알게 되므로 두지 않는다.
 * 알람 식별이 아니면 그대로 위임한다.
 */
public class AlertTemplateLlmClient implements LlmClient {

    private static final Logger log = LoggerFactory.getLogger(AlertTemplateLlmClient.class);
    /** 템플릿으로 내려가기 전에 모델에 줄 수 있는 최대 시간(이 안에 못 끝내면 템플릿). */
    static final long DELEGATE_BUDGET_MS = 10_000;

    private static final Map<String, String> GUIDE = Map.of(
            GrafanaAlertMarker.RECOVERY_NEEDED,
            "복구가 필요한 건이 남아 있습니다. `scripts/recovery list`로 결과 불명·DLQ 건을 확인하고 원인에 맞게 복구하세요.",
            GrafanaAlertMarker.LLM_FINAL_FAILURE,
            "답변 생성이 재시도 끝에 실패했습니다. LLM 서버(Ollama) 상태와 모델 적재 여부를 확인하세요.",
            GrafanaAlertMarker.QUEUE_BACKLOG,
            "처리 대기 큐가 지속적으로 쌓이고 있습니다. 워커가 살아 있는지와 LLM 응답 시간을 확인하세요.",
            GrafanaAlertMarker.APP_UNOBSERVABLE,
            "앱의 지표를 수집하지 못하고 있습니다. 해당 역할의 프로세스·컨테이너가 떠 있는지 확인하세요.");

    private final LlmClient delegate;

    public AlertTemplateLlmClient(LlmClient delegate) {
        this.delegate = delegate;
    }

    @Override
    public LlmResult chat(List<LlmMessage> messages, long remainingMs) {
        Optional<String> rule = messages.isEmpty() ? Optional.empty()
                : GrafanaAlertMarker.ruleOf(messages.get(messages.size() - 1).content());
        if (rule.isEmpty() || !"user".equals(messages.get(messages.size() - 1).role())) {
            return delegate.chat(messages, remainingMs);
        }
        String id = rule.get();
        String template = template(id, messages.get(messages.size() - 1).content());
        if (id.equals(GrafanaAlertMarker.LLM_FINAL_FAILURE) || id.equals(GrafanaAlertMarker.APP_UNOBSERVABLE)) {
            return new LlmResult.Success(template, 0);
        }
        long budget = Math.min(remainingMs, DELEGATE_BUDGET_MS);
        long start = System.nanoTime();
        if (budget <= 0) {
            return fallback(id, template, "timeout", start);
        }
        LlmResult result = delegate.chat(messages, budget);
        long elapsed = (System.nanoTime() - start) / 1_000_000;
        if (result instanceof LlmResult.Success s && elapsed <= DELEGATE_BUDGET_MS && !s.text().isBlank()) {
            return result;
        }
        String reason = result instanceof LlmResult.Failed f ? f.error().code().code() : "timeout";
        return fallback(id, template, reason, start);
    }

    private static LlmResult fallback(String rule, String template, String reason, long startNanos) {
        long elapsed = (System.nanoTime() - startNanos) / 1_000_000;
        log.info("알람 템플릿 폴백 rule={} reason={} elapsed_ms={}", rule, reason, elapsed);
        return new LlmResult.Success(template, elapsed);
    }

    /** 정규화기가 만든 고정 한 줄과 같은 모양일 때만 사실 줄을 옮긴다(엄격한 정규식 — 모양이 다르면 생략). */
    private static final java.util.regex.Pattern FACTS = java.util.regex.Pattern.compile(
            "규칙=(%s) 심각도=(critical|warning) 역할=(-|all|receiver|worker|reactor) 값=(-|-?[0-9]{1,15}(?:\\.[0-9]{1,3})?) "
                    + "시각=([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)");

    /** 알람 원문은 쓰지 않는다. 규칙별 고정 안내에, 형식이 맞는 사실 줄(규칙·심각도·역할·값·시각)만 붙인다. */
    private static String template(String rule, String prompt) {
        var m = java.util.regex.Pattern.compile(FACTS.pattern().formatted(java.util.regex.Pattern.quote(rule)))
                .matcher(GrafanaAlertMarker.collapse(prompt));
        String facts = m.find() ? "\n" + m.group() : "";
        return "[자동 요약] " + GUIDE.get(rule) + facts;
    }
}
