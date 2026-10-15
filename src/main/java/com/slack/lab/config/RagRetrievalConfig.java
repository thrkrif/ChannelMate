package com.slack.lab.config;

import com.slack.lab.core.port.EmbeddingClient;
import com.slack.lab.core.port.VectorStore;
import com.slack.lab.core.service.RetrievalService;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/** 워커(질의)와 색인 CLI(평가 모드)가 RAG를 켰을 때만 검색 서비스를 만든다. 없으면 핸들러는 기존 흐름 그대로 간다(꺼도 동작). */
@Configuration
@ConditionalOnRole({AppRole.WORKER, AppRole.INDEXER, AppRole.ALL})
@ConditionalOnProperty(prefix = "rag", name = "enabled", havingValue = "true")
class RagRetrievalConfig {

    /** 워커·all은 알람 우회가 있는 검색 서비스, 색인(평가)은 기존 그대로다. */
    @Bean
    @ConditionalOnRole({AppRole.WORKER, AppRole.ALL})
    RetrievalService retrievalService(EmbeddingClient embedding, VectorStore store, RagProperties rag,
            RagRetrievalProperties retrieval) {
        return new AlertAwareRetrievalService(embedding, store, config(rag, retrieval));
    }

    @Bean("retrievalService")
    @ConditionalOnRole({AppRole.INDEXER})
    RetrievalService indexerRetrievalService(EmbeddingClient embedding, VectorStore store, RagProperties rag,
            RagRetrievalProperties retrieval) {
        return new RetrievalService(embedding, store, config(rag, retrieval));
    }

    private static RetrievalService.Config config(RagProperties rag, RagRetrievalProperties retrieval) {
        return new RetrievalService.Config(rag.searchDeadlineMs(), retrieval.topK(), retrieval.minScore(),
                retrieval.maxContextChars(), retrieval.maxQueryChars());
    }
}
