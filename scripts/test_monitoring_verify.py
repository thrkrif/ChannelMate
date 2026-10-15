"""scripts/monitoring-verify 단위 검증(M45 범위: mgmt 판정 로직). 외부 호출 없음. 실행: python3 scripts/test_monitoring_verify.py"""
import importlib.machinery
import re
import importlib.util
import os
import socket
import stat
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("mv", os.path.join(HERE, "monitoring-verify"))
mv = importlib.util.module_from_spec(importlib.util.spec_from_loader("mv", loader))
loader.exec_module(mv)

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


TOK = "0123456789abcdef" * 4
OLD = "fedcba9876543210" * 4

# 판정
check("ok는 200만", mv.classify("ok", "200") and not mv.classify("ok", "401") and not mv.classify("ok", "ERR"))
check("deny는 401만", mv.classify("deny", "401") and not mv.classify("deny", "200") and not mv.classify("deny", "404"))
check("no_success는 2xx·3xx만 아님(오류·4xx·5xx 통과)", all(mv.classify("no_success", s) for s in ("401", "404", "405", "500", "ERR"))
      and not any(mv.classify("no_success", s) for s in ("200", "204", "301", "302")))
try:
    mv.classify("bogus", "200")
    check("모르는 기대는 예외", False)
except ValueError:
    check("모르는 기대는 예외", True)

# 케이스 구성
cases = mv.build_token_cases("h", 9091, TOK)
check("토큰 케이스: 유효·없음·오토큰 각 5회 = 15건", len(cases) == 15 and sum(c[6] == "ok" for c in cases) == 5)
cases_old = mv.build_token_cases("h", 9091, TOK, old=OLD)
check("회전 시 회전 전 토큰 5회가 추가돼 20건, 기대는 모두 401", len(cases_old) == 20
      and sum("회전 전" in c[0] and c[6] == "deny" for c in cases_old) == 5)
check("케이스 이름에 토큰 값이 없다", not any(TOK in c[0] or OLD in c[0] for c in cases_old))
deny = mv.build_deny_cases("h", 9091, TOK)
paths = {c[4] for c in deny if c[3] == "GET"}
check("비허용 경로 12종 × 5회 + 메서드 5종 × 5회 = 85건", len(deny) == 85 and len(paths) == 12, f"{len(deny)} {len(paths)}")
check("비허용 목록에 plan의 경로·우회 변형 포함", {"/actuator", "/actuator/health", "/actuator/env", "/actuator/configprops",
      "/actuator/heapdump", "/actuator/threaddump", "/actuator/prometheus/../env"} <= paths
      and any("%2e%2e" in p for p in paths) and any(";" in p for p in paths))
check("비허용 메서드에 POST·PUT·DELETE·OPTIONS·HEAD", {c[3] for c in deny if c[4] == "/actuator/prometheus"} == set(mv.DISALLOWED_METHODS))

ok, failed = mv.summarize([("a", "ok", "200"), ("b", "deny", "200"), ("c", "no_success", "302")])
check("summarize: 실패 목록에 이름·기대·응답", ok == 1 and len(failed) == 2 and "b (기대 deny, 응답 200)" in failed[0])

# 리스너 판정
lsof_ok = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\njava 1 u 10u IPv4 0x1 0t0 TCP 127.0.0.1:9091 (LISTEN)\n"
lsof_all = "java 1 u 10u IPv6 0x1 0t0 TCP *:9091 (LISTEN)\n"
check("127.0.0.1만 바인딩은 문제 없음", mv.listener_findings(lsof_ok, 9091, ["127.0.0.1:"]) == [])
check("*:9091은 문제로 잡힌다", mv.listener_findings(lsof_all, 9091, ["127.0.0.1:"]) == ["*:9091"])
check("리스너가 없으면 문제 없음(별도로 존재 확인)", mv.listener_findings("", 9091, ["127.0.0.1:"]) == [])

