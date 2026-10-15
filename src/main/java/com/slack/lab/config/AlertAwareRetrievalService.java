package com.slack.lab.config;

import com.slack.lab.adapter.alert.GrafanaAlertMarker;
import com.slack.lab.core.model.Retrieval;
import com.slack.lab.core.service.RetrievalService;

/**
 * Grafana 알람은 정규화기가 만든 고정 한 줄이라 검색할 내용이 없다. 임베딩 서버를 부르지 않고 "관련 문서 없음"으로 돌려준다 —
 * 임베딩이 죽은 순간에도 알람 리포트가 나가야 하기 때문이다(알람은 장애 중에 온다). 이 우회는 {@code rag_result} 줄을 남기지 않는다:
 * 시스템 수준의 {@code rag_result} 계약은 "알람 우회에서는 0줄, 그 밖의 운영 검색은 호출당 1줄"이다. {@link RetrievalService#retrieve}
 * 자체는 여전히 호출당 1줄을 남기며, 이 클래스가 그 앞에서 호출을 가로챌 뿐이다(5단계 계약 변경, PLAN 6단계 D-7).
 */
public class AlertAwareRetrievalService extends RetrievalService {

    public AlertAwareRetrievalService(com.slack.lab.core.port.EmbeddingClient embedding, com.slack.lab.core.port.VectorStore store,
            Config config) {
        super(embedding, store, config);
    }

    @Override
    public Retrieval retrieve(String promptText, long budgetMs) {
        if (GrafanaAlertMarker.ruleOf(promptText).isPresent()) {
            return new Retrieval.NoRelevant(0);
        }
        return super.retrieve(promptText, budgetMs);
    }
}
