package com.slack.lab.adapter.alert;

import static org.assertj.core.api.Assertions.assertThat;

import com.slack.lab.core.model.ReactionResult;
import com.slack.lab.core.model.ReferenceList;
import com.slack.lab.core.model.ReplyFooter;
import com.slack.lab.core.model.ReplyMetadata;
import com.slack.lab.core.model.SlackSendResult;
import com.slack.lab.core.port.ChatNotifier;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;

class AlertFooterChatNotifierTest {

    private final List<ReplyFooter> seen = new ArrayList<>();
    private final ChatNotifier inner = new ChatNotifier() {
        @Override
        public SlackSendResult postMessage(String channel, String threadTs, String text, ReplyFooter footer, long remainingMs,
                ReplyMetadata metadata) {
            seen.add(footer);
            return new SlackSendResult.Success("1.1");
        }

        @Override
        public ReactionResult addReaction(String channel, String ts, String emoji) {
            return new ReactionResult(true, "added");
        }
    };
    private final AlertFooterChatNotifier notifier = new AlertFooterChatNotifier(inner);

    private ReplyFooter send(String eventId, ReplyFooter footer) {
        seen.clear();
        notifier.postMessage("C", null, "본문", footer, 1_000, new ReplyMetadata(eventId, "a1"));
        return seen.get(0);
    }

    @Test
    void 그라파나_알람이면_검색_안내_푸터를_뺀다() {
        assertThat(send("alert-grafana-abc", new ReplyFooter.NoRelevantDocuments())).isEqualTo(ReplyFooter.NONE);
        assertThat(send("alert-grafana-abc", new ReplyFooter.SearchUnavailable())).isEqualTo(ReplyFooter.NONE);
    }

    @Test
    void 일반_멘션과_CloudWatch_알람은_그대로다() {
        var none = new ReplyFooter.NoRelevantDocuments();
        assertThat(send("EvNormal", none)).isSameAs(none);
        assertThat(send("alert-abc123", none)).isSameAs(none);
        var unavailable = new ReplyFooter.SearchUnavailable();
        assertThat(send("EvNormal", unavailable)).isSameAs(unavailable);
    }

    @Test
    void 출처_목록_푸터는_그라파나여도_건드리지_않는다() {
        var refs = new ReplyFooter.References(ReferenceList.fromInjected(List.of()));
        assertThat(send("alert-grafana-abc", refs)).isSameAs(refs);
    }

    @Test
    void 메타데이터가_없어도_예외_없이_위임한다() {
        notifier.postMessage("C", null, "본문", new ReplyFooter.NoRelevantDocuments(), 1_000, null);
        assertThat(seen).hasSize(1);
    }
}
