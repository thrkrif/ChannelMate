package com.slack.lab.adapter.alert;

import static com.slack.lab.adapter.alert.GrafanaPayloads.alert;
import static com.slack.lab.adapter.alert.GrafanaPayloads.body;
import static com.slack.lab.adapter.alert.GrafanaPayloads.firing;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.times;
import static org.mockito.Mockito.verify;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.slack.lab.config.AlertProperties;
import com.slack.lab.core.model.ErrorCode;
import com.slack.lab.core.model.ErrorInfo;
import com.slack.lab.core.model.PublishResult;
import com.slack.lab.core.model.SlackMessageEvent;
import com.slack.lab.core.port.EventPublisher;
import java.util.stream.IntStream;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;

/** 기존 {@code AlertControllerTest}(CloudWatch)는 무변경이다 — Grafana 경로만 이 시험이 다룬다. */
@WebMvcTest(AlertController.class)
@Import(AlertControllerGrafanaTest.Config.class)
class AlertControllerGrafanaTest {

    @TestConfiguration
    static class Config {
        @Bean
        AlertProperties alertProperties() {
            return new AlertProperties("s3cret", "C-ALERT");
        }

        @Bean
        GrafanaAlertNormalizer grafana() {
            return new GrafanaAlertNormalizer(new ObjectMapper(), "C-ALERT");
        }

        @Bean
        CloudWatchAlertNormalizer cloudWatch() {
            return new CloudWatchAlertNormalizer(new ObjectMapper(), "C-ALERT");
        }
    }

    @Autowired
    MockMvc mvc;

    @MockitoBean
    EventPublisher publisher;

    @BeforeEach
    void ok() {
        given(publisher.publish(any(SlackMessageEvent.class), anyLong(), any())).willReturn(new PublishResult.Enqueued("m1"));
    }

    private ResultActions send(String path, String secret, String json) throws Exception {
        var req = post(path).contentType(MediaType.APPLICATION_JSON).content(json);
        if (secret != null) {
            req.header("X-Alert-Secret", secret);
        }
        return mvc.perform(req);
    }

    private static ObjectNode r(String rule, String sev, String role) {
        return firing(rule, sev, role, "2026-10-15T01:00:00Z");
    }

    @Test
    void 배열_1_2_4건은_대상_수만큼_발행하고_200이다() throws Exception {
        send("/alerts/grafana", "s3cret", body(r("recovery_needed", "critical", "worker"))).andExpect(status().isOk());
        verify(publisher, times(1)).publish(any(SlackMessageEvent.class), anyLong(), any());

        send("/alerts/grafana", "s3cret", body(r("recovery_needed", "critical", "worker"), r("queue_backlog", "warning", "worker")))
                .andExpect(status().isOk());
        verify(publisher, times(3)).publish(any(SlackMessageEvent.class), anyLong(), any());

        send("/alerts/grafana", "s3cret", body(r("recovery_needed", "critical", "worker"), r("queue_backlog", "warning", "worker"),
                r("llm_final_failure", "warning", "-"), r("app_unobservable", "critical", "all"))).andExpect(status().isOk());
        verify(publisher, times(7)).publish(any(SlackMessageEvent.class), anyLong(), any());
    }

    @Test
    void firing_resolved_혼합과_Datasource_미등록은_무시하고_대상만_발행한다() throws Exception {
        ObjectNode nodata = r("recovery_needed", "critical", "worker");
        ((ObjectNode) nodata.get("labels")).put("alertname", "DatasourceError");
        send("/alerts/grafana", "s3cret", body(r("recovery_needed", "critical", "worker"),
                alert("resolved", "queue_backlog", "warning", "worker", "2026-10-15T01:00:00Z", 0.0), nodata,
                r("zzz_unknown", "critical", "worker"))).andExpect(status().isOk());

        verify(publisher, times(1)).publish(any(SlackMessageEvent.class), anyLong(), any());
    }

    @Test
    void 대상이_0건이어도_200이다() throws Exception {
        send("/alerts/grafana", "s3cret", body(alert("resolved", "queue_backlog", "warning", "worker", "2026-10-15T01:00:00Z", 0.0)))
                .andExpect(status().isOk());
        send("/alerts/grafana", "s3cret", body()).andExpect(status().isOk());

        verify(publisher, never()).publish(any(), anyLong(), any());
    }

