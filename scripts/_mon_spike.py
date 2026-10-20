"""6단계 M46 의미 스파이크(SP-1~SP-13)와 프로비저닝 검증의 구현. `scripts/monitoring-verify spike|provision`이 부른다.

이 모듈은 비밀을 다루지 않는다. 스파이크용 관리 토큰·Grafana 비밀번호·알람 시크릿은 모두 실행 때마다 새로 만든 일회용 값이고, 저장소 밖
600 파일(토큰)이나 프로세스 환경으로만 넘어가며 출력하지 않는다. 사용자의 `.env` 값은 스파이크가 읽지 않는다(프로비저닝 검증만 읽는다).

순수 함수(로그 줄 생성·평가 횟수 계산·D/L 계산·레이블 검사)는 `test_monitoring_spike.py`가 단위로 시험한다. 나머지는 실제 스택이 필요하다.
"""
import base64
import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SELECTOR_ROLES = "worker|all"


# ---------------------------------------------------------------------------------------------------------
# 순수 함수
# ---------------------------------------------------------------------------------------------------------
def iso(ts):
    """M44 포매터와 같은 모양: UTC 밀리초 `Z`."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log_line(ts, msg, event=None, fields=None, level="INFO", role="all"):
    d = {"v": 1, "ts": iso(ts), "level": level, "role": role, "logger": "spike", "msg": msg}
    if event:
        d["event"] = event
    if fields:
        d["fields"] = fields
    return json.dumps(d, ensure_ascii=False)


def snapshot_line(ts, role="all", **kv):
    """부분 스냅샷을 만들려면 키를 빼고 넘긴다(빠진 키는 fields에 넣지 않는다 — 0 치환 금지)."""
    msg = "적체 스냅샷 " + " ".join(f"{k}={v}" for k, v in kv.items())
    return log_line(ts, msg, "backlog_snapshot", dict(kv), role=role)


def unobserved_line(ts, role="all", keyless=True):
    """관측 실패 줄: 키 없는 `적체 스냅샷`(keyless) 또는 `적체 스냅샷 실패 reason=…`."""
    if keyless:
        return log_line(ts, "적체 스냅샷", "backlog_unobserved", None, "WARN", role)
    return log_line(ts, "적체 스냅샷 실패 reason=ConnectException", "backlog_unobserved", {"reason": "ConnectException"}, "WARN", role)


def llm_failure_line(ts, role="all"):
    return log_line(ts, "LLM 실패 → 실패 안내로 전환", "llm_final_failure", None, "WARN", role)


def count_evaluations(samples, from_state="pending", to_state="firing"):
    """samples: [(lastEvaluation 문자열, state, health)] 시간순. from_state를 처음 보인 평가부터 to_state를 처음 보인 평가까지
    **서로 다른 평가** 수(양끝 포함)를 센다. 못 찾으면 None. 같은 평가를 폴링이 여러 번 본 것은 한 번으로 센다."""
    evals = []
    for last, state, _health in samples:
        if not evals or evals[-1][0] != last:
            evals.append((last, state))
        else:
            evals[-1] = (last, state)
    start = next((i for i, (_l, s) in enumerate(evals) if s == from_state), None)
    if start is None:
        return None
    end = next((i for i in range(start, len(evals)) if evals[i][1] == to_state), None)
    if end is None:
        return None
    return end - start + 1


def parse_rfc3339(text):
    """Grafana/Prometheus의 lastEvaluation(예: 2026-10-15T12:00:00.123456789Z 또는 +09:00) → epoch 초(실수)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$", text or "")
    if not m:
        raise ValueError(text)
    base = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    frac = float(m.group(2)) if m.group(2) else 0.0
    tz = m.group(3) or "Z"
    off = 0
    if tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        off = sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    return base + frac - off


def drift_from_grid(times, step=10.0):
    """첫 시각 기준 누적 이탈: max |t_i − t_0 − i·step|. times는 초 단위 실수 목록."""
    if len(times) < 2:
        return 0.0
    t0 = times[0]
    return max(abs(t - t0 - i * step) for i, t in enumerate(times))


def h_sample(for_s, d_s_max, d_e_min, margin):
    """발화 보장 하한 표본: H ≥ for + 10 + D_s − D_e (+여유)."""
    return for_s + 10 + d_s_max - d_e_min + margin


