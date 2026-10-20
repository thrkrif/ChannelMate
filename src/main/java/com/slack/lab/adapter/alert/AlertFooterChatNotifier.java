package com.slack.lab.adapter.alert;

import com.slack.lab.core.model.ReactionResult;
import com.slack.lab.core.model.ReplyFooter;
import com.slack.lab.core.model.ReplyMetadata;
import com.slack.lab.core.model.SlackSendResult;
import com.slack.lab.core.port.ChatNotifier;

/**
 * Grafana 알람 리포트에서 "문서 근거를 찾지 못했다"는 검색 안내 푸터를 뺀다. 알람은 검색을 건너뛰어(검색 우회) 항상 근거 없음으로
 * 나오므로, 푸터를 그대로 두면 모든 리포트에 의미 없는 경고가 붙는다. 이벤트 ID 접두가 {@code alert-grafana-}일 때만 바꾸고, 일반
 * 멘션과 CloudWatch 알람 답글은 그대로 위임한다.
 */
public class AlertFooterChatNotifier implements ChatNotifier {

    private static final String GRAFANA_EVENT_PREFIX = "alert-grafana-";

    private final ChatNotifier delegate;

    public AlertFooterChatNotifier(ChatNotifier delegate) {
        this.delegate = delegate;
    }

    @Override
    public SlackSendResult postMessage(String channel, String threadTs, String text, ReplyFooter footer, long remainingMs,
            ReplyMetadata metadata) {
        boolean searchNotice = footer instanceof ReplyFooter.NoRelevantDocuments || footer instanceof ReplyFooter.SearchUnavailable;
        boolean grafana = metadata != null && metadata.eventId() != null && metadata.eventId().startsWith(GRAFANA_EVENT_PREFIX);
        return delegate.postMessage(channel, threadTs, text, grafana && searchNotice ? ReplyFooter.NONE : footer, remainingMs,
                metadata);
    }

    @Override
    public ReactionResult addReaction(String channel, String ts, String emoji) {
        return delegate.addReaction(channel, ts, emoji);
    }
}
