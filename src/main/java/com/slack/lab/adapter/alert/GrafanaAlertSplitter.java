package com.slack.lab.adapter.alert;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Grafana 웹훅 본문은 한 요청에 경보 여러 건이 들어 있다. 건별로 나눠 각각 같은 모양의 단건 본문(공통 {@code externalURL} +
 * 경보 1건)으로 돌려주면 정규화기는 단건만 알면 된다. 최상위 {@code externalURL}(설치 식별자)이 틀리면 전체를 거부한다 — 어느
 * 설치의 알람인지 모르면 중복 키가 설치 사이에서 겹친다(PLAN M47 설치 식별자 규칙).
 */
public class GrafanaAlertSplitter {

    /** 한 요청이 처리하는 경보 수 상한. 넘으면 전체 400(Grafana 그룹 알림은 보통 규칙 4개 안이다). */
    public static final int MAX_ALERTS = 20;
    private static final int MAX_URL_CHARS = 512;

    private final ObjectMapper mapper;

    public GrafanaAlertSplitter(ObjectMapper mapper) {
        this.mapper = mapper;
    }

    /** @throws IllegalArgumentException 본문이 객체가 아니거나 {@code externalURL}·{@code alerts}가 올바르지 않거나 21건 이상 */
    public List<byte[]> split(byte[] body) {
        JsonNode root;
        try {
            root = mapper.readTree(body);
        } catch (java.io.IOException e) {
            throw new IllegalArgumentException("invalid_json");
        }
        if (root == null || !root.isObject()) {
            throw new IllegalArgumentException("not_json_object");
        }
        JsonNode url = root.get("externalURL");
        installId(url); // 검증만 — 정규화기도 같은 규칙으로 다시 읽는다
        JsonNode alerts = root.get("alerts");
        if (alerts == null || !alerts.isArray()) {
            throw new IllegalArgumentException("missing_alerts");
        }
        if (alerts.size() > MAX_ALERTS) {
            throw new IllegalArgumentException("too_many_alerts");
        }
        List<byte[]> out = new ArrayList<>();
        for (JsonNode alert : alerts) {
            ObjectNode single = mapper.createObjectNode();
            single.set("externalURL", url);
            single.putArray("alerts").add(alert);
            try {
                out.add(mapper.writeValueAsBytes(single));
            } catch (java.io.IOException e) {
                throw new IllegalArgumentException("invalid_json");
            }
        }
        return out;
    }

    /**
     * 설치 식별자: 스킴·호스트 소문자, 기본 포트(80·443) 제거, 쿼리·조각 제거, 경로 끝 {@code /} 제거. 허용: 문자열, 1~512자,
     * 절대 http(s) URL, 호스트 있음, 사용자 정보 없음. 원문은 어디에도 남기지 않는다(호출자는 해시만 쓴다).
     */
    static String installId(JsonNode node) {
        if (node == null || !node.isTextual()) {
            throw new IllegalArgumentException("invalid_external_url");
        }
        String raw = node.asText();
        if (raw.isEmpty() || raw.length() > MAX_URL_CHARS) {
            throw new IllegalArgumentException("invalid_external_url");
        }
        URI uri;
        try {
            uri = new URI(raw);
        } catch (URISyntaxException e) {
            throw new IllegalArgumentException("invalid_external_url");
        }
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        if (!uri.isAbsolute() || !(scheme.equals("http") || scheme.equals("https")) || uri.getRawUserInfo() != null) {
            throw new IllegalArgumentException("invalid_external_url");
        }
        String host = uri.getHost();
        if (host == null || host.isEmpty()) {
            throw new IllegalArgumentException("invalid_external_url");
        }
        int port = uri.getPort();
        boolean defaultPort = port == -1 || (scheme.equals("http") && port == 80) || (scheme.equals("https") && port == 443);
        String path = uri.getRawPath() == null ? "" : uri.getRawPath();
        while (path.endsWith("/")) {
            path = path.substring(0, path.length() - 1);
        }
        return scheme + "://" + host.toLowerCase(Locale.ROOT) + (defaultPort ? "" : ":" + port) + path;
    }
}
