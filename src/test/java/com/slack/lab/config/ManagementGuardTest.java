package com.slack.lab.config;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** 6단계 M45(C2 8): 위험한 관리 서버 조합은 기동에서 거부하고, 사유에 토큰 값이 들어가지 않는다. */
class ManagementGuardTest {

    private static final String TOKEN = "0123456789abcdef".repeat(4);

    @Test
    void 토큰이_없으면_무엇이든_통과한다() {
        assertThat(ManagementGuard.check("", 8080, 8080)).isEmpty();
        assertThat(ManagementGuard.check(null, -1, 8080)).isEmpty();
    }

    @Test
    void 정상_조합은_통과한다() {
        assertThat(ManagementGuard.check(TOKEN, 9091, 8080)).isEmpty();
        assertThat(ManagementGuard.check(TOKEN, 0, 8080)).isEmpty();
        assertThat(ManagementGuard.check(TOKEN, 9091, -1)).isEmpty();
    }

    @Test
    void 관리_포트가_8080이거나_업무_포트와_같으면_거부한다() {
        assertThat(ManagementGuard.check(TOKEN, 8080, 18080)).hasSize(1);
        assertThat(ManagementGuard.check(TOKEN, 18080, 18080)).hasSize(1);
        assertThat(ManagementGuard.check(TOKEN, 8080, 8080)).hasSize(2);
    }

    @Test
    void 토큰_형식이_틀리면_거부하고_값을_출력하지_않는다() {
        for (String bad : new String[] {"short", "g".repeat(64), TOKEN + "0", TOKEN.substring(1)}) {
            var v = ManagementGuard.check(bad, 9091, 8080);
            assertThat(v).hasSize(1);
            assertThat(String.join(";", v)).doesNotContain(bad);
        }
    }
}
