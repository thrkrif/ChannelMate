package com.slack.lab.config;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;
import org.springframework.core.env.Environment;
import org.springframework.stereotype.Component;

/**
 * 관리 서버 설정의 위험한 조합을 기동에서 거부한다(키 이름·사유만, 토큰 값은 절대 출력하지 않는다). 토큰이 없으면 관리 리스너가
 * 없으므로 검사할 것이 없다. 관리 포트가 업무 포트와 같으면 Boot가 두 컨텍스트를 합쳐 인증 필터 설정이 빠진 채 엔드포인트가 열린다.
 */
@Component
@ConditionalOnRole({AppRole.RECEIVER, AppRole.WORKER, AppRole.REACTOR, AppRole.ALL})
public class ManagementGuard {

    private static final Pattern HEX64 = Pattern.compile("^[0-9a-fA-F]{64}$");

    public ManagementGuard(Environment env) {
        String token = env.getProperty(RoleWebTypePostProcessor.TOKEN_PROPERTY, "");
        if (token.isBlank()) {
            token = env.getProperty(RoleWebTypePostProcessor.TOKEN_ENV, "");
        }
        Integer mgmtPort = env.getProperty("management.server.port", Integer.class);
        Integer serverPort = env.getProperty("server.port", Integer.class);
        List<String> violations = check(token, mgmtPort == null ? -1 : mgmtPort, serverPort == null ? 8080 : serverPort);
        if (!violations.isEmpty()) {
            throw new IllegalStateException("관리 서버 설정 위반: " + String.join("; ", violations));
        }
    }

    static List<String> check(String token, int mgmtPort, int serverPort) {
        List<String> v = new ArrayList<>();
        if (token == null || token.isBlank()) {
            return v; // 토큰이 없으면 관리 리스너 자체가 없다
        }
        if (!HEX64.matcher(token).matches()) {
            v.add("MANAGEMENT_METRICS_TOKEN은 64자리 16진수여야 한다(openssl rand -hex 32)");
        }
        if (mgmtPort > 0) {
            if (mgmtPort == 8080) {
                v.add("MANAGEMENT_SERVER_PORT는 8080일 수 없다(업무 포트)");
            }
            if (serverPort > 0 && mgmtPort == serverPort) {
                v.add("MANAGEMENT_SERVER_PORT는 server.port와 달라야 한다");
            }
        }
        return v;
    }
}
