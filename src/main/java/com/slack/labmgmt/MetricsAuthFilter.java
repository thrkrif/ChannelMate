package com.slack.labmgmt;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 관리 포트의 유일한 문이다. 인증을 먼저 보고(없거나 틀리면 경로와 상관없이 401 — 경로 존재 여부를 인증 없이 알려 주지 않는다),
 * 그다음 {@code GET /actuator/prometheus} 정확 일치만 통과시킨다. 경로는 정규화 전 원문({@code getRequestURI})으로 비교해
 * {@code ..}·{@code %2e}·{@code ;} 같은 변형이 허용 경로로 둔갑하지 못하게 한다. 토큰 비교는 길이가 새지 않도록
 * 다이제스트 상수 시간 비교다. 리다이렉트는 만들지 않는다.
 */
public class MetricsAuthFilter extends OncePerRequestFilter {

    static final String ALLOWED_PATH = "/actuator/prometheus";
    private static final String PREFIX = "Bearer ";

    private final byte[] expectedDigest;

    public MetricsAuthFilter(String token) {
        this.expectedDigest = sha256(token);
    }

    @Override
    protected boolean shouldNotFilterAsyncDispatch() {
        return false;
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        if (!authenticated(request.getHeader("Authorization"))) {
            response.setHeader("WWW-Authenticate", "Bearer");
            response.sendError(HttpServletResponse.SC_UNAUTHORIZED);
            return;
        }
        if (!ALLOWED_PATH.equals(request.getRequestURI())) {
            response.sendError(HttpServletResponse.SC_NOT_FOUND);
            return;
        }
        if (!"GET".equals(request.getMethod())) {
            response.setHeader("Allow", "GET");
            response.sendError(HttpServletResponse.SC_METHOD_NOT_ALLOWED);
            return;
        }
        chain.doFilter(request, response);
    }

    private boolean authenticated(String header) {
        // "Bearer"는 대소문자를 구분해 정확히 일치시키고, 값 앞뒤 공백도 허용하지 않는다.
        if (header == null || !header.startsWith(PREFIX)) {
            return false;
        }
        return MessageDigest.isEqual(sha256(header.substring(PREFIX.length())), expectedDigest);
    }

    private static byte[] sha256(String s) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
