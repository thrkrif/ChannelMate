package com.slack.lab.adapter.llm;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.slack.lab.adapter.alert.AlertTemplateLlmClient;
import com.slack.lab.core.model.AlertEvent;
import com.slack.lab.core.model.LlmMessage;
import com.slack.lab.core.model.LlmResult;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;

/**
 * 호출 수의 단위(F6): 처리 시도당 {@code LlmClient} 포트 위임 ≤1, 어댑터 내부 HTTP 요청은 언어 재질문으로 ≤2. 실제
 * {@code OpenAiCompatibleLlmClient}를 쓰고 스텁 접속 수로 센다.
 */
class AlertTemplateDelegationCountTest {

    private static List<LlmMessage> alert(String rule) {
        var e = new AlertEvent("grafana", "grafana-x", "[grafana:" + rule + "]",
                "규칙=" + rule + " 심각도=critical 역할=worker 값=3 시각=2026-10-15T01:00:00Z", "C", java.util.Map.of());
        return List.of(LlmMessage.user(e.toMessageEvent().text().replaceAll("\\s+", " ")));
    }

    /** 위임 수를 세는 얇은 래퍼(포트 호출 횟수). */
    private static com.slack.lab.core.port.LlmClient counting(com.slack.lab.core.port.LlmClient inner, AtomicInteger n) {
        return (messages, remainingMs) -> {
            n.incrementAndGet();
            return inner.chat(messages, remainingMs);
        };
    }

    @Test
    void 정상_응답은_위임_1회_HTTP_1회다() throws Exception {
        try (var stub = StubOpenAiServer.chatSequence(List.of(OpenAiCompatibleLlmClientTest.reply("복구 절차를 확인하세요.")))) {
            var delegations = new AtomicInteger();
            var client = new AlertTemplateLlmClient(counting(
                    new OpenAiCompatibleLlmClient(OpenAiCompatibleLlmClientTest.props(stub.baseUrl()), new ObjectMapper()), delegations));

            var result = client.chat(alert("recovery_needed"), 50_000);

            assertThat(((LlmResult.Success) result).text()).isEqualTo("복구 절차를 확인하세요.");
            assertThat(delegations).hasValue(1);
            assertThat(stub.requestCount()).isEqualTo(1);
        }
    }

    @Test
    void 한자가_섞이면_위임_1회_HTTP_2회_총_10초_안이다() throws Exception {
        try (var stub = StubOpenAiServer.chatSequence(List.of(
                OpenAiCompatibleLlmClientTest.reply("专业的中文翻译如下 확인하세요."), OpenAiCompatibleLlmClientTest.reply("큐를 확인하세요.")))) {
            var delegations = new AtomicInteger();
            var client = new AlertTemplateLlmClient(counting(
                    new OpenAiCompatibleLlmClient(OpenAiCompatibleLlmClientTest.props(stub.baseUrl()), new ObjectMapper()), delegations));
            long start = System.nanoTime();

            var result = client.chat(alert("queue_backlog"), 50_000);

            assertThat(((LlmResult.Success) result).text()).isEqualTo("큐를 확인하세요.");
            assertThat(delegations).hasValue(1);
            assertThat(stub.requestCount()).isEqualTo(2);
            assertThat((System.nanoTime() - start) / 1_000_000).isLessThanOrEqualTo(10_000);
        }
    }

    @Test
    void 규칙_2는_스텁에_접속하지_않는다() throws Exception {
        try (var stub = StubOpenAiServer.chatSequence(List.of(OpenAiCompatibleLlmClientTest.reply("답")))) {
            var delegations = new AtomicInteger();
            var client = new AlertTemplateLlmClient(counting(
                    new OpenAiCompatibleLlmClient(OpenAiCompatibleLlmClientTest.props(stub.baseUrl()), new ObjectMapper()), delegations));

            client.chat(alert("llm_final_failure"), 50_000);
            client.chat(alert("app_unobservable"), 50_000);

            assertThat(delegations).hasValue(0);
            assertThat(stub.requestCount()).isZero();
        }
    }

    @Test
    void 지연이_10초를_넘으면_위임_1회_폴백이다() throws Exception {
        try (var stub = StubOpenAiServer.chatRespondsAfter(OpenAiCompatibleLlmClientTest.reply("늦은 답"), 12_000)) {
            var delegations = new AtomicInteger();
            var client = new AlertTemplateLlmClient(counting(
                    new OpenAiCompatibleLlmClient(OpenAiCompatibleLlmClientTest.props(stub.baseUrl()), new ObjectMapper()), delegations));
            long start = System.nanoTime();

            var result = client.chat(alert("recovery_needed"), 50_000);

            assertThat(((LlmResult.Success) result).text()).startsWith("[자동 요약]");
            assertThat(delegations).hasValue(1);
            assertThat((System.nanoTime() - start) / 1_000_000).isLessThan(11_500);
        }
    }
}
