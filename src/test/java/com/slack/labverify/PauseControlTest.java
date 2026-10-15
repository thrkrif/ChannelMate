package com.slack.labverify;

import static org.assertj.core.api.Assertions.assertThat;

import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class PauseControlTest {

    private static final class Counting implements PauseControl.Switch {
        int starts;
        int closes;

        @Override
        public void start() {
            starts++;
        }

        @Override
        public void close() {
            closes++;
        }
    }

    @Test
    void 정지_정지_재개_재개는_close_한_번_start_한_번(@TempDir Path dir) throws Exception {
        Path flag = dir.resolve("pause");
        var target = new Counting();
        var control = new PauseControl(flag, target);

        Files.createFile(flag);
        assertThat(control.poll()).isTrue();
        assertThat(control.poll()).isFalse();
        Files.delete(flag);
        assertThat(control.poll()).isTrue();
        assertThat(control.poll()).isFalse();

        assertThat(target.closes).isEqualTo(1);
        assertThat(target.starts).isEqualTo(1);
        assertThat(control.state()).isEqualTo(PauseControl.State.RUNNING);
    }

    @Test
    void 플래그_없이_시작하면_첫_폴링에서_호출이_없다(@TempDir Path dir) throws Exception {
        var target = new Counting();
        var control = new PauseControl(dir.resolve("pause"), target);

        assertThat(control.poll()).isFalse();

        assertThat(target.closes).isZero();
        assertThat(target.starts).isZero();
    }

    @Test
    void 플래그가_있는_채_시작하면_첫_폴링에서_close_한_번_start_없음(@TempDir Path dir) throws Exception {
        Path flag = dir.resolve("pause");
        Files.createFile(flag);
        var target = new Counting();
        var control = new PauseControl(flag, target);

        assertThat(control.poll()).isTrue();

        assertThat(target.closes).isEqualTo(1);
        assertThat(target.starts).isZero();
        assertThat(control.state()).isEqualTo(PauseControl.State.PAUSED);
    }

    @Test
    void 소비자_호출이_실패하면_상태를_바꾸지_않아_다음_폴링이_재시도한다(@TempDir Path dir) throws Exception {
        Path flag = dir.resolve("pause");
        Files.createFile(flag);
        int[] calls = {0};
        var control = new PauseControl(flag, new PauseControl.Switch() {
            @Override
            public void start() {
            }

            @Override
            public void close() throws Exception {
                if (calls[0]++ == 0) {
                    throw new IllegalStateException("일시 실패");
                }
            }
        });

        org.assertj.core.api.Assertions.assertThatThrownBy(control::poll).isInstanceOf(IllegalStateException.class);
        assertThat(control.state()).isEqualTo(PauseControl.State.RUNNING);
        assertThat(control.poll()).isTrue();
        assertThat(control.state()).isEqualTo(PauseControl.State.PAUSED);
    }
}
