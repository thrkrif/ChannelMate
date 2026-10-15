package com.slack.lab.adapter.llm;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.slack.lab.adapter.alert.AlertTemplateLlmClient;
import org.junit.jupiter.api.Test;
import org.springframework.boot.DefaultApplicationArguments;

/** 데코레이터가 @Primary로 붙어도 모델 확인·웜업은 원본(OpenAI 호환)으로 한다 — 데코레이터를 받으면 조용히 건너뛴다. */
class LlmStartupCheckWithDecoratorTest {

    @Test
    void 원본을_받으면_모델_목록을_조회하고_웜업_요청을_보낸다() throws Exception {
        String models = "{\"data\":[{\"id\":\"qwen2.5:7b\"}]}";
        try (var modelsStub = StubOpenAiServer.modelsRespondsWith(models)) {
            var props = OpenAiCompatibleLlmClientTest.props(modelsStub.baseUrl());
            var original = new OpenAiCompatibleLlmClient(props, new ObjectMapper());
            var runner = new LlmConfig().llmStartupCheck(original, props);
            runner.run(new DefaultApplicationArguments());
            // 예외 없이 끝남 = 모델 확인 통과. 웜업 실패는 삼켜진다(모델 스텁에 chat 경로가 없다)
        }
    }

    @Test
    void 데코레이터를_받으면_원본이_아니라서_확인을_건너뛴다는_사실을_고정한다() throws Exception {
        var props = OpenAiCompatibleLlmClientTest.props("http://127.0.0.1:1/v1");
        var decorated = new AlertTemplateLlmClient(new OpenAiCompatibleLlmClient(props, new ObjectMapper()));
        // 연결 불가 주소여도 예외가 없다 = 아무 확인도 하지 않았다. 그래서 설정 계층이 @Qualifier("llmClient")로 원본을 주입한다.
        new LlmConfig().llmStartupCheck(decorated, props).run(new DefaultApplicationArguments());
        assertThat(decorated).isNotInstanceOf(OpenAiCompatibleLlmClient.class);
    }
}