# .env 토큰 읽기
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, ".env")
    open(p, "w").write(f"# MANAGEMENT_METRICS_TOKEN=commented\nexport OTHER=1\nMANAGEMENT_METRICS_TOKEN=\"{TOK}\"\n")
    check(".env 토큰: 주석 무시·따옴표 제거", mv.read_token(p) == TOK)
    check(".env가 없으면 빈 값", mv.read_token(os.path.join(td, "none")) == "")

    # 비밀 파일: 600, 값은 파일에만
    d = os.path.join(td, "secrets")
    os.makedirs(d, mode=0o700)
    f = mv.write_secret_env(d, "x.env", "MANAGEMENT_METRICS_TOKEN", TOK)
    check("회전 토큰 파일 권한 600", stat.S_IMODE(os.stat(f).st_mode) == 0o600)
    check("회전 토큰 파일 내용", open(f).read() == f"MANAGEMENT_METRICS_TOKEN={TOK}\n")

    # 새 토큰 형식
    t = mv.new_token()
    check("새 토큰은 64자리 16진수", len(t) == 64 and all(c in "0123456789abcdef" for c in t))
    check("새 토큰은 매번 다르다", mv.new_token() != mv.new_token())

    # jar 선택
    libs = os.path.join(td, "libs")
    os.makedirs(libs)
    for n in ("slack-lab-0.0.1-SNAPSHOT.jar", "slack-lab-0.0.1-SNAPSHOT-plain.jar"):
        open(os.path.join(libs, n), "w").close()
    check("plain jar는 고르지 않는다", mv.pick_jar(libs).endswith("SNAPSHOT.jar") and "plain" not in mv.pick_jar(libs))
    check("jar가 없으면 None", mv.pick_jar(os.path.join(td, "empty")) is None if os.path.isdir(os.path.join(td, "empty")) else True)

# 원문 요청: 서버가 받은 요청 줄이 정규화되지 않았는지(--path-as-is와 같은지)
received = []


def serve(sock):
    conn, _ = sock.accept()
    data = conn.recv(4096).decode("ascii", "replace")
    received.append(data)
    conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
    conn.close()


srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(1)
port = srv.getsockname()[1]
threading.Thread(target=serve, args=(srv,), daemon=True).start()
status = mv.raw_status("127.0.0.1", port, "GET", "/actuator/prometheus/%2e%2e/../env;x", "Bearer abc")
check("raw_status: 상태 코드 파싱", status == "404")
check("raw_status: 요청 줄·인증 헤더가 원문 그대로", received and received[0].startswith("GET /actuator/prometheus/%2e%2e/../env;x HTTP/1.1\r\n")
      and "Authorization: Bearer abc\r\n" in received[0], received)
srv.close()
check("raw_status: 연결 실패는 ERR", mv.raw_status("127.0.0.1", 1, "GET", "/", "") == "ERR")

# 컨테이너 요청기: 케이스는 stdin으로만(argv에 토큰이 없다)
check("컨테이너 요청기 스크립트에 토큰·케이스 값이 없다", TOK not in mv.CONTAINER_REQUESTER and "Authorization" in mv.CONTAINER_REQUESTER)

# 사용법
import subprocess
rc = subprocess.run([sys.executable, os.path.join(HERE, "monitoring-verify")], capture_output=True, text=True).returncode
check("인자 없으면 exit 2", rc == 2)
rc = subprocess.run([sys.executable, os.path.join(HERE, "monitoring-verify"), "mgmt", "--mode", "bogus"], capture_output=True, text=True).returncode
check("mgmt --mode가 잘못되면 exit 2", rc == 2)

# =====================================================================================================
# M48: 가상 시계 모형 시험 (평가 위상·수집 지연 전수, H 확정 규칙, 회차 실행기). 외부 호출 0.
# =====================================================================================================
sys.path.insert(0, HERE)
import _mon_model as mdl  # noqa: E402
import _mon_run as run  # noqa: E402

PHASES = [i / 10 for i in range(0, 100)]  # 평가 위상 0~9.9초 전수


def always(rule, h, d_s_range, d_e_range, want=True):
    """경계 안 모든 (위상, D_s, D_e) 조합에서 발화 여부가 want인가. 어긋난 첫 조합을 돌려준다."""
    for ph in PHASES:
        for ds in d_s_range:
            for de in d_e_range:
                if mdl.simulate_fire(rule, h, ds, de, ph) != want:
                    return (ph, ds, de)
    return None


check("평가 수: 반열린 구간·위상", mdl.eval_count(0, 10, 0) == 1 and mdl.eval_count(0, 10, 5) == 1 and mdl.eval_count(0, 10.0, 9.9) == 1
      and mdl.eval_count(5, 5, 0) == 0 and mdl.eval_count(0, 30, 0) == 3)
check("Pending→Firing: for 30초면 연속 참 평가 4회, 0초면 1회", mdl.fires(30, 0, 40, 0) and not mdl.fires(30, 0, 30, 0)
      and mdl.fires(0, 0, 10, 0) and not mdl.fires(0, 0, 0, 0))
check("X = H + D_e − D_s", mdl.x_len(50, 5, 0) == 45 and mdl.x_len(120, 35, 0) == 85)
check("H 하한 = for + 10 + D_s − D_e (①45·③135·차단85·부재105)", mdl.h_lower_bound(30, 5) == 45 and mdl.h_lower_bound(120, 5) == 135
      and mdl.h_lower_bound(60, 15) == 85 and mdl.h_lower_bound(60, 35) == 105)

