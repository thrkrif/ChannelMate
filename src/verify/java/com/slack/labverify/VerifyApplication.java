package com.slack.labverify;

import com.slack.lab.SlackLabApplication;
import com.slack.lab.adapter.rabbitmq.RabbitConsumer;
import java.nio.file.Path;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.builder.SpringApplicationBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 검증 전용 진입점(Gradle {@code verifyBootRun}). 운영 애플리케이션을 그대로 띄우고 소비 일시정지 제어만 얹는다.
 * 플래그 경로는 환경변수 {@code SLACK_LAB_VERIFY_PAUSE_FLAG}이며 없으면 제어를 켜지 않는다(운영과 같은 동작).
 */
public final class VerifyApplication {

    private VerifyApplication() {
    }

    public static void main(String[] args) {
        new SpringApplicationBuilder(SlackLabApplication.class, VerifyConfiguration.class).run(args);
    }

    @Configuration(proxyBeanMethods = false)
    static class VerifyConfiguration {

        private static final Logger log = LoggerFactory.getLogger(VerifyConfiguration.class);

        @Bean(destroyMethod = "interrupt")
        Thread pauseWatcher(ObjectProvider<RabbitConsumer> consumer) {
            String flag = System.getenv("SLACK_LAB_VERIFY_PAUSE_FLAG");
            RabbitConsumer rabbit = consumer.getIfAvailable();
            Thread t = new Thread(() -> { }, "verify-pause-idle");
            if (flag == null || flag.isBlank() || rabbit == null) {
                return t;
            }
            PauseControl control = new PauseControl(Path.of(flag), new PauseControl.Switch() {
                @Override
                public void start() throws Exception {
                    rabbit.start();
                }

                @Override
                public void close() {
                    rabbit.close();
                }
            });
            t = new Thread(() -> {
                while (!Thread.currentThread().isInterrupted()) {
                    try {
                        if (control.poll()) {
                            log.info("검증 소비 제어 상태={}", control.state());
                        }
                        Thread.sleep(500);
                    } catch (InterruptedException e) {
                        return;
                    } catch (Exception e) {
                        log.warn("검증 소비 제어 실패 reason={}", e.getClass().getSimpleName());
                    }
                }
            }, "verify-pause-watcher");
            t.setDaemon(true);
            t.start();
            return t;
        }
    }
}
