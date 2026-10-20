"""B4 앱 모드 세션 준비(`monitoring-verify alarm-up --mode app`): 스텁 → compose override → 앱 3역할 → 모니터링 → 준비 확인 → app-state.json.
외부 조작은 모두 주입받은 io로만 한다(단위 시험은 가짜 io, 컨테이너·프로세스 접근 0). 호출 순서가 곧 정리 순서의 역순이며,
중간에 실패하면 이미 한 단계들을 거꾸로 되돌리고 app-state.json을 만들지 않는다(준비 완료 표시는 맨 마지막 한 번만 쓴다).
기동 장부는 s6-session이 compose·spawn 호출마다 먼저 적는다. 여기서는 단계 기록(bench-ledger.jsonl)만 더한다.
"""
import json
import os
import sys
import time

import _mon_model as model

TEST_CHANNEL = "CSTUBVERIFY"
SLACK_PORT, LLM_PORT, RECEIVER_PORT = 18962, 18963, 8080
ROLES = ("receiver", "worker", "reactor")
REQUIRED = ("ALERT_CHANNEL", "ALERT_SECRET", "SLACK_SIGNING_SECRET", "GRAFANA_ADMIN_PASSWORD", "MANAGEMENT_METRICS_TOKEN")
OVERRIDE_NAME = "app-override.yaml"
STATE_NAME = "app-state.json"


class AppUpError(RuntimeError):
    pass


def override_lines(failure=False):
    """앱 컨테이너가 호스트의 기록용 스텁으로 나가고, 로그·일시정지 플래그는 세션 디렉터리(/logs 마운트)에 둔다."""
    values = {"SLACK_BASEURL": f"http://host.docker.internal:{SLACK_PORT}", "SLACK_LAB_VERIFY_PAUSE_FLAG": "/logs/pause.flag",
              "SLACK_LAB_LOG_DIR": "/logs", "EXPERIMENT_SLOWMODEMS": "0", "EXPERIMENT_HALTAFTERSEND": "false", "RETRY_MAXRETRIES": "3"}
    if failure:
        values.update(LLM_BASE_URL="http://localhost:9/v1", LLM_VERIFYMODELONSTARTUP="false")
    return [k + ": " + json.dumps(v) for k, v in values.items()]


def with_log_mount(text, sdir):
    return text.replace("    environment:\n", "    volumes:\n      - " + json.dumps(sdir + ":/logs") + "\n    environment:\n")