def h_final(h_model, h_samp, ceiling=150):
    """H = max(모형 하한, 표본)을 5초 단위로 올림. 상한 초과면 None(멈춤)."""
    value = max(h_model, h_samp)
    value = int(-(-value // 5) * 5)
    return None if value > ceiling else value


def label_set_ok(labels, allowed=("app", "role", "level")):
    """Loki 레이블 이름 목록이 {app, role, level}과 정확히 같은가(내부 `__…__` 레이블은 제외)."""
    visible = {x for x in labels if not x.startswith("__")}
    return visible == set(allowed)


def parse_vector(body):
    """Loki/Prometheus 즉시 질의 응답 → {role: float}. 결과 없음이면 {}."""
    out = {}
    for item in body.get("data", {}).get("result", []):
        out[item.get("metric", {}).get("role", "")] = float(item["value"][1])
    return out


def flatten_rules(body):
    rules = []
    for group in body.get("data", {}).get("groups", []):
        for r in group.get("rules", []):
            rules.append(r)
    return rules


# ---------------------------------------------------------------------------------------------------------
# 스텁(호스트 프로세스): 지표 스텁·웹훅 스텁
# ---------------------------------------------------------------------------------------------------------
class MetricsStub:
    """Bearer를 검사하는 가짜 `/actuator/prometheus`. 정지(stop)=연결 즉시 거부, hang 모드=연결은 받고 응답하지 않음(scrape_timeout)."""

    def __init__(self, port, token, host="127.0.0.1"):
        self.port, self.token, self.host = port, token, host
        self.server = None
        self.mode = "ok"
        self.requests = 0
        self.denied = 0

    def start(self, mode="ok"):
        self.mode = mode
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                pass

            def do_GET(self):  # noqa: N802
                outer.requests += 1
                if outer.mode == "hang":
                    time.sleep(30)
                    return
                if self.headers.get("Authorization") != f"Bearer {outer.token}" or self.path != "/actuator/prometheus":
                    outer.denied += 1
                    self.send_response(401)
                    self.end_headers()
                    return
                body = b"# HELP spike_stub_up stub\n# TYPE spike_stub_up gauge\nspike_stub_up 1\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer((self.host, self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


class WebhookStub:
    """Grafana 웹훅을 받아 시각·헤더·본문을 기록한다(수신은 항상 200)."""

    def __init__(self, port, host="127.0.0.1"):
        self.port, self.host = port, host
        self.received = []
        self.lock = threading.Lock()
        self.server = None

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {}
                with outer.lock:
                    outer.received.append({"t": time.time(), "path": self.path,
                                           "headers": {k: v for k, v in self.headers.items()}, "body": body})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer((self.host, self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def snapshot(self):
        with self.lock:
            return list(self.received)

    @staticmethod
    def rule_of(item):
        """알림 본문의 규칙 레이블(그룹 단위로 오므로 첫 알림 기준)."""
        alerts = item["body"].get("alerts") or []
        return (alerts[0].get("labels", {}).get("slack_lab_rule") if alerts else None)

    @staticmethod
    def status_of(item):
        return item["body"].get("status")


# ---------------------------------------------------------------------------------------------------------
# 로그 쓰기(실제 경로: 파일 → Alloy → Loki)
# ---------------------------------------------------------------------------------------------------------
class LogWriter:
    def __init__(self, base_dir):
        self.base = base_dir
        self.lock = threading.Lock()
        for role in ("receiver", "worker", "reactor", "all"):
            os.makedirs(os.path.join(base_dir, role), exist_ok=True)
            # 파일을 미리 만든다: Alloy가 새로 생긴 파일을 발견하기 전에 쓴 첫 줄은 tail_from_end 때문에 읽히지 않는다(M46 스파이크 발견)
            open(os.path.join(base_dir, role, "slack-lab.log"), "a").close()

    def path(self, role):
        return os.path.join(self.base, role, "slack-lab.log")

    def write(self, role, line):
        with self.lock, open(self.path(role), "a", encoding="utf-8") as f:
            f.write(line + "\n")


class Emitter(threading.Thread):
    """10초마다 role=all 적체 스냅샷을 쓴다(BacklogReporter 흉내). state를 바꾸면 다음 줄부터 반영된다."""

    def __init__(self, writer, period=10.0):
        super().__init__(daemon=True)
        self.writer, self.period = writer, period
        self.state = {"queue_ready": 0, "defer": 0, "dead": 0, "retry": 0, "dlq": 0, "recovery": 0}
        self.paused = False
        self.stopped = False
        self.history = []  # (ts, 값 복사)

    def run(self):
        while not self.stopped:
            if not self.paused:
                ts = time.time()
                self.writer.write("all", snapshot_line(ts, **self.state))
                self.history.append((ts, dict(self.state)))
            time.sleep(self.period)

    def set(self, **kv):
        self.state.update(kv)


# ---------------------------------------------------------------------------------------------------------
# Grafana·데이터 소스 클라이언트
# ---------------------------------------------------------------------------------------------------------
class Grafana:
    def __init__(self, password, base="http://127.0.0.1:3000", user="admin"):
        self.base = base
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def request(self, method, path, body=None, timeout=20):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", self.auth)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw
        except (urllib.error.URLError, OSError) as e:
            return 0, str(e)

    def healthy(self):
        code, body = self.request("GET", "/api/health", timeout=5)
        return code == 200 and isinstance(body, dict) and body.get("database") == "ok"

    def version(self):
        code, body = self.request("GET", "/api/health", timeout=5)
        return body.get("version") if isinstance(body, dict) else None

    def query(self, ds, query, t=None, path=None):
        """데이터 소스 프록시로 즉시 질의. 반환 (HTTP 코드, 본문). ds: 'loki'|'prom'."""
        uid = "slack-lab-loki" if ds == "loki" else "slack-lab-prom"
        api = path or ("/loki/api/v1/query" if ds == "loki" else "/api/v1/query")
        qs = {"query": query}
        if t is not None:
            qs["time"] = f"{t:.3f}"
        if ds == "loki":
            qs["limit"] = "1000"
        return self.request("GET", f"/api/datasources/proxy/uid/{uid}{api}?" + urllib.parse.urlencode(qs))

    def query_range(self, ds, query, start, end):
        uid = "slack-lab-loki" if ds == "loki" else "slack-lab-prom"
        api = "/loki/api/v1/query_range" if ds == "loki" else "/api/v1/query_range"
        qs = {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "limit": "1000"}
        return self.request("GET", f"/api/datasources/proxy/uid/{uid}{api}?" + urllib.parse.urlencode(qs))

    def rules(self):
        code, body = self.request("GET", "/api/prometheus/grafana/api/v1/rules")
        return flatten_rules(body) if code == 200 and isinstance(body, dict) else []

    def rule(self, title):
        for r in self.rules():
            if r.get("name") == title:
                return r
        return None

    def rule_sample(self, title):
        r = self.rule(title)
        if not r:
            return None
        return (r.get("lastEvaluation", ""), r.get("state", ""), r.get("health", ""))

    def alert_view(self, title):
        """(lastEvaluation, 규칙 상태, health, 알림 인스턴스 상태 목록). 관측 불가(NoData)는 규칙 health가 아니라 인스턴스가 Pending(NoData)로 가는 것으로 보인다."""
        r = self.rule(title)
        if not r:
            return None
        return (r.get("lastEvaluation", ""), r.get("state", ""), r.get("health", ""), sorted(a.get("state", "") for a in r.get("alerts") or []))

    def provisioned_rule_uids(self):
        code, body = self.request("GET", "/api/v1/provisioning/alert-rules")
        return sorted(x.get("uid") for x in body) if code == 200 and isinstance(body, list) else None

    def eval_rule(self, uid):
        """프로비저닝된 규칙을 Grafana 평가 API로 한 번 돌린다(경로·형식은 고정 버전에서 확인). 반환 (코드, 본문)."""
        code, rule = self.request("GET", f"/api/v1/provisioning/alert-rules/{uid}")
        if code != 200:
            return code, rule
        payload = {"grafana_condition": {"condition": rule["condition"], "data": rule["data"], "now": iso(time.time())}}
        return self.request("POST", "/api/v1/eval", payload, timeout=40)


def wait_until(pred, timeout, interval=1.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
        except Exception:  # noqa: BLE001
            v = False
        if v:
            return v
        time.sleep(interval)
    return False


def poll_rule(grafana, title, seconds, interval=0.5, stop_when=None):
    """seconds 동안 규칙 상태를 폴링해 [(lastEvaluation, state, health, 폴링 시각)]을 돌려준다."""
    out = []
    end = time.time() + seconds
    while time.time() < end:
        s = grafana.rule_sample(title)
        if s:
            out.append((*s, time.time()))
            if stop_when and stop_when(out):
                break
        time.sleep(interval)
    return out


def live_recovery(grafana, title, inject, timeout=100):
    """관측 실패 줄을 넣고(inject(t0)) 규칙이 교란(Pending/NoData 등)됐다가 평상(inactive·ok·알림 Normal)으로 돌아오는 첫 평가까지 본다.
    반환 (t0, 교란 관측 여부, 복귀한 첫 평가의 lastEvaluation 또는 None, 상태 변화 기록)."""
    t0 = time.time()
    inject(t0)
    disturbed, back, log = False, None, []
    end = t0 + timeout
    while time.time() < end:
        v = grafana.alert_view(title)
        if v:
            if not log or log[-1][1:] != (v[1], v[2], tuple(v[3])):
                log.append((round(time.time() - t0, 1), v[1], v[2], tuple(v[3])))
            calm = v[1] == "inactive" and v[2] == "ok" and all(x == "Normal" for x in v[3])
            if not calm:
                disturbed = True
            elif disturbed:
                back = v[0]
                break
        time.sleep(0.5)
    return t0, disturbed, back, log
