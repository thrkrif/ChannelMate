"""B3 알람의 유도·해소와 회차별 실측 환경."""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.request

import _mon_model as model
import _mon_run as run
import _mon_spike as ms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TITLES = {"rule1": "복구 필요", "rule2": "LLM 최종 실패", "rule3": "큐 지속 적체", "rule4": "앱 관측 불가"}


def read_env_file(path):
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = (line[7:] if line.startswith("export ") else line).split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def jar_env(sdir, role, project, extra=None, env_path=None):
    """CLI(recovery·indexer) 실행 환경. `.env`를 먼저 읽고 **그 뒤에** 세션 값을 덮어쓴다(셸 export 순서에 기대지 않음)."""
    env = dict(os.environ)
    env.update(read_env_file(env_path or os.path.join(ROOT, ".env")))
    env.update({"APP_ROLE": role, "SLACK_LAB_LOG_DIR": sdir, "POSTGRES_URL": "jdbc:postgresql://localhost:5432/slacklab",
                "RABBITMQ_HOST": "localhost", "SLACK_LAB_COMPOSE_PROJECT": project})
    env.update(extra or {})
    return env


def run_jar_cli(jar, role, args, sdir, project, extra=None, timeout=120):
    """미리 빌드한 jar를 직접 실행한다(Gradle 기동 없음). 반환 (종료 코드, 표준 출력, 시작·완료 시각)."""
    t0 = time.time()
    p = subprocess.run(["java", "-jar", jar, "--spring.main.banner-mode=off", "--logging.level.root=WARN", *args],
                       env=jar_env(sdir, role, project, extra), cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, (t0, time.time())


def db_target_check(project):
    """삭제·재색인 직전 확인: 5432를 게시한 컨테이너가 정확히 1개이고 그 compose 프로젝트 레이블이 세션 프로젝트인가."""
    out = subprocess.run(["docker", "ps", "--filter", "publish=5432", "--format", "{{.ID}} {{.Label \"com.docker.compose.project\"}}"],
                         capture_output=True, text=True).stdout.strip().splitlines()
    return len(out) == 1 and out[0].split()[-1] == project, [ln.split()[-1] for ln in out]


class RealEnv(run.Env):
    """실측 중의 상태 전이를 보존해 짧은 발화도 판정에서 잃지 않게 한다."""

    def __init__(self, sdir, project, grafana, slack_stub, alert_channel, receiver_url, signing_secret, mode,
                 bot_user=None, role_targets=None, session=None):
        self.sdir, self.project, self.g, self.slack, self.channel = sdir, project, grafana, slack_stub, alert_channel
        self.url, self.secret, self.mode = receiver_url.rstrip("/"), signing_secret, mode
        self.flag = os.path.join(sdir, "pause.flag")
        self.session = session
        self.seq = 0
        self.base_reports = {}
        self.active = None
        if session:
            session.tick = self.observe
            session.sleep = self.sleep

    manages_hold = True

    def now(self):
        return time.time()

    def sleep(self, seconds):
        end = self.now() + max(0, seconds)
        while self.now() < end:
            self.observe()
            time.sleep(min(1, max(0, end-self.now())))
        self.observe()

    def rows(self):
        from _mon_alarm import logs, timestamp
        return [r for r in logs(self.sdir) if timestamp(r["ts"]) >= self.started]

    def state(self, rule):
        v = self.g.alert_view(TITLES[rule[:5]])
        if v is None:
            return "error"
        _last, st, health, alerts = v
        if health == "nodata" or any("NoData" in a for a in alerts):
            return "nodata"
        if health == "error" or any("Error" in a for a in alerts):
            return "error"
        return {"firing": "firing", "pending": "pending"}.get(st, "normal")

    def reports(self, rule):
        return self.slack.count("chat.postMessage", self.channel) - self.base_reports.get(rule, 0)

    def observe(self):
        if not self.active:
            return
        from _mon_alarm import timestamp
        now = self.now()
        for key in TITLES:
            state = self.state(key)
            if key == self.active:
                self.history.append((now, state))
            if state == "firing":
                self.fired.setdefault(key, now)
        if self.reports(self.active) > 0 and self.report_at is None:
            self.report_at = now
        for row in self.rows():
            ts = timestamp(row["ts"])
            if row.get("event") not in ("backlog_snapshot", "llm_final_failure") or ts in self.visible:
                continue
            code, body = self.g.query_range("loki", '{app="slack-lab",role="all"} |= "' + row["event"] + '"', ts-.001, ts+.001)
            if code != 200 or not isinstance(body, dict):
                continue
            for result in body.get("data", {}).get("result", []):
                for _, line in result.get("values", []):
                    try:
                        found = json.loads(line)
                        if found.get("ts") == row["ts"] and found.get("event") == row["event"]:
                            self.visible[ts] = self.now()
                    except (ValueError, TypeError):
                        pass

    def wait(self, predicate, seconds, reason):
        end = self.now() + seconds
        while self.now() <= end:
            self.observe()
            result = predicate()
            if result:
                return result
            self.sleep(1)
        raise RuntimeError(reason)

    def snapshots(self):
        return [r for r in self.rows() if r.get("event") == "backlog_snapshot"]

    def condition(self, row, rule):
        f = row.get("fields", {})
        keys = ("dlq", "recovery") if rule == "rule1" else ("queue_ready",)
        if any(k not in f for k in keys):
            raise RuntimeError("적체 스냅샷 필수 필드 누락")
        return max(f[k] for k in keys) >= (1 if rule == "rule1" else 10)

    def _mention(self, question="로그 적체 원인이 뭐야"):
        import uuid
        self.seq += 1
        self.event_id = "EvS6Verify" + uuid.uuid4().hex
        self.mention_ids.add(self.event_id)
        body = json.dumps({"type": "event_callback", "event_id": self.event_id,
                           "authorizations": [{"user_id": "UBOTSTUB"}],
                           "event": {"type": "app_mention", "channel": "CSTUBVERIFY", "user": "U1", "text": "<@UBOTSTUB> " + question,
                                     "ts": f"{int(self.now())}.{self.seq:06d}"}}, ensure_ascii=False).encode()
        stamp = str(int(self.now()))
        sig = "v0=" + hmac.new(self.secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(self.url + "/slack/events", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": sig})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status

    def inject_requests(self, n):
        codes = [self._mention() for _ in range(n)]
        if any(c != 200 for c in codes):
            raise RuntimeError("합성 멘션 수신 실패")
        return codes

    def normal_mentions(self, n):
        for _ in range(n):
            base = self.slack.count("chat.postMessage", "CSTUBVERIFY")
            self.inject_requests(1)
            self.wait(lambda: self.slack.count("chat.postMessage", "CSTUBVERIFY") == base + 1,
                      60, "정상 멘션 답글 누락·중복")

    def failure_counts(self, kind, event_id):
        selected = [r for r in self.rows() if "event_id=" + event_id + " " in r.get("msg", "")]
        metrics = [r for r in selected if r.get("event") == "processing_metric"]
        observed = {"attempts": len(metrics),
                    "retry_scheduled": sum("재시도 예약" in r.get("msg", "") for r in selected),
                    "final_failure": sum(r.get("event") == "llm_final_failure" for r in selected),
                    "notices": sum(r.get("fields", {}).get("kind") == "failure_notice" for r in metrics),
                    "replies": sum(r.get("fields", {}).get("kind") == "answer" for r in metrics)}
        return not model.compare_counts({k: v for k, v in model.expected_failure_counts(kind).items() if k != "generations"}, observed)

    def prepare(self, rule):
        def normal():
            state = self.state(rule)
            if state in ("nodata", "error"):
                raise run.ObservationError("회차 시작 전 관측 오류")
            return state == "normal"
        self.wait(normal, 360, "회차 시작 전 Normal 확인 실패")
        self.sleep(model.RULE_RESET_WAIT_S)

    def cli(self, args):
        ok, _ = db_target_check(self.project)
        self.db_checked = self.now()
        if not ok:
            raise RuntimeError("복구 CLI DB 대상 불일치")
        code, out, times = run_jar_cli(self.session.jar, "recovery", args, self.sdir, self.project)
        if code:
            raise RuntimeError("복구 CLI 실패")
        return out, times

    def induce(self, rule, variant, round_no):
        from _mon_alarm import timestamp
        self.started = self.now()
        self.history, self.fired, self.visible = [], {}, {}
        self.report_at = None
        self.base_reports[rule] = self.slack.count("chat.postMessage", self.channel)
        self.active = rule
        self.variant, self.round_no = variant, round_no
        self.first = self.cleared = None
        self.mention_ids = set()
        self.close_id = None
        self.timeline = {}
        self.session.interruptions = []
        self.slack.mode("CSTUBVERIFY", "ok")
        if rule == "rule1":
            if variant == "quiet" and round_no % 2:
                self.normal_mentions(5)
                return self.now()
            # not_in_channel은 SlackEventHandler에서 영구 발신 실패로 끝나 preserved_input의 dlq에 남는다.
            self.slack.mode("CSTUBVERIFY", "fail")
            self.inject_requests(1)
            failed_id = self.event_id
            row = self.wait(lambda: next((r for r in self.rows() if "발신 실패" in r.get("msg", "")
                                         and failed_id in r.get("msg", "")), None), 60, "DLQ 실패 응답 로그 없음")
            established = timestamp(row["ts"])
            self.first = self.wait(lambda: next((r for r in self.snapshots() if self.condition(r, rule)), None), 30, "DLQ 스냅샷 없음")
            out, _ = self.cli(["list"])
            ids = [ln.split()[-1] for ln in out.splitlines() if ln.split() and ln.split()[-1] == failed_id]
            if len(ids) != 1:
                raise RuntimeError("복구 목록의 유도 건 확인 실패")
            self.close_id = ids[0]
            self.slack.mode("CSTUBVERIFY", "ok")
            if variant == "quiet":
                self.normal_mentions(5)
            return established
        if rule == "rule2":
            mode = "F-echo" if variant == "fire" else ("echo" if round_no % 2 else "F-echo-stub")
            self.session.restart_app(mode)
            if mode == "F-echo-stub":
                from _mon_alarm import StubClient
                StubClient("http://127.0.0.1:18963").request("/__arm", {"fail_next": 1, "status": 503})
            if variant == "quiet":
                self.normal_mentions(1 if mode == "F-echo-stub" else 5)
                if mode == "F-echo-stub":
                    self.wait(lambda: self.failure_counts(mode, self.event_id), 15, "재시도 성공의 기대 사건 수 불일치")
                if any(r.get("event") == "llm_final_failure" for r in self.rows()):
                    raise RuntimeError("비발화 유도에서 LLM 최종 실패 발생")
                return self.now()
            self.inject_requests(1)
            row = self.wait(lambda: next((r for r in self.rows() if r.get("event") == "llm_final_failure"), None), 240,
                            "LLM 최종 실패 로그 없음")
            established = timestamp(row["ts"])
            self.timeline["failure_ts"] = established
            self.wait(lambda: self.failure_counts("F-echo", self.event_id), 15, "LLM 최종 실패의 기대 사건 수 불일치")
            return established
        if rule == "rule3":
            self.wait(lambda: bool(self.snapshots()) and self.snapshots()[-1].get("fields", {}).get("queue_ready") == 0
                      and not any(r.get("event") == "processing_metric" and self.now()-timestamp(r["ts"]) < 5 for r in self.rows()),
                      60, "큐 소진 확인 실패")
            self.timeline["t0"] = self.now()
            paused = sum("검증 소비 제어 상태=PAUSED" in r.get("msg", "") for r in self.rows())
            open(self.flag, "w").close()
            self.wait(lambda: sum("검증 소비 제어 상태=PAUSED" in r.get("msg", "") for r in self.rows()) > paused,
                      20, "PauseControl 정지 확인 실패")
            last = timestamp(self.snapshots()[-1]["ts"])
            self.sleep(max(0, last + 8.5-self.now()))
            self.n = 10 if variant == "fire" or round_no % 2 == 0 else 9
            self.inject_requests(self.n)
            self.queue_ids = set(self.mention_ids)
            established = self.now()
            self.timeline["t1"] = established
            self.first = self.wait(lambda: next((r for r in self.snapshots() if timestamp(r["ts"]) >= established
                                               and r.get("fields", {}).get("queue_ready") == self.n), None), 20, "정지 중 큐 깊이 불일치")
            if timestamp(self.first["ts"])-established > 2:
                raise RuntimeError("10번째 적재가 다음 스냅샷 직전 2초 범위를 벗어남")
            return established
        raise ValueError("B3에서 지원하지 않는 규칙")

    def resolve(self, rule, variant, round_no):
        from _mon_alarm import timestamp
        if rule == "rule1" and self.first:
            hold = 50 if variant == "fire" else 20
            target = timestamp(self.first["ts"]) + hold
            self.sleep(max(0, target - self.session.data.get("jar_delay", 0) - self.now()))
            _, times = self.cli(["close", self.close_id])
            self.close_id = None
            self.timeline.update(db_checked=self.db_checked, close_started=times[0], close_finished=times[1])
            self.cleared = self.wait(lambda: next((r for r in self.snapshots() if timestamp(r["ts"]) > timestamp(self.first["ts"])
                                                 and not self.condition(r, rule)), None), 30, "DLQ 해소 스냅샷 없음")
        elif rule == "rule2":
            self.session.restart_app("echo")
        elif rule == "rule3":
            hold = 140 if variant == "fire" else (130 if round_no % 2 else 110)
            self.sleep(max(0, timestamp(self.first["ts"]) + hold - .5-self.now()))
            os.remove(self.flag)
            self.timeline["t2"] = self.now()
            self.wait(lambda: any("검증 소비 제어 상태=RUNNING" in r.get("msg", "")
                                 and timestamp(r["ts"]) >= self.timeline["t2"] for r in self.rows()), 20, "PauseControl 재개 확인 실패")
            self.cleared = self.wait(lambda: next((r for r in self.snapshots() if timestamp(r["ts"]) > self.timeline["t2"]
                                                 and r.get("fields", {}).get("queue_ready") == 0), None)
                                    if self.queue_processed() else None,
                                    60, "큐 소진·처리 지표 확인 실패")
            self.timeline["t3"] = self.now()
            if variant == "quiet":
                self.normal_mentions(5)
                self.timeline["replies_done"] = self.now()
        self.observe()

    def queue_processed(self):
        metrics = [r for r in self.rows() if r.get("event") == "processing_metric"]
        return len(self.queue_ids) == self.n and all(
            any("event_id=" + event_id + " " in r.get("msg", "") and r.get("fields", {}).get("finalized") is True
                for r in metrics) for event_id in self.queue_ids)

    def measured(self, rule):
        from _mon_alarm import timestamp
        result = {"h": 0, "timeline": self.timeline}
        failure_ts = self.timeline.get("failure_ts")
        if failure_ts in self.visible:
            result["d_s"] = self.visible[failure_ts] - failure_ts
        if self.first and self.cleared:
            start, end = timestamp(self.first["ts"]), timestamp(self.cleared["ts"])
            result["h"] = end-start if self.condition(self.first, rule) else 0
            result["snapshot_start"], result["snapshot_end"] = start, end
            if rule == "rule3" and self.variant == "quiet":
                series = [(timestamp(r["ts"]), r.get("fields", {}).get("queue_ready")) for r in self.snapshots()
                          if start <= timestamp(r["ts"]) <= end]
                if any(v is None for _, v in series):
                    raise RuntimeError("큐 깊이 필드 누락")
                ok, longest = model.rule3_quiet_ok(series, self.n, result["h"])
                depths_ok = all(v == self.n for t, v in series if t < self.timeline["t2"])
                longest = max(longest, result["h"])
                result.update(depth_series=series, longest_ge10=longest,
                              quiet_ok=ok and depths_ok and longest < model.RULES["rule3"]["for"])
            if start in self.visible:
                result["d_s"] = self.visible[start]-start
            if end in self.visible:
                result["d_e"] = self.visible[end]-end
            if "d_s" in result and "d_e" in result:
                result["x"] = model.x_len(result["h"], result["d_s"], result["d_e"])
        return result

    def other_fired(self, rule):
        return [("rule4-host" if key == "rule4" else key,
                 max(self.session.interruptions) if key == "rule4" and self.session.interruptions else None)
                for key in self.fired if key != rule]

    def cleanup(self):
        if getattr(self, "close_id", None):
            self.cli(["close", self.close_id])
            self.close_id = None
        if os.path.exists(self.flag):
            os.remove(self.flag)
        self.slack.mode("CSTUBVERIFY", "ok")
        if self.session.data.get("mode") != "echo":
            self.session.restart_app("echo")
        self.active = None


def ensure_session_dir(sdir):
    if not os.path.isdir(sdir):
        raise RuntimeError("세션 디렉터리가 없다")
    return sdir