    @Test
    void 한_건이라도_필수값이_틀리면_전체_400이고_큐_저장은_0이다() throws Exception {
        send("/alerts/grafana", "s3cret", body(r("recovery_needed", "critical", "worker"), r("queue_backlog", "critical", "worker")))
                .andExpect(status().isBadRequest());
        send("/alerts/grafana", "s3cret", body((String) null, r("recovery_needed", "critical", "worker"))).andExpect(status().isBadRequest());
        send("/alerts/grafana", "s3cret", "not json").andExpect(status().isBadRequest());

        verify(publisher, never()).publish(any(), anyLong(), any());
    }

    @Test
    void 스물한_건_이상은_400이다() throws Exception {
        send("/alerts/grafana", "s3cret", body(IntStream.range(0, 21).mapToObj(i -> r("recovery_needed", "critical", "worker"))
                .toArray(ObjectNode[]::new))).andExpect(status().isBadRequest());

        verify(publisher, never()).publish(any(), anyLong(), any());
    }

    @Test
    void 쿼리_token은_401이고_헤더가_없거나_틀려도_401이다() throws Exception {
        send("/alerts/grafana?token=s3cret", null, body(r("recovery_needed", "critical", "worker"))).andExpect(status().isUnauthorized());
        send("/alerts/grafana?token=s3cret", "s3cret", body(r("recovery_needed", "critical", "worker")))
                .andExpect(status().isUnauthorized());
        send("/alerts/grafana", null, body(r("recovery_needed", "critical", "worker"))).andExpect(status().isUnauthorized());
        send("/alerts/grafana", "wrong", body(r("recovery_needed", "critical", "worker"))).andExpect(status().isUnauthorized());

        verify(publisher, never()).publish(any(), anyLong(), any());
    }

    @Test
    void 부분_큐_저장_실패는_503이고_저장된_건은_그대로_두며_재전송은_같은_ID다() throws Exception {
        given(publisher.publish(any(SlackMessageEvent.class), anyLong(), any()))
                .willReturn(new PublishResult.Enqueued("m1"))
                .willReturn(new PublishResult.Failed(ErrorInfo.of(ErrorCode.QUEUE_PUBLISH_FAILED)));
        String json = body(r("recovery_needed", "critical", "worker"), r("queue_backlog", "warning", "worker"));

        send("/alerts/grafana", "s3cret", json).andExpect(status().isServiceUnavailable());
        var captor = org.mockito.ArgumentCaptor.forClass(SlackMessageEvent.class);
        verify(publisher, times(2)).publish(captor.capture(), anyLong(), any());
        // 재전송: 같은 본문은 같은 이벤트 ID라 워커의 선점이 이미 저장된 건을 흡수한다
        given(publisher.publish(any(SlackMessageEvent.class), anyLong(), any())).willReturn(new PublishResult.Enqueued("m2"));
        send("/alerts/grafana", "s3cret", json).andExpect(status().isOk());
        verify(publisher, times(4)).publish(captor.capture(), anyLong(), any());
        var ids = captor.getAllValues().stream().map(SlackMessageEvent::eventId).toList();
        org.assertj.core.api.Assertions.assertThat(ids.get(0)).isEqualTo(ids.get(2));
        org.assertj.core.api.Assertions.assertThat(ids.get(1)).isEqualTo(ids.get(3));
    }

    @Test
    void 최초와_재전송_3회는_같은_이벤트_ID고_startsAt이_바뀌면_다르다() throws Exception {
        String json = body(r("recovery_needed", "critical", "worker"));
        for (int i = 0; i < 3; i++) {
            send("/alerts/grafana", "s3cret", json).andExpect(status().isOk());
        }
        send("/alerts/grafana", "s3cret", body(firing("recovery_needed", "critical", "worker", "2026-10-15T02:00:00Z")))
                .andExpect(status().isOk());

        var captor = org.mockito.ArgumentCaptor.forClass(SlackMessageEvent.class);
        verify(publisher, times(4)).publish(captor.capture(), anyLong(), any());
        var ids = captor.getAllValues().stream().map(SlackMessageEvent::eventId).toList();
        org.assertj.core.api.Assertions.assertThat(ids.subList(0, 3)).containsOnly(ids.get(0));
        org.assertj.core.api.Assertions.assertThat(ids.get(3)).isNotEqualTo(ids.get(0));
        org.assertj.core.api.Assertions.assertThat(ids.get(0)).startsWith("alert-grafana-").doesNotStartWith("alert-alert-");
    }
}
