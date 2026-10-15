"""M46 의미 스파이크 SP-1~SP-13 실행기. 설계와 합격 조건의 정본은 `.omc/plans/stage6-plan.md`의 "M46 의미 스파이크" 표다.

원칙: 합성 데이터는 앱이 쓰는 것과 같은 경로(회전 파일 → Alloy → Loki, 가짜 `/actuator/prometheus` → Prometheus)로 넣고, 판정은 프로비저닝된
리터럴 그대로(규칙 파일의 식)를 Grafana 데이터 소스 프록시와 규칙 상태 API로 본다. 시각이 필요한 검사는 줄의 `ts`를 직접 정해 결정적으로
(질의 시각을 바꿔) 보고, 실시간이 필요한 검사(발화 횟수·지연·회복 시간·부재 경계)는 실제로 기다린다.
"""
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time

import _mon_spike as ms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S6 = os.path.join(ROOT, "scripts", "s6-session")
ALERT_DIR = os.path.join(ROOT, "infra", "monitoring", "grafana", "provisioning", "alerting")
STUB_PORTS = {"receiver": 19191, "worker": 19192, "reactor": 19193}
WEBHOOK_PORT = 18181
FILES = ["-f", "compose.yaml", "-f", "compose.monitoring.yaml", "--profile", "monitoring"]
CFILES = FILES
TITLE = {"r1": "복구 필요", "r2": "LLM 최종 실패", "r3": "큐 지속 적체", "r4": "앱 관측 불가"}


def load_rules(name="rules-app.json"):
    with open(os.path.join(ALERT_DIR, name), encoding="utf-8") as f:
        rules = json.load(f)["groups"][0]["rules"]
    return {r["uid"].split("-")[-1]: r for r in rules}


def expr_of(rule, ref):
    return next(d["model"]["expr"] for d in rule["data"] if d["refId"] == ref)


def dashboard_snapshot_expr(key):
    with open(os.path.join(ROOT, "infra", "monitoring", "grafana", "dashboards", "slack-lab.json"), encoding="utf-8") as f:
        panel = json.load(f)["panels"][0]
    return next(t["expr"] for t in panel["targets"] if f'unwrap {key} ' in t["expr"])


def tagged(line, tag):
    d = json.loads(line)
    d["msg"] = f"{d['msg']} sp={tag}"
    return json.dumps(d, ensure_ascii=False)


def classify(code, body):
    """질의 결과를 'value'|'nodata'|'error'와 {role: 값}으로 나눈다."""
    if code != 200 or not isinstance(body, dict) or body.get("status") != "success":
        return "error", {}
    vec = ms.parse_vector(body)
    return ("value" if vec else "nodata"), vec


