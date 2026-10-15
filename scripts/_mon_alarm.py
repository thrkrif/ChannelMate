"""B3 세션의 앱 교체와 로컬 스텁 접근을 한곳에 둔다."""
import glob
import gzip
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

import _mon_env as me
import _mon_model as model
import _mon_spike as ms

ROOT = me.ROOT
TEST_CHANNEL = "CSTUBVERIFY"


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def logs(sdir):
    rows = {}
    for path in glob.glob(os.path.join(sdir, "all", "slack-lab.log*")):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    ts = timestamp(row["ts"])
                    rows[(ts, line)] = row
                except (ValueError, KeyError, TypeError):
                    pass
    return sorted(rows.values(), key=lambda r: timestamp(r["ts"]))


class StubClient:
    def __init__(self, base):
        self.base = base.rstrip("/")

    def request(self, path, body=None):
        req = urllib.request.Request(self.base + path,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def count(self, method, channel):
        return self.request("/__counts").get(method, {}).get(channel, 0)

    def mode(self, channel, mode):
        self.request("/__control", {"channel": channel, "mode": mode})


def parity(grafana):
    path = os.path.join(ROOT, "infra/monitoring/grafana/provisioning/alerting/rules-host.json")
    with open(path, encoding="utf-8") as stream:
        rules = model.rules_from_files([json.load(stream)])
    code, api = grafana.request("GET", "/api/v1/provisioning/alert-rules")
    return model.parity(rules, api if code == 200 and isinstance(api, list) else [])


class AlarmSession:
    def __init__(self, sdir, mv):
        self.sdir, self.mv = os.path.abspath(sdir), mv
        with open(os.path.join(sdir, "ledger.json"), encoding="utf-8") as stream:
            self.project = json.load(stream)["project"]
        with open(os.path.join(sdir, "jar.json"), encoding="utf-8") as stream:
            self.jar = json.load(stream)["jar"]
        self.env = me.read_env_file(os.path.join(ROOT, ".env"))
        self.path = os.path.join(sdir, "alarm-state.json")
        self.data = {}
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as stream:
                self.data = json.load(stream)
        self.grafana = ms.Grafana(self.env.get("GRAFANA_ADMIN_PASSWORD", ""))
        self.slack = StubClient(self.data.get("slack_url", "http://127.0.0.1:18962"))
        self.tick = lambda: None
        self.sleep = time.sleep
        self.interruptions = []

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(self.data, stream)
        os.replace(tmp, self.path)

    def wait(self, predicate, seconds, reason):
        end = time.time() + seconds
        while time.time() <= end:
            self.tick()
            try:
                ready = predicate()
            except OSError:
                ready = False
            if ready:
                return
            self.sleep(1)
        raise RuntimeError(reason)

    def spawn(self, name, command, settings=None):
        args = ["spawn", "--session", self.sdir, "--name", name, "--env-file", os.path.join(ROOT, ".env")]
        for key, value in (settings or {}).items():
            args += ["--env", key + "=" + value]
        return int(self.mv.s6(*args, "--", *command).stdout.strip())

    def health(self):
        return self.mv.raw_status("127.0.0.1", 8080, "GET", "/health", "") == "200"

    def restart_app(self, mode):
        if mode not in ("echo", "F-echo", "F-echo-stub"):
            raise ValueError("지원하지 않는 앱 모드")
        stopped = None
        pid = self.data.get("jvm_pid")
        if pid:
            with open(os.path.join(self.sdir, "ledger.json"), encoding="utf-8") as stream:
                owned = json.load(stream).get("pids", [])
            if not any(row.get("pid") == pid and row.get("name") == "alarm-app-jvm" and row.get("signal_pid") for row in owned):
                raise RuntimeError("앱 JVM의 세션 소유 확인 실패")
            stopped = time.time()
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.wait(lambda: not self.health(), 30, "앱 JVM 종료 확인 실패")
            self.wait(lambda: not self.mv.lsof_listen(8080).strip(), 30, "앱 포트 종료 확인 실패")
        before = len(logs(self.sdir))
        settings = {"APP_ROLE": "all", "LLM_CLIENT": "echo", "RAG_ENABLED": "false", "CLASSIFICATION_ENABLED": "false",
                    "SLACK_BASEURL": self.slack.base, "SLACK_LAB_VERIFY_PAUSE_FLAG": os.path.join(self.sdir, "pause.flag"),
                    "SLACK_LAB_LOG_DIR": self.sdir, "EXPERIMENT_SLOWMODEMS": "0", "RETRY_MAXRETRIES": "3",
                    "EXPERIMENT_HALTAFTERSEND": "false",
                    "RABBITMQ_HOST": "localhost", "POSTGRES_URL": "jdbc:postgresql://localhost:5432/slacklab"}
        if mode != "echo":
            settings.update(LLM_CLIENT="ollama", LLM_VERIFYMODELONSTARTUP="false",
                            LLM_BASE_URL="http://localhost:9/v1" if mode == "F-echo" else "http://127.0.0.1:18963/v1")
            if mode == "F-echo-stub":
                settings["LLM_MODEL"] = "stub-model"
        self.spawn("alarm-app", ["./gradlew", "verifyBootRun", "-q"], settings)
        self.wait(lambda: self.health() and any("워커 소비자 시작" in r.get("msg", "") for r in logs(self.sdir)[before:]),
                  300, "앱 health·새 워커 시작 확인 실패")
        if stopped is not None:
            self.interruptions.append(time.time() - stopped)
        if mode != "echo":
            expected = "Failed" if mode == "F-echo" else "Success"
            self.wait(lambda: any("모델 확인을 건너뜀" in r.get("msg", "") for r in logs(self.sdir)[before:])
                      and any("LLM 웜업 결과=" + expected in r.get("msg", "") for r in logs(self.sdir)[before:]),
                      90, "실패 유도 웜업 확인 실패")
        p = subprocess.run(["lsof", "-t", "-iTCP:8080", "-sTCP:LISTEN"], capture_output=True, text=True)
        pids = set(p.stdout.split())
        if len(pids) != 1:
            raise RuntimeError("앱 JVM PID 확인 실패")
        self.data.update(jvm_pid=int(pids.pop()), mode=mode)
        with open(os.path.join(self.sdir, "ledger.json"), encoding="utf-8") as stream:
            ledger = json.load(stream)
        ledger["pids"].append({"pid": self.data["jvm_pid"], "name": "alarm-app-jvm", "signal_pid": True})
        with open(os.path.join(self.sdir, "ledger.json"), "w", encoding="utf-8") as stream:
            json.dump(ledger, stream)
        self.save()

    def up(self):
        required = ("ALERT_CHANNEL", "ALERT_SECRET", "SLACK_SIGNING_SECRET", "GRAFANA_ADMIN_PASSWORD", "MANAGEMENT_METRICS_TOKEN")
        if any(not self.env.get(key) for key in required) or self.env["ALERT_CHANNEL"] == TEST_CHANNEL:
            raise ValueError("필수 환경 키 또는 알람·테스트 채널 분리 확인 필요")
        if self.data:
            raise ValueError("이미 초기화한 알람 세션")
        if self.mv.lsof_listen(8080).strip() or self.mv.lsof_listen(18962).strip() or self.mv.lsof_listen(18963).strip():
            raise ValueError("B3 전용 포트가 이미 사용 중")
        self.mv.s6("compose", "--session", self.sdir, "--", "up", "-d", "--wait", "rabbitmq", "postgres")
        for name, kind, port in (("alarm-slack", "slack", 18962), ("alarm-llm", "llm", 18963)):
            self.spawn(name, [sys.executable, os.path.join(ROOT, "scripts/monitoring-verify"), "stub", kind, "--port", str(port)])
        self.wait(lambda: self.slack.request("/__counts") is not None, 30, "Slack 스텁 준비 실패")
        self.wait(lambda: StubClient("http://127.0.0.1:18963").request("/__stats") is not None, 30, "LLM 스텁 준비 실패")
        self.slack.mode(TEST_CHANNEL, "ok")
        self.slack.mode(self.env["ALERT_CHANNEL"], "ok")
        env = dict(os.environ, **self.env)
        env.update(S6_SESSION=self.sdir, SLACK_LAB_COMPOSE_PROJECT=self.project, SLACK_LAB_LOG_DIR_HOST=self.sdir,
                   GRAFANA_ALERT_WEBHOOK_URL="http://host.docker.internal:8080/alerts/grafana")
        self.mv.s6("mark", "--session", self.sdir, "monitoring")
        p = subprocess.run([os.path.join(ROOT, "scripts/monitoring"), "start", "--host"], env=env, cwd=ROOT, capture_output=True)
        if p.returncode:
            raise RuntimeError("모니터링 기동 실패")
        self.restart_app("echo")
        self.wait(self.grafana.healthy, 120, "Grafana 준비 실패")
        def target_up():
            code, body = self.grafana.query("prom", 'up{job="slack-lab",role="all"}')
            result = body.get("data", {}).get("result", []) if isinstance(body, dict) else []
            return code == 200 and bool(result) and all(float(row["value"][1]) == 1 for row in result)
        self.wait(target_up, 90, "Prometheus 대상 up 확인 실패")
        if parity(self.grafana):
            return model.EXIT_RULES
        ok, _ = me.db_target_check(self.project)
        if not ok:
            raise RuntimeError("복구 CLI DB 대상 확인 실패")
        delays = []
        for _ in range(3):
            code, out, (start, end) = me.run_jar_cli(self.jar, "recovery", ["list"], self.sdir, self.project)
            if code or "미해결 건 없음" not in out:
                raise RuntimeError("세션 DB에 미해결 건이 있음")
            delays.append(end-start)
        self.data.update(ready=True, jar_delay=max(delays), project=self.project, receiver_url="http://127.0.0.1:8080",
                         slack_url=self.slack.base, llm_url="http://127.0.0.1:18963")
        self.save()
        return model.EXIT_OK

    def real_env(self):
        if not self.data.get("ready") or self.data.get("project") != self.project:
            raise ValueError("alarm-up 완료 세션이 필요하다")
        return me.RealEnv(self.sdir, self.project, self.grafana, self.slack, self.env["ALERT_CHANNEL"],
                          self.data["receiver_url"], self.env["SLACK_SIGNING_SECRET"], "host", session=self)
