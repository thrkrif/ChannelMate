package com.slack.lab.config;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.Map;
import org.junit.jupiter.api.Test;

/** 6단계 M45(C2): 역할 × 토큰 유무 × 포트 입력에서 관리 포트·웹 타입이 정해진 대로 도출된다. */
class RoleWebTypePostProcessorTest {

    private static final String TOKEN = "a".repeat(64);

    @Test
    void 토큰이_없으면_어떤_역할도_관리_리스너가_없다() {
        for (AppRole role : AppRole.values()) {
            Map<String, Object> m = RoleWebTypePostProcessor.derive(role, null, "9091");
            assertThat(m.get("management.server.port")).as(role.name()).isEqualTo(-1);
            assertThat(m.get("management.endpoints.access.default")).isEqualTo("none");
        }
        assertThat(RoleWebTypePostProcessor.derive(AppRole.RECEIVER, "  ", "9091").get("management.server.port"))
                .isEqualTo(-1);
    }

    @Test
    void 토큰이_없으면_업무_웹_타입은_기존_그대로다() {
        assertThat(RoleWebTypePostProcessor.derive(AppRole.WORKER, "", null).get("spring.main.web-application-type"))
                .isEqualTo("none");
        assertThat(RoleWebTypePostProcessor.derive(AppRole.RECEIVER, "", null)).doesNotContainKey("spring.main.web-application-type")
                .doesNotContainKey("server.port");
    }

    @Test
    void 토큰이_있으면_수신과_all은_관리_포트만_더_켠다() {
        for (AppRole role : new AppRole[] {AppRole.RECEIVER, AppRole.ALL}) {
            Map<String, Object> m = RoleWebTypePostProcessor.derive(role, TOKEN, null);
            assertThat(m.get("management.server.port")).isEqualTo(9091);
            assertThat(m).doesNotContainKey("server.port").doesNotContainKey("spring.main.web-application-type");
            assertThat(m).doesNotContainKey("management.endpoints.access.default");
        }
    }

    @Test
    void 토큰이_있으면_워커와_반응은_서블릿_타입에_업무_포트만_끈다() {
        for (AppRole role : new AppRole[] {AppRole.WORKER, AppRole.REACTOR}) {
            Map<String, Object> m = RoleWebTypePostProcessor.derive(role, TOKEN, "9191");
            assertThat(m.get("management.server.port")).isEqualTo(9191);
            assertThat(m.get("spring.main.web-application-type")).isEqualTo("servlet");
            assertThat(m.get("server.port")).isEqualTo(-1);
        }
    }

    @Test
    void CLI_역할은_토큰이_있어도_관리_리스너가_없고_웹_타입_none이다() {
        for (AppRole role : new AppRole[] {AppRole.RECOVERY, AppRole.INDEXER, AppRole.EVALUATOR}) {
            Map<String, Object> m = RoleWebTypePostProcessor.derive(role, TOKEN, "9091");
            assertThat(m.get("management.server.port")).as(role.name()).isEqualTo(-1);
            assertThat(m.get("spring.main.web-application-type")).isEqualTo("none");
            assertThat(m).doesNotContainKey("server.port");
        }
    }

    @Test
    void 포트_입력이_이상하면_기본값이고_0은_시험용으로_허용한다() {
        assertThat(RoleWebTypePostProcessor.derive(AppRole.RECEIVER, TOKEN, "abc").get("management.server.port")).isEqualTo(9091);
        assertThat(RoleWebTypePostProcessor.derive(AppRole.RECEIVER, TOKEN, "70000").get("management.server.port")).isEqualTo(9091);
        assertThat(RoleWebTypePostProcessor.derive(AppRole.RECEIVER, TOKEN, "").get("management.server.port")).isEqualTo(9091);
        assertThat(RoleWebTypePostProcessor.derive(AppRole.RECEIVER, TOKEN, "0").get("management.server.port")).isEqualTo(0);
    }
}
