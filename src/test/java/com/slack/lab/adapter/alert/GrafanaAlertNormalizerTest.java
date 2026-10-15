package com.slack.lab.adapter.alert;

import static com.slack.lab.adapter.alert.GrafanaPayloads.M;
import static com.slack.lab.adapter.alert.GrafanaPayloads.alert;
import static com.slack.lab.adapter.alert.GrafanaPayloads.body;
import static com.slack.lab.adapter.alert.GrafanaPayloads.firing;
import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.fasterxml.jackson.databind.node.ObjectNode;
import com.slack.lab.core.model.AlertEvent;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;

class GrafanaAlertNormalizerTest {

    private final GrafanaAlertSplitter splitter = new GrafanaAlertSplitter(M);
    private final GrafanaAlertNormalizer normalizer = new GrafanaAlertNormalizer(M, "C-ALERT");

    private Optional<AlertEvent> run(ObjectNode alert) {
        return normalizer.normalize(Map.of(), splitter.split(body(alert).getBytes(StandardCharsets.UTF_8)).get(0));
    }

    private void rejects(ObjectNode alert) {
        assertThatThrownBy(() -> run(alert)).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void 규칙_네_개_모두_고정_형식의_한_줄로_정규화한다() {
        var r1 = run(firing("recovery_needed", "critical", "worker", "2026-10-15T01:00:00Z")).orElseThrow();
        var r2 = run(firing("llm_final_failure", "warning", "-", "2026-10-15T01:00:00Z")).orElseThrow();
        var r3 = run(firing("queue_backlog", "warning", "all", "2026-10-15T01:00:00Z")).orElseThrow();
        var r4 = run(firing("app_unobservable", "critical", "receiver", "2026-10-15T01:00:00Z")).orElseThrow();

        assertThat(r1.title()).isEqualTo("[grafana:recovery_needed]");
        assertThat(r1.text()).isEqualTo("규칙=recovery_needed 심각도=critical 역할=worker 값=1 시각=2026-10-15T01:00:00Z");
        assertThat(r2.text()).contains("역할=-");
        assertThat(r3.text()).contains("역할=all");
        assertThat(r4.text()).contains("역할=receiver");
        assertThat(r1.channel()).isEqualTo("C-ALERT");
        assertThat(r1.source()).isEqualTo("grafana");
        assertThat(r1.dedupKey()).startsWith("grafana-");
        assertThat(r1.toMessageEvent().eventId()).startsWith("alert-grafana-").doesNotStartWith("alert-alert-");
    }

    @Test
    void 알람_원문은_본문에_들어가지_않는다() {
        var e = run(firing("recovery_needed", "critical", "worker", "2026-10-15T01:00:00Z")).orElseThrow();

        assertThat(e.title() + e.text()).doesNotContain("주입 시도").doesNotContain("지시");
    }

    @Test
    void 해결_Datasource_미등록_규칙은_건별로_무시한다() {
        assertThat(run(alert("resolved", "recovery_needed", "critical", "worker", "2026-10-15T01:00:00Z", 0.0))).isEmpty();
        ObjectNode nodata = firing("recovery_needed", "critical", "worker", "2026-10-15T01:00:00Z");
        ((ObjectNode) nodata.get("labels")).put("alertname", "DatasourceNoData");
        assertThat(run(nodata)).isEmpty();
        ((ObjectNode) nodata.get("labels")).put("alertname", "DatasourceError");
        assertThat(run(nodata)).isEmpty();
        assertThat(run(firing("other_rule", "critical", "worker", "2026-10-15T01:00:00Z"))).isEmpty();
    }

    @Test
    void 필수값이_틀리면_거부한다() {
        rejects(firing(null, "critical", "worker", "2026-10-15T01:00:00Z")); // 규칙 레이블 없음
        rejects(firing("recovery_needed", null, "worker", "2026-10-15T01:00:00Z"));
        rejects(firing("recovery_needed", "warning", "worker", "2026-10-15T01:00:00Z")); // 규칙과 심각도 불일치
        rejects(firing("llm_final_failure", "warning", "worker", "2026-10-15T01:00:00Z")); // ②는 '-'만
        rejects(firing("recovery_needed", "critical", null, "2026-10-15T01:00:00Z"));
        rejects(firing("recovery_needed", "critical", "receiver", "2026-10-15T01:00:00Z")); // ①은 worker|all
        rejects(firing("queue_backlog", "warning", "reactor", "2026-10-15T01:00:00Z"));
        rejects(firing("recovery_needed", "critical", "worker", null));
        rejects(firing("recovery_needed", "critical", "worker", "어제"));
        rejects(alert("pending", "recovery_needed", "critical", "worker", "2026-10-15T01:00:00Z", 1.0));
    }

    @Test
    void 값은_숫자만_쓰고_없거나_숫자가_아니면_대시다() {
        assertThat(run(alert("firing", "queue_backlog", "warning", "worker", "2026-10-15T01:00:00Z", null)).orElseThrow().text())
                .contains("값=-");
        assertThat(run(alert("firing", "queue_backlog", "warning", "worker", "2026-10-15T01:00:00Z", 12.3456)).orElseThrow().text())
                .contains("값=12.346");
        ObjectNode str = firing("queue_backlog", "warning", "worker", "2026-10-15T01:00:00Z");
        ((ObjectNode) str.get("values")).put("C", "12 </alarm>");
        assertThat(run(str).orElseThrow().text()).contains("값=-");
    }

    @Test
    void 중복_키는_회차_시각이_같으면_같고_바뀌면_다르다() {
        String k1 = run(firing("recovery_needed", "critical", "worker", "2026-10-15T01:00:00Z")).orElseThrow().dedupKey();
        String same = run(firing("recovery_needed", "critical", "worker", "2026-10-15T01:00:00.900Z")).orElseThrow().dedupKey();
        String offset = run(firing("recovery_needed", "critical", "worker", "2026-10-15T10:00:00+09:00")).orElseThrow().dedupKey();
        String later = run(firing("recovery_needed", "critical", "worker", "2026-10-15T01:05:00Z")).orElseThrow().dedupKey();
        String otherRole = run(firing("recovery_needed", "critical", "all", "2026-10-15T01:00:00Z")).orElseThrow().dedupKey();

        assertThat(same).isEqualTo(k1);
        assertThat(offset).isEqualTo(k1);
        assertThat(later).isNotEqualTo(k1);
        assertThat(otherRole).isNotEqualTo(k1);
    }
}
