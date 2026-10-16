"""M49a B1·B2·B3·B4 콜드 기동 측정. echo-mon은 같은 환경에서 알람 시나리오로 이어진다."""
import argparse
import glob
import gzip
import hashlib
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import time
import urllib.request

import _mon_env
import _mon_alarm
import _mon_appup
import _mon_spike as ms
import _mon_model as model

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = ("receiver", "worker", "reactor")
EXPECTED_FAILURE = {"attempts": 20, "retries": 15, "llm_final_failure": 5, "notices": 5, "replies": 0, "received": 5}


class BenchError(Exception):
    pass


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, value):
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)


def round_entry(sdir, value):
    with open(os.path.join(sdir, "bench-ledger.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False) + "\n")


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def counts(rows):
    # 모니터링을 켠 행에서는 실패 단계 부하가 규칙 ②를 실제로 발화시켜 알람 리포트(event_id=alert-grafana-…) 처리 지표가 같은 로그에 섞인다.
    # 사용자 멘션 집계에서는 빼야 F-echo 기대 사건 수(시도 20·답글 0)와 맞는다. 알람 리포트 수는 따로 센다.
    alerts = [r for r in rows if r.get("event") == "processing_metric" and "event_id=alert-grafana-" in r.get("msg", "")]
    rows = [r for r in rows if r not in alerts]
    msgs = [r.get("msg", "") for r in rows]
    return {"attempts": sum(r.get("event") == "processing_metric" for r in rows),
            "retries": sum("LLM 재시도 가능한 실패 → 재시도 예약" in m for m in msgs),
            "llm_final_failure": sum(r.get("event") == "llm_final_failure" for r in rows),
            "received": sum("slack 수신 retry_num=" in m for m in msgs),
            "notices": sum(r.get("event") == "processing_metric" and r.get("fields", {}).get("kind") == "failure_notice"
                           and r.get("fields", {}).get("result") == "Delivered" for r in rows),
            "replies": sum(r.get("event") == "processing_metric" and r.get("fields", {}).get("kind") == "answer"
                           and r.get("fields", {}).get("result") == "Delivered" for r in rows)}


def failure_evidence(rows, summary):
    got = counts(rows)
    return got, got == EXPECTED_FAILURE and summary["failure_notice"] == 5 and summary["ok"] == 0


class Bench:
    def __init__(self, opts, mv):
        self.o, self.mv = opts, mv
        self.sdir = os.path.realpath(opts.session)
        self.hwdir = os.path.join(self.sdir, "hw-" + opts.mode)
        self.out = os.path.realpath(opts.out or os.path.join(self.sdir, "bench-" + opts.mode + ".json"))
        self.env = _mon_env.jar_env(self.sdir, "all", "")
        self.app_pid = None
        self.jvm_pids = []
        self.started = False
        self.row = getattr(opts, "row", "echo")
        self.alarm = None
        self.appup = None
        self.result = {"mode": opts.mode, "row": self.row, "ok": False, "loads": []}

    def call(self, args, check=True, env=None):
        p = subprocess.run(args, cwd=ROOT, env=env or self.env, capture_output=True, text=True)
        if check and p.returncode:
            # 하위 명령의 stderr에는 설정값이 섞일 수 있어 원문을 전달하지 않는다.
            raise BenchError("하위 명령 실패: " + os.path.basename(args[0]))
        return p

    def s6(self, command, *args):
        return self.call([os.path.join(ROOT, "scripts", "s6-session"), command, "--session", self.sdir, *args])

    def hw(self, command, *args, check=True):
        return self.call([os.path.join(ROOT, "scripts", "hw-measure"), command, *args, "--out", self.hwdir], check=check)

    def wait(self, predicate, seconds, reason):
        deadline = time.monotonic() + seconds
        while True:
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise BenchError(reason)
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def register(self, pid, name, direct=False):
        ledger = read_json(os.path.join(self.sdir, "ledger.json"))
        ledger["pids"].append({"pid": pid, "name": name, "started_at": time.time(), "signal_pid": direct})
        write_json(os.path.join(self.sdir, "ledger.json"), ledger)

    def preflight(self):
        if os.path.commonpath([ROOT, self.sdir]) == ROOT or os.path.commonpath([self.sdir, self.out]) != self.sdir:
            raise BenchError("세션과 결과는 저장소 밖 세션 디렉터리에 있어야 한다")
        try:
            self.ledger = read_json(os.path.join(self.sdir, "ledger.json"))
            self.meta = read_json(os.path.join(self.sdir, "jar.json"))
            self.project = self.ledger["project"]
            if not self.project.startswith("slack-lab-s6v-") or digest(self.meta["jar"]) != self.meta["sha256"]:
                raise ValueError()
        except (OSError, ValueError, KeyError):
            raise BenchError("session start의 세션·jar 기록이 없거나 SHA-256이 다르다")
        if os.path.exists(self.hwdir):
            raise BenchError("측정 디렉터리가 이미 있다: 회차 재사용 금지")
        for key in ("SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN", "SLACK_TEST_CHANNEL", "ALERT_CHANNEL"):
            if not self.env.get(key):
                raise BenchError("필수 환경변수 누락: " + key)
        listeners = self.call(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], check=False)
        if listeners.returncode not in (0, 1):
            raise BenchError("리스너 확인 실패")
        import re
        if re.search(r":(?:8080|9091|3000)(?:\s|$)", listeners.stdout):
            raise BenchError("사전 조건: 8080/9091/3000 리스너가 남아 있다")
        if any(x.strip() for x in self.call(["ollama", "ps"]).stdout.splitlines()[1:]):
            raise BenchError("사전 조건: Ollama 모델이 남아 있다")
        if self.call(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project]).stdout.strip():
            raise BenchError("사전 조건: 같은 프로젝트 컨테이너가 남아 있다")
        if self.ledger.get("compose") or self.ledger.get("pids"):
            raise BenchError("사전 조건: 새 세션이 필요하다")
        if self.call(["docker", "volume", "ls", "-q", "--filter", "name=^" + self.project + "_"]).stdout.strip():
            raise BenchError("사전 조건: 같은 프로젝트 볼륨이 남아 있다")
        self.env["SLACK_LAB_COMPOSE_PROJECT"] = self.project
        if self.row == "echo-mon":
            self.env["SLACK_TEST_CHANNEL"] = _mon_appup.TEST_CHANNEL
            if self.o.mode == "app":
                env = _mon_env.read_env_file(os.path.join(ROOT, ".env"))
                if env.get("ALERT_CHANNEL") == _mon_appup.TEST_CHANNEL:
                    raise BenchError("알람·테스트 채널 분리 확인 필요")
                io = _mon_appup.RealIo(self.sdir, self.mv, env, self.project,
                                       ms.Grafana(env.get("GRAFANA_ADMIN_PASSWORD", "")), ROOT)
                self.appup = _mon_appup.AppUp(self.sdir, io)
            else:
                self.alarm = _mon_alarm.AlarmSession(self.sdir, self.mv)

    def compose(self, *args):
        return self.s6("compose", "--", "-f", os.path.join(ROOT, "compose.yaml"),
                       "-f", self.override, "--profile", "app", *args)

    def image_check(self):
        if self.o.build_first:
            # 세션에서 검증한 jar를 넣어야 이미지와 측정 대상의 바이트가 같아진다.
            shutil.copyfile(self.meta["jar"], os.path.join(self.sdir, "bench-app.jar"))
            with open(os.path.join(self.sdir, "bench-Dockerfile"), "w", encoding="utf-8") as f:
                f.write('FROM eclipse-temurin:21-jre\nWORKDIR /app\nCOPY bench-app.jar app.jar\nENTRYPOINT ["java", "-jar", "/app/app.jar"]\n')
            self.compose("build", *SERVICES)
        image = self.call(["docker", "image", "inspect", "slack-lab-app", "--format", "{{.Id}}"], check=False)
        if image.returncode or not image.stdout.strip():
            raise BenchError("앱 이미지 없음: 측정 전에 --build-first가 필요하다")
        name = self.project + "-image-check"
        try:
            self.call(["docker", "create", "--name", name, "--label", "com.docker.compose.project=" + self.project, "slack-lab-app"])
            target = os.path.join(self.sdir, "image-check.jar")
            self.call(["docker", "cp", name + ":/app/app.jar", target])
            if digest(target) != self.meta["sha256"]:
                raise BenchError("앱 이미지 jar와 세션 jar가 다르다: 측정 전 빌드·세션 jar 갱신 필요")
        finally:
            removed = self.call(["docker", "rm", "-f", name], check=False)
            target = os.path.join(self.sdir, "image-check.jar")
            if os.path.exists(target):
                os.remove(target)
            if removed.returncode:
                raise BenchError("이미지 확인용 정지 컨테이너 제거 실패: teardown 필요")

    def spawn(self, name, command, extra=None):
        args = ["--name", name, "--env-file", os.path.join(ROOT, ".env")]
        for k, v in (extra or {}).items():
            args += ["--env", k + "=" + str(v)]
        return int(self.s6("spawn", *args, "--", *command).stdout.strip())

    def settings(self, failure=False):
        values = {"LLM_CLIENT": "ollama" if failure else "echo", "RAG_ENABLED": "false", "CLASSIFICATION_ENABLED": "false",
                  "SLACK_BASEURL": self.stub_app, "SLACK_LAB_VERIFY_PAUSE_FLAG": os.path.join(self.sdir, "pause.flag"),
                  "EXPERIMENT_SLOWMODEMS": "0", "EXPERIMENT_HALTAFTERSEND": "false", "RETRY_MAXRETRIES": "3"}
        if failure:
            values.update(LLM_BASE_URL="http://localhost:9/v1", LLM_VERIFYMODELONSTARTUP="false")
        return values

    def write_override(self, failure=False):
        if self.appup:
            self.appup.write_override(failure)
            self.override = self.appup.override
            if self.o.build_first:
                with open(self.override, encoding="utf-8") as stream:
                    text = stream.read()
                build = "    build:\n      context: " + json.dumps(self.sdir) + "\n      dockerfile: bench-Dockerfile\n"
                with open(self.override, "w", encoding="utf-8") as stream:
                    stream.write(text.replace("    environment:\n", build + "    environment:\n"))
            return
        values = self.settings(failure)
        extras = [k + ": " + json.dumps(v) for k, v in values.items() if k not in ("LLM_CLIENT", "RAG_ENABLED", "CLASSIFICATION_ENABLED")]
        extras.append('SLACK_LAB_LOG_DIR: "/logs"')
        path = self.mv.write_override(self.sdir, "bench-override.yaml", extras)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if failure:
            text = text.replace("LLM_CLIENT: echo", "LLM_CLIENT: ollama")
        mount = json.dumps(self.sdir + ":/logs")
        build = ("    build:\n      context: " + json.dumps(self.sdir) + "\n      dockerfile: bench-Dockerfile\n") if self.o.build_first else ""
        text = text.replace("    environment:\n", build + "    volumes:\n      - " + mount + "\n    environment:\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.override = path

    def logs(self):
        rows = []
        for role in (("all",) if self.o.mode == "host" else SERVICES):
            for path in sorted(glob.glob(os.path.join(self.sdir, role, "slack-lab.log*"))):
                opener = gzip.open if path.endswith(".gz") else open
                with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            pass
        return rows

    def health(self):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3) as r:
                return r.status == 200
        except OSError:
            return False

    def start_app(self, failure=False):
        before_rows = self.logs()
        before = sum("워커 소비자 시작" in r.get("msg", "") for r in before_rows)
        if self.o.mode == "host":
            extra = self.settings(failure)
            extra.update(APP_ROLE="all", RABBITMQ_HOST="localhost", POSTGRES_URL="jdbc:postgresql://localhost:5432/slacklab")
            if self.o.launcher == "jar":
                command = ["java", "-jar", self.meta["jar"]]
            else:
                command = [os.path.join(ROOT, "gradlew"), "verifyBootRun" if self.o.launcher == "verifyBootRun" else "bootRun", "-q"]
            self.app_pid = self.spawn("bench-app-failure" if failure else "bench-app", command, extra)
        else:
            self.write_override(failure)
            self.compose("up", "-d", "--no-build", "--pull", "never", *(["--no-deps", "--force-recreate"] if failure or self.appup else ["--wait"]), *SERVICES)
        self.wait(lambda: self.health() and sum("워커 소비자 시작" in r.get("msg", "") for r in self.logs()) > before,
                  300, "앱 health 또는 워커 소비자 시작 확인 실패")
        if self.o.mode == "host":
            pids = self.call(["lsof", "-t", "-iTCP:8080", "-sTCP:LISTEN"]).stdout.split()
            self.jvm_pids = [int(p) for p in set(pids)]
            if not self.jvm_pids:
                raise BenchError("앱 JVM PID 확인 실패")
            for pid in self.jvm_pids:
                self.register(pid, "bench-app-jvm", direct=True)
        if failure:
            def fresh(message):
                return sum(message in r.get("msg", "") for r in self.logs()) > sum(message in r.get("msg", "") for r in before_rows)
            self.wait(lambda: fresh("모델 확인을 건너뜀") and fresh("LLM 웜업 결과=Failed"), 90, "유도 문제: 모델 확인 생략·웜업 Failed 로그 없음")

    def stop_host_app(self):
        for pid in self.jvm_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.wait(lambda: not self.call(["lsof", "-t", "-iTCP:8080", "-sTCP:LISTEN"], check=False).stdout.strip(), 30, "앱 교체 전 종료 확인 실패")
        if self.app_pid:
            try:
                os.killpg(self.app_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def load(self, label, args, expected, failure=False):
        path = os.path.join(self.sdir, "load-" + label + ".json")
        self.spawn("load-" + label, [os.path.join(ROOT, "scripts", "p1-load"), *args],
                   {"SLACK_BASEURL": self.stub_host, "SLACK_TEST_CHANNEL": self.env["SLACK_TEST_CHANNEL"],
                    "P1_URL": "http://127.0.0.1:8080", "P1_REPLY_TIMEOUT": "240", "P1_SUMMARY_OUT": path})
        self.wait(lambda: os.path.isfile(path), 1800, "부하 완료 요약 없음")
        raw = read_json(path)
        s = raw["summary"]
        clean = {k: int(s[k]) for k in ("threads", "ok", "missing", "dup", "failure_notice", "language_violation")}
        clean["http_errors"] = int(raw["http_errors"])
        clean["phase"] = label
        self.result["loads"].append(clean)
        good = (clean["threads"] == expected and clean["missing"] == clean["dup"] == clean["http_errors"] == 0
                and clean["failure_notice"] == (expected if failure else 0) and clean["ok"] == (0 if failure else expected))
        if not good:
            raise BenchError("유도 문제: 부하 요약 불일치" if failure else "부하 누락·중복 또는 답글 건수 미달")
        return clean

    def stub_posts(self):
        with urllib.request.urlopen(self.stub_host + "/__counts", timeout=5) as r:
            data = json.load(r)
        return int(data.get("chat.postMessage", {}).get(self.env["SLACK_TEST_CHANNEL"], 0))

    def check_alarm_posts(self):
        slack = self.appup.io.slack(self.stub_host) if self.appup else self.alarm.slack
        env = self.appup.io.env if self.appup else self.alarm.env
        posts = slack.request("/__counts").get("chat.postMessage", {})
        alarm_posts = int(posts.get(env["ALERT_CHANNEL"], 0))
        self.result["alarm_posts"] = alarm_posts
        self.result["stub_posts_total"] = sum(int(n) for n in posts.values())
        if alarm_posts and not self.appup:
            raise BenchError("측정 중 알람 채널 게시 발생: 알람 시나리오 전 게시 0건 조건 미달")
        if self.result["stub_posts_total"] != self.result["stub_posts"] + (alarm_posts if self.appup else 0):
            raise BenchError("측정 중 테스트 채널 외 스텁 게시 발생")

    def run(self):
        self.preflight()
        port = 18962
        self.stub_host = "http://127.0.0.1:" + str(port)
        self.stub_app = ("http://host.docker.internal:" + str(port)) if self.o.mode == "app" else self.stub_host
        if self.o.mode == "app":
            self.write_override()
            self.image_check()
        elif self.o.build_first:
            raise BenchError("--build-first는 app 모드에서만 쓴다")
        self.result["cold_started"] = time.time()
        self.result["round"] = "B4" if self.appup else "B3" if self.alarm else ("B1" if self.o.mode == "host" else "B2")
        label = self.row + "-" + self.o.mode
        round_entry(self.sdir, {"event": "start", "round": self.result["round"], "purpose": label,
                                "started": self.result["cold_started"]})
        try:
            self.hw("start", "--label", label, "--ledger", os.path.join(self.sdir, "boot-ledger.jsonl"))
        finally:
            pid_file = os.path.join(self.hwdir, "hw-sampler.pid")
            if os.path.isfile(pid_file):
                self.started = True
                with open(pid_file, encoding="utf-8") as f:
                    self.register(int(f.read()), "hw-sampler")
        if self.alarm or self.appup:
            code = (self.appup or self.alarm).up()
            self.result["alarm_up_exit"] = code
            if code != model.EXIT_OK:
                self.result["exit"] = code
                raise BenchError("알람 규칙·지속 시간 불일치" if code == model.EXIT_RULES else "알람 환경 준비 실패")
            if not (read_json(os.path.join(self.sdir, _mon_appup.STATE_NAME)) if self.appup else self.alarm.data).get("ready"):
                raise BenchError("알람 환경 ready 확인 실패")
        else:
            self.spawn("bench-slack", [os.path.join(ROOT, "scripts", "monitoring-verify"), "stub", "slack", "--port", str(port),
                                       "--host", "0.0.0.0" if self.o.mode == "app" else "127.0.0.1"])
            self.wait(lambda: self.stub_ready(), 15, "Slack 스텁 준비 실패")
            self.s6("compose", "--", "-f", os.path.join(ROOT, "compose.yaml"), "up", "-d", "--wait", "rabbitmq", "postgres")
            self.start_app()
        self.load("warmup", ["warmup"], 2)
        self.hw("mark", "warmup")
        self.load("seq", ["seq", "20"], 20)
        self.load("burst", ["burst", "5", "2"], 10)
        self.load("normal", ["seq", "5"], 5)
        self.result["failure_restart_started"] = time.time()
        if self.alarm:
            self.alarm.restart_app("F-echo")
        else:
            if self.o.mode == "host":
                self.stop_host_app()
            self.start_app(True)
        self.result["failure_ready"] = time.time()
        before_rows = self.logs()
        posts = self.stub_posts()
        summary = self.load("failure", ["seq", "5"], 5, failure=True)
        # 회전 파일 정렬은 시간 순서와 다를 수 있어 목록의 위치로 구간을 자르지 않는다.
        before = counts(before_rows)
        after = counts(self.logs())
        got = {k: after[k] - before[k] for k in before}
        got["stub_posts"] = self.stub_posts() - posts
        self.result["failure_evidence"] = got
        if {k: got[k] for k in EXPECTED_FAILURE} != EXPECTED_FAILURE or got["stub_posts"] != 10:
            raise BenchError("유도 문제: F-echo 기대 사건 수 불일치")
        self.result["failure_finished"] = time.time()
        if self.alarm:
            self.alarm.restart_app("echo")
        elif self.appup:
            self.start_app()
        deadline = self.result["cold_started"] + self.o.minutes * 60
        while time.time() < deadline:
            if not self.health():
                raise BenchError("측정 창 중 앱 health 미달")
            time.sleep(min(5, deadline - time.time()))
        self.finish_measurement()
        totals = counts(self.logs())
        self.result["log_counts"] = totals
        self.result["stub_posts"] = self.stub_posts()
        if self.alarm or self.appup:
            self.check_alarm_posts()
        if sum("워커 소비자 시작" in r.get("msg", "") for r in self.logs()) != (3 if self.alarm or self.appup else 2):
            raise BenchError("계획한 앱 교체 외 워커 재시작 감지")
        if totals != {"attempts": 57, "retries": 15, "llm_final_failure": 5, "received": 42, "replies": 37, "notices": 5} or self.stub_posts() != 84:
            raise BenchError("수신·답변 로그와 스텁 건수 대조 불일치")
        self.result["ok"] = bool(self.result["hardware"].get("ok"))

    def stub_ready(self):
        try:
            self.stub_posts()
            return True
        except (OSError, ValueError):
            return False

    def finish_measurement(self):
        self.hw("stop")
        self.started = False
        # stop은 SIGTERM 전달 직후 반환하므로 마지막 표본의 flush를 기다려야 한다.
        pid = next(e["pid"] for e in reversed(read_json(os.path.join(self.sdir, "ledger.json"))["pids"]) if e["name"] == "hw-sampler")
        def stopped():
            p = self.call(["ps", "-p", str(pid), "-o", "stat="], check=False)
            state = p.stdout.strip()
            return p.returncode in (0, 1) and (not state or state.startswith("Z"))
        self.wait(stopped, 30, "표본 수집기 종료 확인 실패")
        report = self.hw("report", "--json", check=False)
        self.result["hardware"] = json.loads(report.stdout)
        self.result["measurement_processes"] = [
            {k: row[k] for k in ("pid", "name", "signal_pid") if k in row}
            for row in read_json(os.path.join(self.sdir, "ledger.json"))["pids"]]
        self.result["measured_until"] = time.time()


def main(argv, mv):
    parser = argparse.ArgumentParser(prog="monitoring-verify bench")
    parser.add_argument("--mode", choices=("host", "app"), required=True)
    parser.add_argument("--row", choices=("echo", "echo-mon"), required=True,
                        help="echo: B1/B2, echo-mon: B3/B4(host verifyBootRun 또는 app, 측정 후 알람 환경 유지)")
    parser.add_argument("--session", required=True)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--out")
    parser.add_argument("--launcher", choices=("bootrun", "jar", "verifyBootRun"), default="bootrun")
    parser.add_argument("--build-first", action="store_true")
    try:
        opts = parser.parse_args(argv)
        if not math.isfinite(opts.minutes) or opts.minutes <= 0 or (opts.mode == "app" and opts.launcher != "bootrun"):
            parser.error("minutes는 양수, launcher는 host 전용이다")
        if opts.row == "echo-mon":
            if opts.mode == "host":
                if opts.build_first or opts.launcher == "jar":
                    parser.error("host echo-mon은 verifyBootRun 전용이며 --build-first를 쓰지 않는다")
                opts.launcher = "verifyBootRun"
    except SystemExit as e:
        return e.code
    bench = Bench(opts, mv)
    try:
        bench.run()
    except BenchError as e:
        bench.result["reason"] = str(e)
    except (Exception, KeyboardInterrupt):
        bench.result["reason"] = "검증 중단: 내부 오류(원문은 비밀 보호를 위해 출력하지 않음)"
    finally:
        if bench.started:
            try:
                bench.finish_measurement()
            except Exception:
                bench.result["measurement_stop_failed"] = True
    if os.path.isdir(bench.sdir) and os.path.commonpath([ROOT, bench.sdir]) != ROOT and os.path.commonpath([bench.sdir, bench.out]) == bench.sdir:
        bench.result["teardown_pending"] = bool(bench.result.get("cold_started"))
        write_json(bench.out, bench.result)
        if bench.result.get("cold_started"):
            round_entry(bench.sdir, {"event": "measurement_finished", "round": bench.result["round"], "finished": time.time(),
                                     "ok": bench.result["ok"], "teardown_pending": True})
    print("bench: " + ("합격" if bench.result["ok"] else bench.result.get("reason", "하드웨어 미달")))
    return 0 if bench.result["ok"] else bench.result.get("exit", 1)