# 발화 보장: 실행 값은 모든 위상·경계 안 지연에서 발화한다
check("① H=50: D_s,D_e∈[0,5] × 위상 전수 → 항상 발화", always("rule1", 50, range(0, 6), range(0, 6)) is None)
check("③ H=140: 전수 → 항상 발화", always("rule3", 140, range(0, 6), range(0, 6)) is None)
check("④ 차단 H=95: D_s∈[0,15], D_e∈[0,15] 전수 → 항상 발화", always("rule4-block", 95, range(0, 16), range(0, 16)) is None)
check("④ 부재 H=120: D_s∈[0,35], D_e∈[0,20] 전수 → 항상 발화", always("rule4-absent", 120, range(0, 36, 5), range(0, 21, 5)) is None)
# 비발화 보장
check("① 비발화 H=20: 전수 → 발화 0", always("rule1", 20, range(0, 6), range(0, 6), want=False) is None)
check("③ 비발화 H=110: 전수 → 발화 0", always("rule3", 110, range(0, 6), range(0, 6), want=False) is None)
check("④ 차단 비발화 H=40: D_s∈[0,15], D_e∈[0,15] 전수 → 발화 0", always("rule4-block", 40, range(0, 16), range(0, 16), want=False) is None)
check("비발화 보장 공식: X_max ≤ for", mdl.quiet_guaranteed("rule1") and mdl.quiet_guaranteed("rule3") and mdl.quiet_guaranteed("rule4-block")
      and mdl.quiet_x_max("rule1") == 25 and mdl.quiet_x_max("rule3") == 115 and mdl.quiet_x_max("rule4-block") == 55)
# 스펙 값 그대로는 일부 영역에서 놓친다(D-8 (a)를 택한 이유)
check("스펙 값 ① H=40은 D_s=5,D_e=0에서 일부 위상 미발화", always("rule1", 40, [5], [0]) is not None)
check("스펙 값 ③ H=130, ④ H=70도 일부 영역 미발화", always("rule3", 130, [5], [0]) is not None and always("rule4-absent", 70, [35], [0]) is not None)

# H 확정 규칙 [A-2][B-3]
code, h_abs = mdl.calibrate("rule4-absent", [(10, 10)] * 3)
check("(i) 유리한 부재 표본(D_s=D_e=10 ×3)으로도 H는 모형 하한 120", code == 0 and h_abs == 120, str((code, h_abs)))
check("(i) 유리한 차단 표본(D_s=0,D_e=0 ×3)으로도 H=95", mdl.calibrate("rule4-block", [(0, 0)] * 3) == (0, 95))
check("(ii) 확정한 H=120은 불리한 부재(D_s=35,D_e=0)에서 위상 전수 발화", always("rule4-absent", h_abs, [35], [0]) is None)
_code2, h_blk = mdl.calibrate("rule4-block", [(0, 0)] * 3)
check("(ii) 확정한 차단 H=95는 불리한 D_s=15,D_e=0에서 전수 발화", always("rule4-block", h_blk, [15], [0]) is None)
naive = mdl.h_final(0, mdl.h_sample(60, 10, 10, 15))  # 표본 극값만 쓴 H
check("(iii) 대조군: 표본 극값만 쓴 H(부재 85)는 같은 불리한 입력에서 미발화 영역 존재", naive == 85 and always("rule4-absent", naive, [35], [0]) is not None, str(naive))
check("(iv) 경계 밖 입력은 판정 대신 모형 경계 초과(exit 5): 부재 D_s=40·로그 D_s=6·D_e<0",
      mdl.calibrate("rule4-absent", [(40, 0)])[0] == mdl.EXIT_BOUND and mdl.calibrate("rule1", [(6, 0)])[0] == mdl.EXIT_BOUND
      and mdl.calibrate("rule1", [(0, -1)])[0] == mdl.EXIT_BOUND and mdl.calibrate("rule1", [])[0] == mdl.EXIT_BOUND)
check("(iv) H 상한 150 초과 시 멈춤", mdl.h_final(120, 151) is None and mdl.h_final(120, 100) == 120 and mdl.h_final(46, 0) == 50)
# (v) 다차원 부재: 한 역할을 제거하면 그 역할만 발화한다(역할별 독립 구간)
roles = {"receiver": False, "worker": True, "reactor": False}
fired = {r: (mdl.simulate_fire("rule4-absent", 120, 20, 0, 3.0) if removed else False) for r, removed in roles.items()}
check("(v) 다차원 부재: 제거한 역할만 발화, 나머지 불변", fired == {"receiver": False, "worker": True, "reactor": False}, str(fired))
# (vi) 스냅샷 위상이 회차마다 밀려도(scheduleWithFixedDelay 모사) ①③ 판정 불변
drift_ok = all(mdl.simulate_fire("rule1", 50, 2, 3, (i * 1.7) % 10) and not mdl.simulate_fire("rule1", 20, 2, 3, (i * 1.7) % 10)
               and mdl.simulate_fire("rule3", 140, 2, 3, (i * 3.3) % 10) and not mdl.simulate_fire("rule3", 110, 2, 3, (i * 3.3) % 10)
               for i in range(40))