class Spike:
    def __init__(self, rep, sdir):
        self.rep, self.sdir = rep, sdir
        self.results = {}
        self.det_base = time.time() - 7200  # 결정적 검사용 과거 시각 기준(전부 스택이 생기기 전)
        self.rules = load_rules("rules-app.json")

    # ----- 준비 -----
    def setup(self):
        home_secret = os.path.expanduser("~/.slack-lab-secrets")
        self.secret_dir = os.path.join(home_secret, f"spike-{int(time.time())}")
        os.makedirs(self.secret_dir, mode=0o700, exist_ok=True)
        os.chmod(home_secret, 0o700)
        os.chmod(self.secret_dir, 0o700)
        self.token = secrets.token_hex(32)
        fd = os.open(os.path.join(self.secret_dir, "metrics_token"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.token)
        self.gpw = secrets.token_hex(12)
        self.alert_secret = secrets.token_hex(16)
        self.mon_dir = os.path.join(self.sdir, "mon")
        os.makedirs(os.path.join(self.mon_dir, "targets"), exist_ok=True)
        self.log_dir = os.path.join(self.sdir, "logs")
        self.writer = ms.LogWriter(self.log_dir)
        # SP-9: Alloy가 뜨기 전부터 있던 옛 줄
        self.writer.write("all", ms.log_line(time.time() - 3600, "SP9OLD 기동 전 줄"))
        self.set_targets(list(STUB_PORTS))
        self.stubs = {r: ms.MetricsStub(p, self.token) for r, p in STUB_PORTS.items()}
        for s in self.stubs.values():
            s.start()
        self.hook = ms.WebhookStub(WEBHOOK_PORT)
        self.hook.start()
        self.g = ms.Grafana(self.gpw)
        self.env = {
            "SLACK_LAB_LOG_DIR_HOST": self.log_dir, "SLACK_LAB_SECRET_DIR": self.secret_dir, "SLACK_LAB_MON_DIR": self.mon_dir,
            "SLACK_LAB_RULES_FILE": os.path.join(ALERT_DIR, "rules-app.json"), "GRAFANA_ADMIN_PASSWORD": self.gpw,
            "ALERT_SECRET": self.alert_secret, "POSTGRES_PASSWORD": "spike-unused",
            "GRAFANA_ALERT_WEBHOOK_URL": f"http://host.docker.internal:{WEBHOOK_PORT}/alerts/grafana",
        }
        os.environ.update(self.env)

    def set_targets(self, roles):
        data = [{"targets": [f"host.docker.internal:{STUB_PORTS[r]}"], "labels": {"role": r}} for r in roles]
        path = os.path.join(self.mon_dir, "targets", "targets.json")
        with open(path + ".tmp", "w") as f:
            json.dump(data, f)
        os.replace(path + ".tmp", path)

    def compose(self, *args):
        p = subprocess.run([S6, "compose", "--session", self.sdir, "--", *FILES, *args], cwd=ROOT, capture_output=True, text=True)
        return p.returncode, (p.stdout + p.stderr).strip()[-400:]

    def start_stack(self):
        rc, out = self.compose("up", "-d", "alloy", "loki", "prometheus", "grafana")
        self.rep.add("스택 기동(컨테이너 4개)", rc == 0, out)
        ok = ms.wait_until(self.g.healthy, 120, 2)
        self.rep.add("Grafana 정상(/api/health)", bool(ok))
        if ok:
            self.results["versions"] = {"grafana": self.g.version()}
        return bool(ok)

    def teardown_stubs(self):
        for s in self.stubs.values():
            s.stop()
        self.hook.stop()

    # ----- 보조 -----
    def loki(self, expr, t=None):
        return classify(*self.g.query("loki", expr, t))

    def prom(self, expr):
        return classify(*self.g.query("prom", expr))

    def det(self, tag, specs, wait_expected=None):
        """결정적 검사용 줄을 쓴다. specs=[(offset_s, line)]. role=worker 스트림에 시간순으로 쓰고 수집을 기다린다. 기준 시각 B를 돌려준다."""
        self.det_base += 120
        b = self.det_base
        for off, builder in specs:
            self.writer.write("worker", tagged(builder(b + off), tag))
        want = len(specs)
        ok = ms.wait_until(lambda: self.count_tag(tag, b) >= want, 40, 1)
        if not ok:
            self.rep.add(f"[{tag}] 줄 수집 대기", False, f"기대 {want}줄")
        return b

    def count_tag(self, tag, b):
        code, body = self.g.query("loki", f'sum(count_over_time({{app="slack-lab",role="worker"}} |= "sp={tag}" [3m]))', b + 110)
        try:
            return int(float(body["data"]["result"][0]["value"][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            return 0

    def snap_q(self, key):
        return expr_of(self.rules["r1"], "A") if key == "dlq" else expr_of(self.rules["r3"], "A")

    # ----- SP-10 문법 -----
    def sp10(self):
        queries = {}
        for rid, r in self.rules.items():
            for d in r["data"]:
                if d["datasourceUid"] in ("slack-lab-loki", "slack-lab-prom"):
                    queries[f"{rid}.{d['refId']}"] = (d["datasourceUid"], d["model"]["expr"])
        for k in ("queue_ready", "defer", "dead", "retry", "dlq", "recovery"):
            queries[f"panel1.{k}"] = ("slack-lab-loki", dashboard_snapshot_expr(k))
        bad = []
        for name, (uid, expr) in queries.items():
            code, body = self.g.query("loki" if uid.endswith("loki") else "prom", expr)
            if code != 200:
                bad.append(f"{name}:{code}:{str(body)[:120]}")
        self.rep.add("SP-10 규칙·패널 식 전부 수락(vector(0)·and on()·unless on(role)·범위 집계 안 by·json 중첩 경로)", not bad, "; ".join(bad[:4]))
        code, body = self.g.query("loki", "vector(0)")
        self.rep.add("SP-10 vector(0) 단독", code == 200, str(body)[:100])
        v = {}
        code, body = self.g.request("GET", "/api/datasources/proxy/uid/slack-lab-loki/loki/api/v1/status/buildinfo")
        v["loki"] = body.get("version") if isinstance(body, dict) else str(body)[:60]
        code, body = self.g.request("GET", "/api/datasources/proxy/uid/slack-lab-prom/api/v1/status/buildinfo")
        v["prometheus"] = body.get("data", {}).get("version") if isinstance(body, dict) else str(body)[:60]
        self.results.setdefault("versions", {}).update(v)

    # ----- SP-9 Alloy -----
    def sp9(self):
        now = time.time()
        self.writer.write("all", ms.log_line(now - 600, "SP9TEN 10분 전 ts 줄"))
        self.writer.write("all", ms.log_line(now, "SP9NOW 현재 줄"))
        found = ms.wait_until(lambda: self._entries('|= "SP9TEN"', now - 700, now - 500) == 1, 40, 1)
        self.rep.add("SP-9 10분 전 ts 줄이 원천 ts 시각에 조회됨", bool(found))
        self.rep.add("SP-9 Alloy 시작 전 옛 줄은 읽지 않음", self._entries('|= "SP9OLD"', now - 7200, now + 60) == 0)
        code, body = self.g.request("GET", f"/api/datasources/proxy/uid/slack-lab-loki/loki/api/v1/labels?start={int(now-7200)}&end={int(now+60)}")
        labels = body.get("data", []) if isinstance(body, dict) else []
        self.rep.add("SP-9 레이블 집합 = {app, role, level}", ms.label_set_ok(labels), ",".join(labels))

    def _entries(self, filt, start, end):
        code, body = self.g.query_range("loki", '{app="slack-lab"} ' + filt, start, end)
        try:
            return sum(len(s["values"]) for s in body["data"]["result"])
        except (KeyError, TypeError):
            return -1

    # ----- SP-6 / SP-1 -----
    def sp6_a(self):
        """(가) 새 볼륨에서 스냅샷 줄이 없음 → ②는 NoData."""
        kind, _ = self.loki(expr_of(self.rules["r2"], "A"))
        ok = ms.wait_until(lambda: (self.g.rule_sample(TITLE["r2"]) or ("", "", ""))[2] == "nodata", 60, 2)
        self.rep.add("SP-6 (가) 줄 없음 → 질의 비어 있음·규칙 health=nodata", kind == "nodata" and bool(ok), f"query={kind}")

    def start_emitter(self):
        self.em = ms.Emitter(self.writer)
        self.em.start()

    def sp1_sp6_b(self):
        zero = ms.wait_until(lambda: self.loki(expr_of(self.rules["r2"], "A"))[1] == {"all": 0.0} or
                             self.loki(expr_of(self.rules["r2"], "A"))[1] == {"": 0.0}, 60, 2)
        vecs = {k: self.loki(expr_of(self.rules["r1"], k))[1] for k in ("A", "B")}
        ok_state = ms.wait_until(lambda: (self.g.rule_sample(TITLE["r1"]) or ("", "", ""))[1:] == ("inactive", "ok"), 40, 2)
        r2_ok = ms.wait_until(lambda: (self.g.rule_sample(TITLE["r2"]) or ("", "", ""))[1:] == ("inactive", "ok"), 40, 2)
        self.rep.add("SP-6 (나) 스냅샷만 → 값 0·Normal", bool(zero) and bool(r2_ok), str(self.g.rule_sample(TITLE["r2"])))
        self.rep.add("SP-1 (dlq,recovery)=(0,0) → 두 질의 모두 값 0·규칙 ①이 Normal(health ok)",
                     all(v and all(x == 0 for x in v.values()) for v in vecs.values()) and bool(ok_state), str(vecs))
        code, body = self.g.eval_rule("slack-lab-r1")
        self.results["eval_api_r1"] = {"code": code, "sample": str(body)[:300]}  # 참고 기록(합격 기준 아님): 프록시 질의로 값을 직접 확인한다

    def sp6_c(self):
        t0 = time.time()
        self.writer.write("all", ms.llm_failure_line(t0))
        fired = ms.wait_until(lambda: (self.g.rule_sample(TITLE["r2"]) or ("", "", ""))[1] == "firing", 60, 1)
        kind, vec = self.loki(expr_of(self.rules["r2"], "A"))
        self.rep.add("SP-6 (다) 실패 1줄 → 값 1·Firing(for 0s)", bool(fired) and list(vec.values()) == [1.0], f"{kind} {vec}")
        self.results["sp6c_fire_s"] = round(time.time() - t0, 1)

    # ----- SP-3/4/5/7 결정적 -----
    def sp3(self):
        b = self.det("sp3", [(0, lambda t: ms.snapshot_line(t, "worker", queue_ready=0, defer=0, dead=0, retry=0, dlq=1, recovery=0)),
                             (10, lambda t: ms.snapshot_line(t, "worker", queue_ready=0, defer=0, dead=0, retry=0, dlq=0, recovery=0))])
        _, v1 = self.loki(self.snap_q("dlq"), b + 1)
        _, v2 = self.loki(self.snap_q("dlq"), b + 11)
        self.rep.add("SP-3 dlq 1 → 0: 0 줄 뒤 첫 질의는 0(창 최댓값 1이 아님)", list(v1.values()) == [1.0] and list(v2.values()) == [0.0], f"{v1} {v2}")

    def sp4(self):
        b = self.det("sp4", [(0, lambda t: ms.snapshot_line(t, "worker", queue_ready=1, defer=0, dead=0, retry=0, dlq=0, recovery=0)),
                             (10, lambda t: ms.snapshot_line(t, "worker", queue_ready=2, defer=0, dead=0, retry=1, dlq=0, recovery=0)),
                             (20, lambda t: ms.snapshot_line(t, "worker", queue_ready=3, defer=1, dead=0, retry=1, dlq=0, recovery=0))])
        code, body = self.g.query("loki", self.snap_q("queue_ready"), b + 21)
        n = len(body["data"]["result"]) if code == 200 else -1
        _, vec = classify(code, body)
        self.rep.add("SP-4 30초 안 스냅샷 3줄 → 질의 A 시계열 1개(role=worker, 최신 값)", n == 1 and list(vec.values()) == [3.0], f"n={n} {vec}")

    def sp5(self):
        full = lambda t: ms.snapshot_line(t, "worker", queue_ready=1, defer=0, dead=0, retry=0, dlq=0, recovery=0)  # noqa: E731
        partial = lambda t: ms.snapshot_line(t, "worker", queue_ready=1, defer=0, dead=0, retry=0, recovery=0)  # noqa: E731
        qd = self.snap_q("dlq")
        pd = dashboard_snapshot_expr("dlq")
        # (가) 완전 → 부분
        b = self.det("sp5a", [(0, full), (10, partial)])
        res = {q: [self.loki(e, b + t)[0] for t in (9, 11, 39)] for q, e in (("rule", qd), ("panel", pd))}
        ok = all(r[0] == "value" and r[1] in ("error", "nodata") and r[2] in ("error", "nodata") for r in res.values())
        self.rep.add("SP-5 (가) 완전→부분: 부분 줄 이후 Error/NoData, 직전 값 반환 0 (규칙·패널 동일)", ok, str(res))
        # (나) 완전 → 부분 → 완전
        b = self.det("sp5b", [(0, full), (10, partial), (20, full)])
        res = {q: [self.loki(e, b + t)[0] for t in (11, 29, 39.5, 41)] for q, e in (("rule", qd), ("panel", pd))}
        ok = all(r[0] in ("error", "nodata") and r[1] in ("error", "nodata") and r[2] in ("error", "nodata") and r[3] == "value" for r in res.values())
        self.rep.add("SP-5 (나) 완전→부분→완전: 부분 줄이 창(30s)에 있는 동안 Error/NoData, 창을 벗어난 뒤(+41) 값 복귀 → R 30~31초", ok, str(res))
        for tag, keyless in (("sp5c", True), ("sp5d", False)):
            b = self.det(tag, [(0, full), (10, lambda t, k=keyless: ms.unobserved_line(t, "worker", k)), (20, full)])
            res = {q: [self.loki(e, b + t)[0] for t in (11, 25, 39.5, 41)] for q, e in (("rule", qd), ("panel", pd))}
            ok = all(r[0] == "nodata" and r[1] == "nodata" and r[2] == "nodata" and r[3] == "value" for r in res.values())
            label = "(다) 키 없는 `적체 스냅샷`" if keyless else "(라) `적체 스냅샷 실패`"
            self.rep.add(f"SP-5 {label}: 관측 실패 줄 이후 NoData(직전 값 0회), 창 이후 값 복귀 → R 30~31초", ok, str(res))

    def sp7(self):
        specs = [(i * 10, (lambda v: lambda t: ms.snapshot_line(t, "worker", queue_ready=v, defer=0, dead=0, retry=0, dlq=0, recovery=0))(v))
                 for i, v in enumerate((10, 9, 10, 0))]
        b = self.det("sp7", specs)
        got = [list(self.loki(self.snap_q("queue_ready"), b + i * 10 + 1)[1].values()) for i in range(4)]
        self.rep.add("SP-7 queue_ready 10→9→10→0: 최신 값이 즉시 반영, 조건(>9)은 10에서만", got == [[10.0], [9.0], [10.0], [0.0]], str(got))

    # ----- SP-2 / SP-13 발화 사이클 -----
    def fire_cycle(self, key, hold=50):
        title = TITLE["r1"]
        before = len(self.hook.snapshot())
        t_c = time.time()
        self.em.set(**{key: 1})
        samples = []
        fired_at = None
        end = t_c + 150
        while time.time() < end:
            s = self.g.rule_sample(title)
            if s:
                samples.append(s)
                if s[1] == "firing" and fired_at is None:
                    fired_at = time.time()
            if fired_at and time.time() >= t_c + hold:
                break
            if not fired_at and time.time() >= t_c + hold + 60:
                break
            time.sleep(0.5)
        self.em.set(**{key: 0})
        n_eval = ms.count_evaluations(samples)
        hook_firing = lambda: next((h for h in self.hook.snapshot()[before:]  # noqa: E731
                                    if self.hook.rule_of(h) == "recovery_needed" and self.hook.status_of(h) == "firing"), None)
        got = ms.wait_until(hook_firing, 60, 1)
        fire_eval = next((s[0] for s in samples if s[1] == "firing"), None)
        delay = None
        if got and fire_eval:
            delay = got["t"] - ms.parse_rfc3339(fire_eval)  # Firing이 된 평가 시각 → 웹훅 수신
        normal = ms.wait_until(lambda: (self.g.rule_sample(title) or ("", "", ""))[1] == "inactive", 150, 2)
        t_normal = time.time()
        resolved = ms.wait_until(lambda: next((h for h in self.hook.snapshot()[before:]
                                               if self.hook.rule_of(h) == "recovery_needed" and self.hook.status_of(h) == "resolved"), None), 90, 2)
        self.results.setdefault("webhooks", []).extend(
            {"cycle": key, "t": round(h["t"], 1), "status": self.hook.status_of(h), "rule": self.hook.rule_of(h),
             "path": h["path"], "has_secret_header": any(k.lower() == "x-alert-secret" for k in h["headers"])}
            for h in self.hook.snapshot()[before:])
        if not got:
            self.results.setdefault("notifier_log", []).append(self.docker_log_grep("grafana", r"ngalert\.notifier|dispatch|webhook|Notify|flush"))
        return {"key": key, "evals_pending_to_firing": n_eval, "fired_s": (fired_at - t_c) if fired_at else None,
                "webhook_after_fire_poll_s": delay,
                "webhook": bool(got), "headers": (got or {}).get("headers"), "path": (got or {}).get("path"),
                "normal": bool(normal), "normal_t": t_normal, "resolved_webhook": bool(resolved)}

    def docker_log_grep(self, service, pattern):
        p = subprocess.run([S6, "compose", "--session", self.sdir, "--", *FILES, "logs", "--no-color", "--tail", "400", service],
                           cwd=ROOT, capture_output=True, text=True)
        rows = [ln for ln in p.stdout.splitlines() if re.search(pattern, ln) and "template definitions" not in ln]
        return rows[-12:]

    def sp2_sp13(self):
        r_recovery = self.fire_cycle("recovery")
        self.results["cycle_recovery"] = {k: v for k, v in r_recovery.items() if k != "headers"}
        r_dlq_wait_start = time.time()
        # Normal 뒤 70초 대기 후 재발화(SP-13)
        time.sleep(max(0, 70 - (time.time() - r_recovery["normal_t"])))
        r_dlq = self.fire_cycle("dlq")
        self.results["cycle_dlq"] = {k: v for k, v in r_dlq.items() if k != "headers"}
        for r, which in ((r_dlq, "dlq=1"), (r_recovery, "recovery=1")):
            self.rep.add(f"SP-2 {which}: E≠0 → Pending→Firing 조건 참 평가 수 = for/10+1 = 4", r["evals_pending_to_firing"] == 4,
                         f"평가 수={r['evals_pending_to_firing']}")
        d1, d2 = r_recovery["webhook_after_fire_poll_s"], r_dlq["webhook_after_fire_poll_s"]
        self.rep.add("SP-13 재발화(Normal+70초 뒤): 웹훅 수신, Firing 관측→웹훅 지연 ≤15초(새 그룹 group_wait 경로)",
                     r_dlq["webhook"] and d2 is not None and d2 <= 15, f"1차={d1} 2차={d2}")
        self.rep.add("SP-13 해소 알림도 웹훅에 도착(M47 정규화기는 200으로 무시해야 함)", r_dlq["resolved_webhook"] and r_recovery["resolved_webhook"])
        hit = r_dlq if r_dlq.get("headers") else r_recovery
        hdrs = {k.lower(): v for k, v in (hit.get("headers") or {}).items()}
        self.rep.add("SP-11 사용자 헤더 X-Alert-Secret이 전송됨(값 일치)", hdrs.get("x-alert-secret") == self.alert_secret,
                     "헤더 있음" if "x-alert-secret" in hdrs else "헤더 없음 → D-4 필요")
        self.rep.add("SP-11 웹훅 경로 = /alerts/grafana", hit.get("path") == "/alerts/grafana", str(hit.get("path")))

    # ----- SP-5 실시간 회복 시간 -----
    def sp5_live(self):
        t0, disturbed, back, log = ms.live_recovery(self.g, TITLE["r3"], lambda t: self.writer.write("all", ms.unobserved_line(t, "all", True)))
        r = (ms.parse_rfc3339(back) - t0) if back else None
        self.results["sp5_live_R_s"] = r
        self.results["sp5_live_log"] = log[-10:]
        self.rep.add("SP-5 실시간 회복 시간 R(관측 실패 줄 → 규칙이 평상으로 돌아온 첫 평가) 30~45초", disturbed and r is not None and 28 <= r <= 47,
                     f"disturbed={disturbed} R={r} log={log[-6:]}")

    # ----- SP-12 -----
    def sp12(self):
        ok = ms.wait_until(lambda: all(self.prom(f'up{{job="slack-lab",role="{r}"}}')[1] == {r: 1.0} for r in STUB_PORTS), 60, 2)
        self.rep.add("SP-12 Prometheus가 600 credentials_file로 인증해 대상 3개 up=1 (호스트 localhost 스텁에 host.docker.internal 도달 포함)", bool(ok))
        mode = stat.S_IMODE(os.stat(os.path.join(self.secret_dir, "metrics_token")).st_mode)
        p = subprocess.run([S6, "compose", "--session", self.sdir, "--", *FILES, "exec", "-T", "prometheus", "sh", "-c",
                            "ls -ln /run/secrets/metrics_token; id"], cwd=ROOT, capture_output=True, text=True)
        self.results["sp12"] = {"host_mode": oct(mode), "container": (p.stdout + p.stderr).strip()[:300]}
        self.rep.add("SP-12 토큰 파일 권한 600(저장소 밖)", mode == 0o600)

    # ----- SP-8 ④ -----
    def role_ts(self, role, op):
        """up{role}==0 / ==1 인 가장 최근 표본의 시각(초). 없으면 None."""
        code, body = self.g.query("prom", f'timestamp(up{{job="slack-lab",role="{role}"}} {op})')
        try:
            return float(body["data"]["result"][0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def sp8(self):
        roles = list(STUB_PORTS)
        r4 = expr_of(self.rules["r4"], "A")
        ok = ms.wait_until(lambda: self.prom(r4)[1] == {r: 0.0 for r in roles}, 60, 2)
        self.rep.add("SP-8 (가) 정상: 세 역할 모두 값 0", bool(ok), str(self.prom(r4)[1]))
        s = self.g.rule_sample(TITLE["r4"])
        self.rep.add("SP-8 (가) 규칙 ④ Normal(health ok)", bool(s) and s[1] == "inactive" and s[2] == "ok", str(s))
        # (나) 즉시 거부, (나') 연결 받고 무응답 — 역할을 돌려 쓴다
        d_s_all, d_e_all = [], []
        refused = []
        for i, role in enumerate(roles):
            hang = False
            t_c = time.time()
            self.stubs[role].stop()
            ts = ms.wait_until(lambda: (lambda x: x if x and x >= t_c else None)(self.role_ts(role, "== 0")), 40, 0.5)
            d_s = (ts - t_c) if ts else None
            t_r = time.time()
            self.stubs[role].start("ok")
            te = ms.wait_until(lambda: (lambda x: x if x and x >= t_r else None)(self.role_ts(role, "== 1")), 40, 0.5)
            d_e = (te - t_r) if te else None
            refused.append((role, d_s, d_e))
            if d_s is not None:
                d_s_all.append(d_s)
            if d_e is not None:
                d_e_all.append(d_e)
        self.results["sp8_refused"] = [(r, None if a is None else round(a, 1), None if b is None else round(b, 1)) for r, a, b in refused]
        self.rep.add("SP-8 (나) 정지(연결 즉시 거부) 3회: D_s ≤ 15초, 재시작 D_e ≤ 15초",
                     len(refused) == 3 and all(a is not None and a <= 15 and b is not None and b <= 15 for _r, a, b in refused),
                     str(self.results["sp8_refused"]))
        hung = []
        for role in roles:
            t_c = time.time()
            self.stubs[role].stop()
            self.stubs[role].start("hang")
            ts = ms.wait_until(lambda: (lambda x: x if x and x >= t_c else None)(self.role_ts(role, "== 0")), 40, 0.5)
            d_s = (ts - t_c) if ts else None
            code, body = self.g.query("prom", f'scrape_duration_seconds{{job="slack-lab",role="{role}"}}')
            try:
                dur = float(body["data"]["result"][0]["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                dur = None
            t_r = time.time()
            self.stubs[role].stop()
            self.stubs[role].start("ok")
            te = ms.wait_until(lambda: (lambda x: x if x and x >= t_r else None)(self.role_ts(role, "== 1")), 40, 0.5)
            d_e = (te - t_r) if te else None
            hung.append((role, d_s, dur, d_e))
        self.results["sp8_hang"] = [(r, None if a is None else round(a, 1), b, None if c is None else round(c, 1)) for r, a, b, c in hung]
        self.rep.add("SP-8 (나') 연결 받고 무응답 3회: scrape_timeout(5초)로 실패 확정, D_s ≤ 15초, D_e ≤ 15초",
                     all(a is not None and a <= 15 and c is not None and c <= 15 and (b is None or b >= 4.5) for _r, a, b, c in hung),
                     str(self.results["sp8_hang"]))
        # (다)(라) file_sd 제거 → H 유지 → 복원 (역할 하나씩)
        absent = []
        before = len(self.hook.snapshot())
        all_evals = []
        H = 120
        for role in roles:
            others = [r for r in roles if r != role]
            t_c = time.time()
            self.set_targets(others)
            gone = ms.wait_until(lambda: self.prom(f'up{{job="slack-lab",role="{role}"}}')[0] == "nodata", 60, 0.5)
            d_s = (time.time() - t_c) if gone else None
            kind, vec = self.prom(r4)
            isolated = vec.get(role) == 1.0 and all(vec.get(o) == 0.0 for o in others)
            s_hold = ms.poll_rule(self.g, TITLE["r4"], max(0, H - (time.time() - t_c)), 0.5)
            all_evals += [x[0] for x in s_hold]
            fired = any(x[1] == "firing" for x in s_hold)
            t_r = time.time()
            self.set_targets(roles)
            te = ms.wait_until(lambda: (lambda x: x if x and x >= t_r else None)(self.role_ts(role, "== 1")), 60, 0.5)
            d_e = (te - t_r) if te else None
            back = self.prom(r4)[1].get(role)
            normal = ms.wait_until(lambda: (self.g.rule_sample(TITLE["r4"]) or ("", "", ""))[1] == "inactive", 120, 2)
            absent.append({"role": role, "D_s": d_s, "D_e": d_e, "isolated": isolated, "fired": fired, "back0": back == 0.0 or back is None, "normal": bool(normal)})
        self.results["sp8_absent"] = [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in a.items()} for a in absent]
        self.rep.add("SP-8 (다) file_sd 제거 3회: D_s ≤ 35초(모형 경계), 복원 뒤 up=1·Normal",
                     all(a["D_s"] is not None and a["D_s"] <= 35 and a["D_e"] is not None and a["normal"] for a in absent), str(self.results["sp8_absent"]))
        self.rep.add("SP-8 (라) 다차원 부재: 제거한 역할의 시계열만 값 1·role 레이블 정확, 나머지 역할 0 유지", all(a["isolated"] for a in absent))
        self.rep.add(f"SP-8 (다) H={H}초 유지에서 ④가 Firing", all(a["fired"] for a in absent), str([a["fired"] for a in absent]))
        ds = [a["D_s"] for a in absent if a["D_s"] is not None]
        de = [a["D_e"] for a in absent if a["D_e"] is not None]
        if ds and de:
            hs = ms.h_sample(60, max(ds), min(de), 15)
            hf = ms.h_final(120, hs)
            self.results["sp8_H_absent"] = {"H_model": 120, "H_sample": hs, "H_final": hf}
            self.rep.add("SP-8 H_부재 = max(120, 표본) ≤ 150(상한)", hf is not None, f"표본={hs} 최종={hf}")
        hooks = [h for h in self.hook.snapshot()[before:] if self.hook.rule_of(h) == "app_unobservable" and self.hook.status_of(h) == "firing"]
        roles_hooked = sorted({a.get("labels", {}).get("role") for h in hooks for a in h["body"].get("alerts", [])})
        self.rep.add("SP-8 ④ 웹훅이 역할 레이블과 함께 도착(역할별 그룹)", set(roles) <= set(roles_hooked), str(roles_hooked))
        # (마) 격자: 규칙 평가 시각(14회), Prometheus 원시 표본
        evals = []
        for e in all_evals:
            if not evals or evals[-1] != e:
                evals.append(e)
        drift_eval = 0.0  # 평가 격자는 sp8grid가 연속 구간에서 잰다(여기서는 구간이 끊겨 있어 쓰지 않는다)
        code, body = self.g.query("prom", f'up{{job="slack-lab",role="receiver"}}[3m]')
        try:
            samples = [float(v[0]) for v in body["data"]["result"][0]["values"]][-14:]
            drift_scrape = ms.drift_from_grid(samples, 10.0)
        except (KeyError, IndexError, TypeError, ValueError):
            drift_scrape = None
        self.results["sp8_grid"] = {"eval_drift_s": drift_eval, "scrape_drift_s": drift_scrape, "evals": len(evals)}
        self.rep.add("SP-8 (마) Prometheus 원시 표본 격자: 14표본 누적 이탈 ≤ 5초",
                     drift_scrape is not None and drift_scrape <= 5, f"scrape={drift_scrape}")

    def sp8grid(self):
        """(마) 규칙 평가 시각의 격자: 연속한 15개 평가(=14간격)에서 첫 시각 기준 누적 이탈."""
        samples = ms.poll_rule(self.g, TITLE["r4"], 165, 0.5)
        evals = []
        for e, _s, _h, _t in samples:
            if not evals or evals[-1] != e:
                evals.append(e)
        times = [ms.parse_rfc3339(e) for e in evals[:15]]
        gaps = [round(b - a, 2) for a, b in zip(times, times[1:])]
        drift = ms.drift_from_grid(times, 10.0) if len(times) >= 15 else None
        self.results["sp8_eval_grid"] = {"n": len(times), "drift_s": drift, "gaps": gaps}
        self.rep.add("SP-8 (마) 규칙 평가 격자: 연속 15개 평가의 누적 이탈 ≤ 5초", drift is not None and drift <= 5, f"n={len(times)} drift={drift}")

    # ----- SP-11 전환 -----
    def sp11_switch(self):
        want = sorted(f"slack-lab-r{i}" for i in range(1, 5))
        app = self.g.provisioned_rule_uids()
        self.env["SLACK_LAB_RULES_FILE"] = os.path.join(ALERT_DIR, "rules-host.json")
        os.environ.update(self.env)
        self.compose("rm", "-s", "-f", "grafana")
        rc, out = self.compose("up", "-d", "grafana")
        ok = ms.wait_until(self.g.healthy, 90, 2)
        host = ms.wait_until(lambda: self.g.provisioned_rule_uids() == want, 60, 2)
        code, rule = self.g.request("GET", "/api/v1/provisioning/alert-rules/slack-lab-r4")
        expr = next((d["model"]["expr"] for d in rule["data"] if d["refId"] == "A"), "") if code == 200 else ""
        has_all = 'role="all"' in expr
        self.rep.add("SP-11 app↔host 규칙 파일 전환: 규칙 정확히 4개·UID 동일, ④ 식이 role=all 하나",
                     app == want and bool(ok) and bool(host) and has_all and 'role="receiver"' not in expr,
                     f"app={app} host={host} expr_has_all={has_all}")


def run_spike(rep, sdir, only=None):
    sp = Spike(rep, sdir)
    try:
        sp.setup()
        if not sp.start_stack():
            return sp
        steps = [("sp10", sp.sp10), ("sp9", sp.sp9), ("sp6a", sp.sp6_a), ("emit", sp.start_emitter), ("sp1_6b", sp.sp1_sp6_b),
                 ("sp6c", sp.sp6_c), ("sp12", sp.sp12), ("sp3", sp.sp3), ("sp4", sp.sp4), ("sp5", sp.sp5), ("sp7", sp.sp7),
                 ("sp2_13", sp.sp2_sp13), ("sp5live", sp.sp5_live), ("sp8", sp.sp8), ("sp8grid", sp.sp8grid), ("sp11", sp.sp11_switch)]
        for name, fn in steps:
            if only and name not in only and name != "emit":
                continue
            t = time.time()
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — 한 검사의 실패가 다른 검사를 막지 않는다
                rep.add(f"{name} 실행 중 예외", False, f"{type(e).__name__}: {str(e)[:200]}")
            print(f"  ({name} {time.time() - t:.0f}s)")
            sys.stdout.flush()
    finally:
        try:
            if getattr(sp, "em", None):
                sp.em.stopped = True
            sp.teardown_stubs()
        except Exception:  # noqa: BLE001
            pass
    return sp


# ---------------------------------------------------------------------------------------------------------
# 프로비저닝 검증(M46 중간 기동 2): 실제 앱(echo) + `scripts/monitoring start --host`
# ---------------------------------------------------------------------------------------------------------
def run_provision(rep, s6fn, mv):
    env_path = os.path.join(ROOT, ".env")
    token = mv.read_token(env_path)
    gpw = mv._env_file_value("GRAFANA_ADMIN_PASSWORD")
    if not token or not gpw:
        rep.add("사전 조건: .env에 MANAGEMENT_METRICS_TOKEN·GRAFANA_ADMIN_PASSWORD", False, "H2a 값이 필요하다")
        return
    sdir = s6fn("start", "--id", "m46-provision").stdout.strip()
    project = json.load(open(os.path.join(sdir, "ledger.json")))["project"]
    try:
        build = subprocess.run([os.path.join(ROOT, "gradlew"), "bootJar", "-x", "test", "-q"], cwd=ROOT, capture_output=True, text=True)
        jar = mv.pick_jar(os.path.join(ROOT, "build", "libs"))
        rep.add("bootJar 빌드", build.returncode == 0 and bool(jar), build.stderr.strip()[:200])
        if not jar:
            return
        s6fn("compose", "--session", sdir, "--", "up", "-d", "--wait", "rabbitmq", "postgres")
        mv.spawn_app(sdir, jar, [env_path], {"APP_ROLE": "all", "LLM_CLIENT": "echo", "RAG_ENABLED": "false",
                                             "CLASSIFICATION_ENABLED": "false"}, "app")
        rep.add("앱(echo, 역할 all) /health 200", mv.host_health())
        env = dict(os.environ, S6_SESSION=sdir, SLACK_LAB_LOG_DIR_HOST=sdir)
        p = subprocess.run([os.path.join(ROOT, "scripts", "monitoring"), "start", "--host"], cwd=ROOT, env=env, capture_output=True, text=True)
        rep.add("scripts/monitoring start --host", p.returncode == 0, (p.stdout + p.stderr).strip()[-300:])
        if p.returncode != 0:
            return
        g = ms.Grafana(gpw)
        rep.add("Grafana 정상", bool(ms.wait_until(g.healthy, 120, 2)))
        code, found = g.request("GET", "/api/search?type=dash-db")
        uids = [x.get("uid") for x in found] if code == 200 and isinstance(found, list) else []
        code, dash = g.request("GET", "/api/dashboards/uid/slack-lab-overview")
        panels = len(dash.get("dashboard", {}).get("panels", [])) if code == 200 and isinstance(dash, dict) else -1
        rep.add("대시보드 1개·패널 8개", uids == ["slack-lab-overview"] and panels == 8, f"{uids} panels={panels}")
        want = {"slack-lab-r1": ("30s", "복구 필요"), "slack-lab-r2": ("0s", "LLM 최종 실패"),
                "slack-lab-r3": ("2m0s", "큐 지속 적체"), "slack-lab-r4": ("1m0s", "앱 관측 불가")}
        code, rules = g.request("GET", "/api/v1/provisioning/alert-rules")
        got = {r["uid"]: (r.get("for"), r.get("title"), r.get("noDataState"), r.get("execErrState")) for r in rules} if code == 200 and isinstance(rules, list) else {}
        def dur_eq(a, b):
            return a == b or {"2m": "2m0s", "1m": "1m0s", "2m0s": "2m", "1m0s": "1m"}.get(a) == b
        ok = len(got) == 4 and all(u in got and dur_eq(got[u][0], f) and got[u][1] == t and got[u][2] == "NoData" and got[u][3] == "Error"
                                   for u, (f, t) in want.items())
        rep.add("알람 규칙 4개(제목·지속·NoData/Error 상태)", ok, str(got)[:300])
        code, groups = g.request("GET", "/api/ruler/grafana/api/v1/rules")
        intervals = sorted({gr.get("interval") for fl in (groups.values() if isinstance(groups, dict) else []) for gr in fl})
        rep.add("평가 간격 10초", intervals in (["10s"],), str(intervals))
        code, pol = g.request("GET", "/api/v1/provisioning/policies")
        routes = pol.get("routes") or [] if isinstance(pol, dict) else []
        pol_ok = isinstance(pol, dict) and pol.get("group_wait") == "5s" and pol.get("group_interval") == "1m" and pol.get("repeat_interval") == "1h" \
            and sorted(pol.get("group_by") or []) == ["role", "slack_lab_rule"] and len(routes) == 1 \
            and routes[0].get("receiver") == "slack-lab-webhook"
        rep.add("알림 정책(group_by·group_wait 5s·group_interval 1m·repeat_interval 1h, 웹훅 경로 1개)", pol_ok, str(pol)[:300])
        code, cps = g.request("GET", "/api/v1/provisioning/contact-points")
        names = sorted((c.get("name"), c.get("type")) for c in cps) if code == 200 and isinstance(cps, list) else []
        rep.add("연락 지점 slack-lab-webhook(webhook)", ("slack-lab-webhook", "webhook") in names, str(names)[:200])
        for uid in ("slack-lab-loki", "slack-lab-prom"):
            code, body = g.request("GET", f"/api/datasources/uid/{uid}/health")
            rep.add(f"데이터 소스 {uid} 상태", code == 200 and isinstance(body, dict) and body.get("status") == "OK", str(body)[:120])
        up = ms.wait_until(lambda: classify(*g.query("prom", 'up{job="slack-lab",role="all"}'))[1] == {"all": 1.0}, 90, 3)
        rep.add("Prometheus 대상 role=all up=1 (host.docker.internal:9091 도달·Bearer 인증)", bool(up))
        rules_app = load_rules("rules-host.json")
        def has_values():
            return classify(*g.query("loki", dashboard_snapshot_expr("queue_ready")))[1] != {}
        got_values = bool(ms.wait_until(has_values, 90, 3))
        rep.add("실제 앱 스냅샷이 대시보드 패널 1 질의로 조회됨(앱 → 회전 파일 → Alloy → Loki)", got_values)
        if not got_values:  # 원인 진단: 수집기·저장소 로그의 오류성 줄과 컨테이너 안에서 보이는 파일
            def tail(service):
                q = subprocess.run([S6, "compose", "--session", sdir, "--", *CFILES, "logs", "--no-color", "--tail", "300", service],
                                   cwd=ROOT, capture_output=True, text=True)
                return [ln[-240:] for ln in q.stdout.splitlines() if re.search(r"error|warn|fail|reject|too|denied|permission|tail|file", ln, re.I)][-12:]
            ls = subprocess.run([S6, "compose", "--session", sdir, "--", *CFILES, "exec", "-T", "alloy", "ls", "-la", "/var/log/slack-lab/all"],
                                cwd=ROOT, capture_output=True, text=True)
            rep.add("진단: Alloy 로그", False, " || ".join(tail("alloy")))
            rep.add("진단: Loki 로그", False, " || ".join(tail("loki")))
            rep.add("진단: Alloy 컨테이너에서 본 로그 디렉터리", False, (ls.stdout + ls.stderr)[-400:])
        code, body = g.request("GET", "/api/datasources/proxy/uid/slack-lab-loki/loki/api/v1/labels")
        labels = body.get("data", []) if isinstance(body, dict) else []
        rep.add("Loki 레이블 = {app, role, level}", ms.label_set_ok(labels), ",".join(labels))
        rep.add("규칙 ④(host) 식 질의 결과 {all: 0}", classify(*g.query("prom", expr_of(rules_app["r4"], "A")))[1] == {"all": 0.0})
        bad = ms.wait_until(lambda: not any(r.get("health") == "error" for r in g.rules()) and len(g.rules()) == 4, 60, 3)
        calm = ms.wait_until(lambda: (g.alert_view(TITLE["r3"]) or ("", "", "", []))[1:3] == ("inactive", "ok"), 60, 2)
        app_log = os.path.join(sdir, "all", "slack-lab.log")

        def inject(t):  # 앱이 쓰는 같은 파일에 관측 실패 줄 한 줄을 덧붙인다(Alloy → Loki 경로)
            with open(app_log, "a", encoding="utf-8") as f:
                f.write(ms.unobserved_line(t, "all", True) + "\n")
        t0, disturbed, back, log = ms.live_recovery(g, TITLE["r3"], inject)
        r_live = (ms.parse_rfc3339(back) - t0) if back else None
        rep.add("실제 앱·host 규칙: 관측 실패 줄 → ③이 NoData(Pending)로 교란됐다가 R 30~45초 안에 복귀", bool(calm) and disturbed and r_live is not None and 28 <= r_live <= 47,
                f"R={r_live} log={log[-6:]}")
        rep.add("규칙 4개 모두 평가 오류 없음(health≠error)", bool(bad), str([(r.get("name"), r.get("health")) for r in g.rules()]))
        ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
                             capture_output=True, text=True).stdout.split()
        mounts = subprocess.run(["docker", "inspect", *ids, "--format", "{{.Name}} {{json .Mounts}}"], capture_output=True, text=True).stdout
        rep.add("docker.sock 마운트 0", "docker.sock" not in mounts)
        alloy_ro = [ln for ln in mounts.splitlines() if "alloy" in ln.split()[0]]
        rep.add("Alloy 로그 마운트는 읽기 전용", bool(alloy_ro) and '"Destination":"/var/log/slack-lab"' in alloy_ro[0] and
                re.search(r'"Destination":"/var/log/slack-lab","Mode":"[^"]*","RW":false', alloy_ro[0]) is not None, "")
        pub = subprocess.run(["docker", "ps", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Names}} {{.Ports}}"],
                             capture_output=True, text=True).stdout
        grafana_pub = [ln for ln in pub.splitlines() if "grafana" in ln]
        others_pub = [ln for ln in pub.splitlines() if any(x in ln for x in ("loki", "prometheus", "alloy")) and "->" in ln]
        rep.add("Grafana만 127.0.0.1:3000 게시, Loki·Prometheus·Alloy 게시 0", bool(grafana_pub) and "127.0.0.1:3000->3000" in grafana_pub[0] and not others_pub, pub.strip()[:200])
        # 중지·초기화 계약
        subprocess.run([os.path.join(ROOT, "scripts", "monitoring"), "stop"], cwd=ROOT, env=env, capture_output=True, text=True)
        left = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.Names}}"],
                              capture_output=True, text=True).stdout
        vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={project}_mon-"], capture_output=True, text=True).stdout.split()
        rep.add("monitoring stop: 스택 컨테이너 4개 제거·볼륨 4개 유지·기본 스택 컨테이너 무영향",
                not any(x in left for x in ("grafana", "loki", "prometheus", "alloy")) and len(vols) == 4 and "postgres" in left, f"vols={len(vols)}")
        subprocess.run([os.path.join(ROOT, "scripts", "monitoring"), "reset", "--confirm"], cwd=ROOT, env=env, capture_output=True, text=True)
        vols2 = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={project}_mon-"], capture_output=True, text=True).stdout.split()
        pg = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={project}_postgres-data"], capture_output=True, text=True).stdout.split()
        rep.add("monitoring reset --confirm: 모니터링 볼륨 4개만 삭제(postgres 볼륨 유지)", not vols2 and len(pg) == 1, f"mon={len(vols2)} pg={len(pg)}")
    finally:
        p = s6fn("teardown", "--session", sdir, check=False)
        rep.add("teardown 최종 확인", p.returncode == 0, (p.stdout + p.stderr).strip()[:300])
