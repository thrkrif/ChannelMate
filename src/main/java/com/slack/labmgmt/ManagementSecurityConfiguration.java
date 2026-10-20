package com.slack.labmgmt;

import org.springframework.boot.actuate.autoconfigure.web.ManagementContextConfiguration;
import org.springframework.boot.actuate.autoconfigure.web.ManagementContextType;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.core.Ordered;
import org.springframework.core.env.Environment;

/**
 * 관리 서버(자식 컨텍스트)에만 인증 필터를 건다. 일부러 스캔 루트({@code com.slack.lab}) 밖 패키지에 두었다 — 주 컨텍스트가
 * 이 설정을 줍지 못하게 하고, 관리 서버가 업무 포트와 같은 컨텍스트로 합쳐지면 이 설정 자체가 로드되지 않아 필터 없는
 * 엔드포인트가 열리는 일을 {@code ManagementGuard}가 기동 거부로 막는다.
 */
@ManagementContextConfiguration(value = ManagementContextType.CHILD, proxyBeanMethods = false)
public class ManagementSecurityConfiguration {

    @Bean
    FilterRegistrationBean<MetricsAuthFilter> metricsAuthFilter(Environment env) {
        String token = env.getProperty("slacklab.management.token", "");
        if (token.isBlank()) {
            token = env.getProperty("MANAGEMENT_METRICS_TOKEN", "");
        }
        if (token.isBlank()) {
            // 토큰이 없으면 관리 서버가 뜨지 않는 것이 정상이다. 여기에 오면 어딘가 어긋난 것이므로 열지 않고 실패시킨다.
            throw new IllegalStateException("관리 서버가 토큰 없이 켜졌다(MANAGEMENT_METRICS_TOKEN)");
        }
        FilterRegistrationBean<MetricsAuthFilter> reg = new FilterRegistrationBean<>(new MetricsAuthFilter(token));
        reg.addUrlPatterns("/*");
        reg.setOrder(Ordered.HIGHEST_PRECEDENCE);
        return reg;
    }
}
