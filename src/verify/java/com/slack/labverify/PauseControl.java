package com.slack.labverify;

import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 검증 전용: 플래그 파일 유무로 큐 소비를 멈췄다 재개한다. 운영 코드(main)는 이 파일을 모르고, 운영 jar에도 들어가지 않는다.
 * 알람 ③(큐 지속 적체)은 소비가 멈춘 채 메시지가 쌓여야 성립해서 필요하다. 같은 상태가 반복되는 신호는 무시해,
 * 폴링이 몇 번 돌든 소비자의 close/start는 전이 때 한 번씩만 불린다.
 */
public final class PauseControl {

    /** 멈추고 다시 켤 수 있는 대상(운영의 RabbitConsumer가 구현체로 감싸진다). */
    public interface Switch {
        void start() throws Exception;

        void close() throws Exception;
    }

    public enum State { RUNNING, PAUSED }

    private final Path flag;
    private final Switch target;
    // 빈 초기화에서 initMethod="start"가 이미 불렸으므로 시작 상태는 RUNNING이다(RabbitConfig).
    private State state = State.RUNNING;

    public PauseControl(Path flag, Switch target) {
        this.flag = flag;
        this.target = target;
    }

    /** 폴링 한 번. 전이가 있으면 true. 소비자 호출이 실패하면 상태를 바꾸지 않고 예외를 올린다(다음 폴링이 재시도). */
    public synchronized boolean poll() throws Exception {
        boolean wantPaused = Files.exists(flag);
        if (wantPaused && state == State.RUNNING) {
            target.close();
            state = State.PAUSED;
            return true;
        }
        if (!wantPaused && state == State.PAUSED) {
            target.start();
            state = State.RUNNING;
            return true;
        }
        return false;
    }

    public synchronized State state() {
        return state;
    }
}
