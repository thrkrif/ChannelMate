package com.slack.lab.config;

import com.slack.lab.adapter.alert.AlertFooterChatNotifier;
import com.slack.lab.adapter.alert.AlertTemplateLlmClient;
import com.slack.lab.adapter.slack.SlackClient;
import com.slack.lab.core.port.ChatNotifier;
import com.slack.lab.core.port.LlmClient;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;

/**
 * Grafana 알람 리포트를 위한 데코레이터 배선. 코어는 알람을 모르는 채로 {@code LlmClient}·{@code ChatNotifier} 포트만 쓰고, 알람
 * 식별과 우회는 모두 이 설정 계층에서 포트를 감싸 해결한다(core diff 0). 데코레이터는 {@code @Primary}라 핸들러가 이것을 받고,
 * 원본은 이름({@code llmClient})으로 따로 받는다 — 모델 확인·웜업은 원본으로 한다.
 */
@Configuration
@ConditionalOnRole({AppRole.WORKER, AppRole.ALL})
class AlertReportConfig {

    @Bean
    @Primary
    LlmClient alertTemplateLlmClient(@Qualifier("llmClient") LlmClient original) {
        return new AlertTemplateLlmClient(original);
    }

    @Bean
    @Primary
    ChatNotifier alertFooterChatNotifier(SlackClient slackClient) {
        return new AlertFooterChatNotifier(slackClient);
    }
}