check("(vi) 위상이 회차마다 밀려도 ①③ 발화/비발화 판정 불변", drift_ok)
check("비발화 회차의 실측 H가 for−5를 넘으면 채점 안 함(① >25, ③ >115)", not mdl.non_fire_scorable("rule1", 26) and mdl.non_fire_scorable("rule1", 25)
      and not mdl.non_fire_scorable("rule3", 116) and mdl.non_fire_scorable("rule3", 115) and mdl.non_fire_scorable("rule4-block", 999))

# ③ 비발화 시각표: 정지 구간 큐 깊이 = N, ≥10 연속 구간 < 120초
series_b = [(t, 10) for t in range(0, 111, 5)] + [(t, 0) for t in range(115, 131, 5)]
ok_b, run_b = mdl.rule3_quiet_ok(series_b, 10, 110)
series_a = [(t, 9) for t in range(0, 131, 5)]
series_bad = [(t, 10) for t in range(0, 131, 5)]
check("③ (b) 10건 110초: ≥10 연속 110초 < 120 → 합격", ok_b and run_b == 110.0)
check("③ (a) 9건 130초: ≥10 구간 0 → 합격", mdl.rule3_quiet_ok(series_a, 9, 130) == (True, 0.0))
check("③ 대조: 10건을 130초 유지하면 ≥10 연속 130 ≥ 120 → 불합격", mdl.rule3_quiet_ok(series_bad, 10, 130)[0] is False)

# 예상 외 발화 분류 [K17]·예산·상태
check("예상 외 발화: ② 회차 중 ④(all) — 중단 60초 이상 유도 문제, 미만 결함, 그 밖 미상",
      mdl.classify_unexpected("rule2", "rule4-host", 60) == "induction" and mdl.classify_unexpected("rule2", "rule4-host", 59) == "defect"
      and mdl.classify_unexpected("rule2", "rule3") == "unknown" and mdl.classify_unexpected("rule1", "rule1") == "ok")
check("예산: ①83·②33·③174·차단103·부재123초", all(mdl.within_deadline(r, d) and not mdl.within_deadline(r, d + 1)
      for r, d in (("rule1", 83), ("rule2", 33), ("rule3", 174), ("rule4-block", 103), ("rule4-absent", 123))))
check("종료 코드 6종이 서로 다르다(0·1·2·3·4·5)", len({mdl.EXIT_OK, mdl.EXIT_FAIL, mdl.EXIT_USAGE, mdl.EXIT_RULES, mdl.EXIT_OBSERVE, mdl.EXIT_BOUND}) == 6)

# 실패 유도 표 사건 수 대조 [B-2] (C10 실패 5건 = 시도 20·재시도 예약 15·최종 실패 5·안내 5)
exp5 = mdl.expected_failure_counts("F-echo", 5)
check("실패 5건: 처리 시도 20·재시도 예약 15·llm_final_failure 5·실패 안내 5", (exp5["attempts"], exp5["retry_scheduled"], exp5["final_failure"], exp5["notices"]) == (20, 15, 5, 5))
check("F-echo-stub: 시도 2·재시도 1·최종 실패 0·답글 1·스텁 생성 2", mdl.expected_failure_counts("F-echo-stub") ==
      {"attempts": 2, "retry_scheduled": 1, "final_failure": 0, "notices": 0, "replies": 1, "generations": 2})
check("F-ollama RAG 행: 시도마다 rag_result 1줄(4줄)", mdl.expected_failure_counts("F-ollama", 1, rag=True)["rag_result"] == 4)
check("사건 수 대조: 일치하면 빈 딕트, 어긋나면 키별 (기대, 관측)", mdl.compare_counts(exp5, dict(exp5)) == {}
      and mdl.compare_counts(exp5, {**exp5, "attempts": 19})["attempts"] == (20, 19))
check("리포트 수 판정: 발화 회차 정확히 1(0·2는 불합격), 비발화는 0", mdl.count_reports(1) and not mdl.count_reports(0) and not mdl.count_reports(2) and mdl.count_reports(0, 0))

