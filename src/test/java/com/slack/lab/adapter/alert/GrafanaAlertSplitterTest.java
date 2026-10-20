package com.slack.lab.adapter.alert;

import static com.slack.lab.adapter.alert.GrafanaPayloads.M;
import static com.slack.lab.adapter.alert.GrafanaPayloads.URL;
import static com.slack.lab.adapter.alert.GrafanaPayloads.body;
import static com.slack.lab.adapter.alert.GrafanaPayloads.firing;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.stream.IntStream;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

class GrafanaAlertSplitterTest {

    private final GrafanaAlertSplitter splitter = new GrafanaAlertSplitter(M);
    private final GrafanaAlertNormalizer normalizer = new GrafanaAlertNormalizer(M, "C-ALERT");

    private static ObjectNode a() {
        return firing("recovery_needed", "critical", "worker", "2026-10-15T01:00:00Z");
    }

    @Test
    void 배열을_건별_단건_본문으로_나누고_각각_externalURL을_붙인다() throws Exception {
        List<byte[]> parts = splitter.split(body(a(), a(), a(), a()).getBytes(StandardCharsets.UTF_8));

        assertThat(parts).hasSize(4);
        for (byte[] p : parts) {
            var n = M.readTree(p);
            assertThat(n.path("externalURL").asText()).isEqualTo(URL);
            assertThat(n.path("alerts")).hasSize(1);
        }
    }

    @Test
    void 빈_배열은_0건이고_20건은_허용_21건은_거부한다() {
        assertThat(splitter.split(body().getBytes(StandardCharsets.UTF_8))).isEmpty();
        assertThat(splitter.split(body(IntStream.range(0, 20).mapToObj(i -> a()).toArray(ObjectNode[]::new)).getBytes(StandardCharsets.UTF_8)))
                .hasSize(20);
        assertThatThrownBy(() -> splitter.split(body(IntStream.range(0, 21).mapToObj(i -> a()).toArray(ObjectNode[]::new))
                .getBytes(StandardCharsets.UTF_8))).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void alerts가_없거나_배열이_아니면_거부한다() {
        assertThatThrownBy(() -> splitter.split("{\"externalURL\":\"http://a\"}".getBytes(StandardCharsets.UTF_8)))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> splitter.split("{\"externalURL\":\"http://a\",\"alerts\":{}}".getBytes(StandardCharsets.UTF_8)))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> splitter.split("[]".getBytes(StandardCharsets.UTF_8))).isInstanceOf(IllegalArgumentException.class);
    }

    @ParameterizedTest
    @ValueSource(strings = {"", "/relative", "ftp://a/b", "http://user@host/", "http://user:pw@host/", "http://", "not a url",
            "mailto:x@y"})
    void 잘못된_externalURL은_전체_거부다(String bad) {
        assertThatThrownBy(() -> splitter.split(body(bad, a()).getBytes(StandardCharsets.UTF_8)))
                .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void externalURL_누락_비문자열_513자는_거부한다() {
        assertThatThrownBy(() -> splitter.split(body((String) null, a()).getBytes(StandardCharsets.UTF_8)))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> splitter.split("{\"externalURL\":3,\"alerts\":[]}".getBytes(StandardCharsets.UTF_8)))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> splitter.split(body("http://a/" + "x".repeat(513 - 9), a()).getBytes(StandardCharsets.UTF_8)))
                .isInstanceOf(IllegalArgumentException.class);
        // 512자는 허용
        assertThat(splitter.split(body("http://a/" + "x".repeat(512 - 9), a()).getBytes(StandardCharsets.UTF_8))).hasSize(1);
    }

    private String keyOf(String url) {
        byte[] single = splitter.split(body(url, a()).getBytes(StandardCharsets.UTF_8)).get(0);
        return normalizer.normalize(java.util.Map.of(), single).orElseThrow().dedupKey();
    }

    @Test
    void 같은_설치의_표기_차이는_같은_키다() {
        String k = keyOf("http://localhost:3000");
        assertThat(keyOf("http://LOCALHOST:3000/")).isEqualTo(k);
        assertThat(keyOf("http://localhost:3000/?x=1#y")).isEqualTo(k);
        assertThat(keyOf("https://a:443/")).isEqualTo(keyOf("https://a"));
        assertThat(keyOf("http://a:80")).isEqualTo(keyOf("http://a/"));
    }

    @Test
    void 다른_설치는_다른_키다() {
        String k = keyOf("http://localhost:3000");
        assertThat(keyOf("http://localhost:3001")).isNotEqualTo(k);
        assertThat(keyOf("http://localhost:3000/grafana")).isNotEqualTo(k);
        assertThat(keyOf("https://localhost:3000")).isNotEqualTo(k);
    }
}