class AppUp:
    def __init__(self, sdir, io):
        self.sdir, self.io = os.path.abspath(sdir), io
        self.undo = []      # (이름, 함수) 쌓기. 실패 시 거꾸로 실행한다.
        self.steps = []
        self.errors = []

    def record(self, step, ok=True):
        self.steps.append({"step": step, "ok": ok})
        self.io.ledger_line({"kind": "app-up", "step": step, "ok": ok, "at": self.io.now()})

    def check_before(self):
        env = self.io.env
        if any(not env.get(k) for k in REQUIRED) or env["ALERT_CHANNEL"] == TEST_CHANNEL:
            raise ValueError("필수 환경 키 또는 알람·테스트 채널 분리 확인 필요")
        if os.path.exists(os.path.join(self.sdir, STATE_NAME)):
            raise ValueError("이미 초기화한 앱 세션")
        busy = [p for p in (RECEIVER_PORT, SLACK_PORT, LLM_PORT) if self.io.listening(p)]
        if busy:
            raise ValueError("B4 전용 포트가 이미 사용 중")

    def up(self):
        """반환 종료 코드: 0 준비됨, 3 규칙 불일치, 1 실패(되돌림 완료 또는 되돌림 오류 보고)."""
        self.check_before()
        try:
            self.start_stubs()
            self.write_override()
            self.start_app()
            self.start_monitoring()
            self.wait_ready()
            if self.io.rules_problems():
                self.record("rules-parity", False)
                self.rollback()
                return model.EXIT_RULES
            self.record("rules-parity")
            self.write_state()
            return model.EXIT_OK
        except (OSError, ValueError, RuntimeError):
            self.rollback()
            return model.EXIT_FAIL

    def start_stubs(self):
        for name, kind, port in (("app-slack", "slack", SLACK_PORT), ("app-llm", "llm", LLM_PORT)):
            pid = self.io.spawn_stub(name, kind, port)
            self.undo.append((name, lambda pid=pid: self.io.kill(pid)))
            self.record("stub-" + kind)
        self.io.wait(lambda: self.io.slack(f"http://127.0.0.1:{SLACK_PORT}").request("/__counts") is not None, 30, "Slack 스텁 준비 실패")
        self.io.wait(lambda: self.io.slack(f"http://127.0.0.1:{LLM_PORT}").request("/__stats") is not None, 30, "LLM 스텁 준비 실패")
        client = self.io.slack(f"http://127.0.0.1:{SLACK_PORT}")
        client.mode(TEST_CHANNEL, "ok")
        client.mode(self.io.env["ALERT_CHANNEL"], "ok")

    def write_override(self, failure=False):
        path = self.io.write_override(self.sdir, OVERRIDE_NAME, override_lines(failure))
        with open(path, encoding="utf-8") as stream:
            text = stream.read()
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(with_log_mount(text.replace("LLM_CLIENT: echo", "LLM_CLIENT: ollama") if failure else text, self.sdir))
        self.undo.append(("override", lambda: os.path.exists(path) and os.remove(path)))
        self.override = path
        self.record("override")

    def compose(self, *args):
        rc, out = self.io.s6("compose", "--", "-f", os.path.join(self.io.root, "compose.yaml"), "-f", self.override, "--profile", "app", *args)
        if rc:
            raise RuntimeError("compose 실패")
        return out

    def start_app(self):
        self.io.s6("mark", "monitoring")
        # 되돌림을 먼저 쌓는다: up이 중간에 실패해도 만들어진 컨테이너를 멈춘다.
        self.undo.append(("app", lambda: self.compose("stop", *ROLES)))
        self.compose("up", "-d", "--no-build", "--pull", "never", "--wait", *ROLES)
        self.record("app-compose-up")

    def start_monitoring(self):
        self.undo.append(("monitoring", lambda: self.io.run([os.path.join(self.io.root, "scripts", "monitoring"), "stop"], self.io.mon_env())))
        rc, _ = self.io.run([os.path.join(self.io.root, "scripts", "monitoring"), "start", "--app"], self.io.mon_env())
        if rc:
            raise RuntimeError("모니터링 기동 실패")
        self.record("monitoring-start")

    def wait_ready(self):
        self.io.wait(self.io.grafana_healthy, 120, "Grafana 준비 실패")
        self.io.wait(lambda: all(self.io.up_value(role) == 1.0 for role in ROLES), 120, "Prometheus 대상 up 확인 실패")
        self.io.wait(self.io.receiver_health, 60, "수신 health 확인 실패")
        self.record("ready-check")

    def write_state(self):
        data = {"ready": True, "project": self.io.project, "receiver_url": f"http://127.0.0.1:{RECEIVER_PORT}",
                "slack_url": f"http://127.0.0.1:{SLACK_PORT}", "llm_url": f"http://127.0.0.1:{LLM_PORT}", "roles": list(ROLES),
                "override": os.path.basename(self.override)}
        path = os.path.join(self.sdir, STATE_NAME)
        with open(path + ".tmp", "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(path + ".tmp", path)
        self.record("state-written")

    def rollback(self):
        """거꾸로 되돌린다. 한 단계가 실패해도 나머지를 계속하고, 실패한 이름만 모아 알린다(세부 비밀 없음)."""
        while self.undo:
            name, step = self.undo.pop()
            try:
                step()
            except (OSError, ValueError, RuntimeError):
                self.errors.append(name)
        state = os.path.join(self.sdir, STATE_NAME)
        if os.path.exists(state):
            os.remove(state)
        self.record("rollback" + ("" if not self.errors else ":" + ",".join(self.errors)), not self.errors)


class RealIo:
    """실제 조작 구현. 단위 시험 대상이 아니다(오프라인 시험은 가짜 io로 AppUp을 검증)."""

    def __init__(self, sdir, mv, env, project, grafana, root):
        import _mon_alarm
        import subprocess
        self.mv, self.env, self.project, self.g, self.root, self.sdir = mv, env, project, grafana, root, sdir
        self._sp, self._alarm = subprocess, _mon_alarm

    def now(self):
        return time.time()

    def ledger_line(self, value):
        with open(os.path.join(self.sdir, "bench-ledger.jsonl"), "a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")

    def s6(self, *args):
        p = self.mv.s6(args[0], "--session", self.sdir, *args[1:], check=False)
        return p.returncode, p.stdout

    def run(self, args, env=None):
        p = self._sp.run(args, cwd=self.root, env=env, capture_output=True, text=True)
        return p.returncode, p.stdout

    def mon_env(self):
        env = dict(os.environ, **self.env)
        env.update(S6_SESSION=self.sdir, SLACK_LAB_COMPOSE_PROJECT=self.project, SLACK_LAB_LOG_DIR_HOST=self.sdir)
        return env

    def listening(self, port):
        return bool(self.mv.lsof_listen(port).strip())

    def spawn_stub(self, name, kind, port):
        rc, out = self.s6("spawn", "--name", name, "--env-file", os.path.join(self.root, ".env"), "--",
                          sys.executable, os.path.join(self.root, "scripts", "monitoring-verify"), "stub", kind, "--port", str(port), "--host", "0.0.0.0")
        if rc:
            raise RuntimeError("스텁 기동 실패")
        return int(out.strip())

    def kill(self, pid):
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def slack(self, base):
        return self._alarm.StubClient(base)

    def write_override(self, sdir, name, lines):
        return self.mv.write_override(sdir, name, lines)

    def wait(self, predicate, seconds, reason):
        end = time.time() + seconds
        while time.time() <= end:
            try:
                if predicate():
                    return
            except OSError:
                pass
            time.sleep(1)
        raise RuntimeError(reason)

    def grafana_healthy(self):
        return self.g.healthy()

    def up_value(self, role):
        code, body = self.g.query("prom", f'up{{job="slack-lab",role="{role}"}}')
        result = body.get("data", {}).get("result", []) if code == 200 and isinstance(body, dict) else []
        return float(result[0]["value"][1]) if result else None

    def receiver_health(self):
        return self.mv.raw_status("127.0.0.1", RECEIVER_PORT, "GET", "/health", "") == "200"

    def rules_problems(self):
        return self._alarm.parity(self.g, "rules-app.json")