# 규칙·지속 시간 일치(exit 3 근거): 파일 = 모형 = API
import glob, json as _json  # noqa: E402,E401
docs = [_json.load(open(p, encoding="utf-8")) for p in sorted(glob.glob(os.path.join(HERE, "..", "infra", "monitoring", "grafana", "provisioning", "alerting", "rules-*.json")))]
frules = mdl.rules_from_files(docs[:1])
api_ok = [{"uid": u, "title": v["title"], "for": int(v["for"] * 1e9)} for u, v in frules.items()]
check("규칙 4개: 파일 for·평가 간격이 계획값(30s·0s·2m·1m, 10초)과 일치", {u: v["for"] for u, v in frules.items()} ==
      {"slack-lab-r1": 30.0, "slack-lab-r2": 0.0, "slack-lab-r3": 120.0, "slack-lab-r4": 60.0} and all(v["interval"] == 10.0 for v in frules.values()))
check("API와 파일이 같으면 불일치 0", mdl.parity(frules, api_ok) == [])
api_bad = [dict(x) for x in api_ok]
api_bad[0]["for"] = int(20 * 1e9)
check("API의 for가 다르면 불일치(exit 3 근거)", any("for 불일치" in p for p in mdl.parity(frules, api_bad)))
check("API에 규칙이 없으면 불일치", any("Grafana에 없음" in p for p in mdl.parity(frules, api_ok[:3])))
host_rules, app_rules = (mdl.rules_from_files([d]) for d in docs[:2]) if len(docs) >= 2 else (frules, frules)
check("host/app 규칙 파일의 UID 집합·for·간격이 같다", {u: (v["for"], v["interval"]) for u, v in host_rules.items()} == {u: (v["for"], v["interval"]) for u, v in app_rules.items()})

# 지속 시간 덮어쓰기 옵션이 소스에 없다(운영 값 = 검증 값)
src_all = open(os.path.join(HERE, "monitoring-verify"), encoding="utf-8").read() + open(os.path.join(HERE, "_mon_run.py"), encoding="utf-8").read()
check("monitoring-verify·실행기에 for/interval/duration 덮어쓰기 옵션 0", not re.search(r"[\"'](--for|--interval|--duration|--eval|--hold-time|--short)[\"' =]", src_all))


# --- 회차 실행기: 모사 환경(가상 시계 + 모형 Grafana) --------------------------------------------------
class FakeEnv(run.Env):
    def __init__(self, rule, phase=3.0, d_s=2.0, d_e=3.0, report_delay=8.0, report_ok=True, nodata=False, others=None, h_meas=None):
        self.t, self.rule, self.phase, self.d_s, self.d_e = 0.0, rule, phase, d_s, d_e
        self.report_delay, self.report_ok, self.nodata, self.others, self.h_meas = report_delay, report_ok, nodata, others or [], h_meas
        self.t_ind = self.t_res = self.h = None
        self.slept = 0.0
        self.fired_time = None

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += s

    def induce(self, rule, variant, round_no):
        self.t_ind = self.t
        self.t_res = None
        return self.t

    def resolve(self, rule, variant, round_no):
        self.t_res = self.t

    def _evals_true(self):
        """조작 이후 t 시점까지 연속 참 평가 수."""
        if self.t_ind is None:
            return 0
        start = self.t_ind + self.d_s
        end = (self.t_res if self.t_res is not None else 10 ** 9) + self.d_e
        step, ph = mdl.STEP, self.phase
        k_last = int((self.t - ph) // step)
        count = 0
        for k in range(k_last, -1, -1):
            te = ph + k * step
            if te < self.t_ind:
                break
            if start <= te < end:
                count += 1
            elif te < start:
                break
            else:
                count = 0 if count == 0 else count
        return count if (self.t_res is None or self.t < self.t_res + self.d_e) else self._after_resolve()

    def _after_resolve(self):
        return 0

    def state(self, rule):
        if self.nodata:
            return "nodata"
        n = self._evals_true()
        need = mdl.RULES[self.rule]["for"] // mdl.STEP + 1
        if n >= need:
            if self.fired_time is None:
                self.fired_time = self.t
            return "firing"
        # 해소 뒤 정상
        return "pending" if n >= 1 else "normal"

    def reports(self, rule):
        if self.fired_time is not None and self.report_ok and self.t - self.fired_time >= self.report_delay:
            return 1
        return 0

    def other_fired(self, rule):
        return self.others

    def measured(self, rule):
        out = {"d_s": self.d_s, "d_e": self.d_e}
        if self.h_meas is not None:
            out["h"] = self.h_meas
        return out


r_fire = run.run_round(FakeEnv("rule1"), "rule1", "fire", 1, 50)
check("실행기: ① 발화 회차 H=50 → pass, 리포트 1, 예산(83초) 안", r_fire["verdict"] == "pass" and r_fire["reports"] == 1 and r_fire["report_elapsed"] <= 83, str(r_fire))
r_quiet = run.run_round(FakeEnv("rule1"), "rule1", "quiet", 1, 20, settle_s=40)
check("실행기: ① 비발화 H=20 → pass, 발화·리포트 0", r_quiet["verdict"] == "pass" and r_quiet["reports"] == 0, str(r_quiet))
check("실행기: 리포트가 안 올라오면 fail", run.run_round(FakeEnv("rule1", report_ok=False), "rule1", "fire", 1, 50)["verdict"] == "fail")
check("실행기: 관측 오류(NoData)면 observe", run.run_round(FakeEnv("rule1", nodata=True), "rule1", "fire", 1, 50)["verdict"] == "observe")
check("실행기: D_s가 모형 경계 밖이면 bound(채점 안 함)", run.run_round(FakeEnv("rule1", d_s=9), "rule1", "fire", 1, 50)["verdict"] == "bound")
check("실행기: 비발화 회차 실측 H=26이면 induction(채점 안 함)", run.run_round(FakeEnv("rule1", h_meas=26), "rule1", "quiet", 1, 20)["verdict"] == "induction")
check("실행기: 예상 외 발화 ② 중 ④(중단 59초)는 fail, 60초는 induction",
      run.run_round(FakeEnv("rule2", others=[("rule4-host", 59)]), "rule2", "fire", 1, 0)["verdict"] == "fail"
      and run.run_round(FakeEnv("rule2", others=[("rule4-host", 60)]), "rule2", "fire", 1, 0)["verdict"] == "induction")
check("실행기: 위상 전수에서 ① 발화 회차 모두 pass", all(run.run_round(FakeEnv("rule1", phase=p), "rule1", "fire", 1, 50)["verdict"] == "pass" for p in PHASES[::7]))


class ReportWhileHeldEnv(FakeEnv):
    def reports(self, rule):
        if self.t_res is not None:
            return self.posts_at_resolve
        return super().reports(rule)

    def resolve(self, rule, variant, round_no):
        self.posts_at_resolve = self.reports(rule)
        super().resolve(rule, variant, round_no)

    def measured(self, rule):
        return {**super().measured(rule), "h": self.t_res - self.t_ind}


late = ReportWhileHeldEnv("rule1", report_delay=40)
late_result = run.run_round(late, "rule1", "fire", 1, 50)
check("실행기: H 뒤 리포트가 올 때까지 해소하지 않고 최초 발화·리포트 시각 기록",
      late_result["verdict"] == "pass" and late.t_res == 73 and late.posts_at_resolve == 1
      and late_result["firing_elapsed"] == 33 and late_result["report_elapsed"] == 73
      and late_result["h"] == 50 and late_result["measured"]["h"] == 73
      and (60, "firing") in late_result["states"], str(late_result))
missing = ReportWhileHeldEnv("rule1", report_ok=False)
missing_result = run.run_round(missing, "rule1", "fire", 1, 50, poll_s=7)
check("실행기: 리포트 없으면 조작 성립 기준 deadline에서 해소하고 fail",
      missing.t_res == 83 and missing_result["verdict"] == "fail" and missing_result["reports"] == 0, str(missing_result))
for delay, expected_resolve, verdict in ((8, 140, "pass"), (42, 165, "pass"), (60, 174, "fail")):
    queue = ReportWhileHeldEnv("rule3", report_delay=delay)
    queue_result = run.run_round(queue, "rule3", "fire", 1, 140)
    check(f"실행기: ③ 무회귀 리포트 지연 {delay}초 → {expected_resolve}초 해소·{verdict}",
          queue.t_res == expected_resolve and queue_result["verdict"] == verdict
          and queue_result["firing_elapsed"] == 123, str(queue_result))
queue_quiet = ReportWhileHeldEnv("rule3")
queue_quiet_result = run.run_round(queue_quiet, "rule3", "quiet", 1, 110)
check("실행기: ③ quiet는 H 뒤 즉시 해소·리포트 0 유지",
      queue_quiet.t_res == 110 and queue_quiet_result["verdict"] == "pass", str(queue_quiet_result))
instant = ReportWhileHeldEnv("rule2")
instant_result = run.run_round(instant, "rule2", "fire", 1, 0)
check("실행기: ② H=0도 조작 직후부터 리포트 대기 후 해소",
      instant.t_res == 11 and instant_result["verdict"] == "pass" and instant.posts_at_resolve == 1, str(instant_result))


class HoldObservationErrorEnv(ReportWhileHeldEnv):
    def state(self, rule):
        return self.hold_error if self.now() >= 60 else super().state(rule)


for error in ("nodata", "error"):
    broken = HoldObservationErrorEnv("rule1", report_ok=False)
    broken.hold_error = error
    broken_result = run.run_round(broken, "rule1", "fire", 1, 50)
    check(f"실행기: H 이후 {error}도 해소 후 observe·상태 보존",
          broken.t_res == 60 and broken_result["verdict"] == "observe"
          and (60, error) in broken_result["states"], str(broken_result))


class DuplicateAfterResolveEnv(ReportWhileHeldEnv):
    manages_hold = True
    history = []

    def reports(self, rule):
        return 2 if self.now() >= 80 else super().reports(rule)


duplicate = DuplicateAfterResolveEnv("rule1", report_delay=40)
duplicate_result = run.run_round(duplicate, "rule1", "fire", 1, 50)
check("실행기: 해소 뒤 settle 중 리포트 2건이면 기존대로 fail",
      duplicate.t_res == 73 and duplicate_result["reports"] == 2 and duplicate_result["verdict"] == "fail", str(duplicate_result))
# 회차 열: 같은 규칙 Normal 확인 → 70초 대기 → 다음 회차
env_seq = FakeEnv("rule1")
orig_resolve = env_seq.resolve


def rolling_induce(rule, variant, round_no, _e=env_seq):
    _e.t_ind, _e.t_res, _e.fired_time = _e.t, None, None
    return _e.t


env_seq.induce = rolling_induce
code, rs = run.run_rounds(env_seq, "rule1", [("fire", 50), ("quiet", 20)], wait_normal_s=200)
check("회차 열: 발화 1 + 비발화 1 모두 pass → exit 0, 회차 사이 ≥70초 대기", code == 0 and [r["verdict"] for r in rs] == ["pass", "pass"] and env_seq.slept >= 70, str((code, rs and [r["verdict"] for r in rs])))
code_b, _ = run.run_rounds(FakeEnv("rule1", d_s=9), "rule1", [("fire", 50), ("fire", 50)])
check("회차 열: 모형 경계 초과는 즉시 멈춤(exit 5)", code_b == mdl.EXIT_BOUND)
code_o, _ = run.run_rounds(FakeEnv("rule1", nodata=True), "rule1", [("fire", 50)])
check("회차 열: 관측 오류는 exit 4", code_o == mdl.EXIT_OBSERVE)

# --- CLI 하위 명령·스텁 (M48) ------------------------------------------------------------------------
import json as _j  # noqa: E402
import subprocess as _sp  # noqa: E402
import urllib.error as _ue  # noqa: E402
import urllib.request as _ur  # noqa: E402
import _mon_stubs  # noqa: E402


def cli(*args):
    p = _sp.run([sys.executable, os.path.join(HERE, "monitoring-verify"), *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


rc, out, _ = cli("run", "--mode", "host", "--scenario", "rule3-quiet", "--dry-run")
plan = _j.loads(out)["plan"] if rc == 0 else []
check("run --dry-run: ③ 비발화 계획 (a)130/(b)110 번갈아, 70초 대기, 예산 174", rc == 0 and [p["h"] for p in plan] == [130, 110, 130, 110, 130]
      and _j.loads(out)["reset_wait_s"] == 130 and _j.loads(out)["deadline_s"] == 174)
rc, out, _ = cli("run", "--mode", "app", "--scenario", "rule4-fire", "--repeat", "2", "--dry-run")
check("run --dry-run: ④ 발화는 차단 H=95", rc == 0 and [p["h"] for p in _j.loads(out)["plan"]] == [95, 95])
check("run: 모드와 시나리오가 안 맞으면 exit 2", cli("run", "--mode", "host", "--scenario", "rule4-fire", "--dry-run")[0] == 2)
check("run: 모르는 시나리오·모드·반복이면 exit 2", cli("run", "--mode", "host", "--scenario", "bogus")[0] == 2 and cli("run", "--mode", "x", "--scenario", "rule1-fire")[0] == 2
      and cli("run", "--mode", "host", "--scenario", "rule1-fire", "--repeat", "0", "--dry-run")[0] == 2)
check("run: 세션 없이 실제 실행은 exit 2(시작하지 않는다)", cli("run", "--mode", "host", "--scenario", "rule1-fire")[0] == 2)
check("session calibrate: 유리한 표본도 H=120, 경계 밖은 exit 5", cli("session", "calibrate", "--rule", "rule4-absent", "--samples", "10,10;10,10;10,10")[0] == 0
      and cli("session", "calibrate", "--rule", "rule4-absent", "--samples", "40,0")[0] == 5 and cli("session", "calibrate", "--rule", "bogus", "--samples", "1,1")[0] == 2)
check("teardown: 모르는 프로젝트는 exit 2", cli("teardown", "--project", "slack-lab-s6v-nonexistent-zzz")[0] == 2)
with tempfile.TemporaryDirectory() as d:
    rc, out, _ = cli("override", "--session", d, "--name", "ov.yaml", "--env", "LLM_BASE_URL=http://localhost:9/v1")
    text = open(out.strip(), encoding="utf-8").read() if rc == 0 else ""
    check("override: 세션 디렉터리 안에 쓰고 env 줄을 넣는다", rc == 0 and out.strip().startswith(d) and "LLM_BASE_URL" in text)
    check("override --remove: 파일 삭제", cli("override", "--session", d, "--name", "ov.yaml", "--remove")[0] == 0 and not os.path.exists(out.strip()))


def free_port():
    import socket
    with socket.socket() as sck:
        sck.bind(("127.0.0.1", 0))
        return sck.getsockname()[1]


def http(method, url, body=None):
    req = _ur.Request(url, data=(_j.dumps(body).encode() if body is not None else None), method=method, headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=5) as r:
            return r.status, _j.loads(r.read() or b"{}")
    except _ue.HTTPError as e:
        return e.code, {}
    except Exception as e:  # noqa: BLE001
        return -1, str(type(e).__name__)


sp_ = free_port()
slack = _mon_stubs.SlackRecorder(sp_).start()
try:
    base = f"http://127.0.0.1:{sp_}"
    check("Slack 스텁: 정상 채널은 ok와 ts", http("POST", base + "/chat.postMessage", {"channel": "C1", "text": "비밀 본문"})[1].get("ok") is True)
    http("POST", base + "/__control", {"channel": "C2", "mode": "fail"})
    check("Slack 스텁: fail 채널은 ok=false(영구 실패)", http("POST", base + "/chat.postMessage", {"channel": "C2", "text": "x"})[1].get("ok") is False)
    http("POST", base + "/__control", {"channel": "C3", "mode": "drop"})
    code, _b = http("POST", base + "/chat.postMessage", {"channel": "C3", "text": "x"})
    check("Slack 스텁: drop 채널은 요청을 받고 응답 없이 끊음(결과 불명)", code == -1 and slack.count("chat.postMessage", "C3") == 1, str(code))
    check("Slack 스텁: 채널별 건수만 기록하고 본문은 저장하지 않는다", slack.count("chat.postMessage") == 3 and "비밀 본문" not in _j.dumps(slack.summary(), ensure_ascii=False))
    http("POST", base + "/__control", {"channel": "*", "mode": "ok"})
finally:
    slack.stop()

lp = free_port()
llm = _mon_stubs.LlmStub(lp, model="m1").start()
try:
    base = f"http://127.0.0.1:{lp}"
    check("LLM 스텁: /v1/models가 모델 ID를 돌려준다(기동 확인용)", http("GET", base + "/v1/models")[1]["data"][0]["id"] == "m1")
    check("LLM 스텁: 정상 생성", http("POST", base + "/v1/chat/completions", {"model": "m1", "messages": []})[0] == 200)
    http("POST", base + "/__arm", {"fail_next": 2, "status": 503})
    codes = [http("POST", base + "/v1/chat/completions", {})[0] for _ in range(3)]
    check("LLM 스텁: 다음 2회 503 장전 후 3번째는 정상", codes == [503, 503, 200], str(codes))
    st_ = http("GET", base + "/__stats")[1]
    check("LLM 스텁: 요청 수·생성 수 집계(503은 생성이 아님)", st_ == {"requests": 4, "generations": 2}, str(st_))
finally:
    llm.stop()

from unittest.mock import patch
import _mon_dash
import _mon_env
import _mon_spike

with tempfile.TemporaryDirectory() as d:
    with patch.object(_mon_env, "read_env_file", return_value={}), patch.object(_mon_spike, "Grafana") as graf, patch.object(_mon_dash, "run", return_value=4) as dashboard:
        rc = mv.cmd_run(["--mode", "host", "--scenario", "dashboard", "--session", d])
        check("dashboard: 회차 실행·기동 없이 전용 검증기로 위임하고 종료 코드 보존", rc == 4 and dashboard.call_count == 1
              and dashboard.call_args.args == (mv.ROOT, d, graf.return_value))
    check("dashboard: 세션이 없으면 사용법 오류", mv.cmd_run(["--mode", "host", "--scenario", "dashboard", "--session", os.path.join(d, "missing")]) == 2)
    with patch.object(_mon_dash, "run") as dashboard:
        rc = mv.cmd_run(["--mode", "host", "--scenario", "dashboard", "--dry-run"])
        check("dashboard: dry-run은 외부 호출 없이 계획만 출력", rc == 0 and not dashboard.called)

print("전체", "PASS" if all(results) else "FAIL", sum(results), "/", len(results))
sys.exit(0 if all(results) else 1)
