"""scripts/_mon_spike.py의 순수 함수 단위 검증(M46). 외부 호출 없음. 실행: python3 scripts/test_monitoring_spike.py"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _mon_spike as ms  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


# 로그 줄: 포매터(M44)와 같은 모양
d = json.loads(ms.snapshot_line(1760000000.123, "worker", queue_ready=1, dlq=0))
check("스냅샷 줄: v·ts(Z, 밀리초)·role·event·fields", d["v"] == 1 and d["ts"].endswith("Z") and d["ts"][19] == "." and d["role"] == "worker"
      and d["event"] == "backlog_snapshot" and d["fields"] == {"queue_ready": 1, "dlq": 0})
check("부분 스냅샷은 빠진 키를 fields에 넣지 않음(0 치환 없음)", "recovery" not in json.loads(ms.snapshot_line(1.0, queue_ready=1, dlq=0))["fields"])
check("관측 실패 줄 2종: 키 없음/실패 사유", json.loads(ms.unobserved_line(1.0))["msg"] == "적체 스냅샷"
      and json.loads(ms.unobserved_line(1.0, keyless=False))["fields"] == {"reason": "ConnectException"})
check("LLM 실패 줄 사건 키", json.loads(ms.llm_failure_line(1.0))["event"] == "llm_final_failure")

# 평가 횟수 계산
S = lambda *xs: [(e, s, "ok") for e, s in xs]  # noqa: E731
check("Pending→Firing 평가 수(같은 평가를 여러 번 본 폴링은 1회)",
      ms.count_evaluations(S(("e1", "inactive"), ("e2", "pending"), ("e2", "pending"), ("e3", "pending"), ("e4", "pending"), ("e5", "firing"))) == 4)
check("Firing이 없으면 None", ms.count_evaluations(S(("e1", "pending"), ("e2", "pending"))) is None)
check("Pending이 없으면 None", ms.count_evaluations(S(("e1", "inactive"))) is None)
check("for 0s: 첫 평가에서 바로 Firing이면 1", ms.count_evaluations(S(("e1", "firing")), "firing", "firing") == 1)

# 격자 이탈
check("정확한 10초 격자는 이탈 0", ms.drift_from_grid([100.0, 110.0, 120.0, 130.0]) == 0)
check("누적 이탈은 첫 시각 기준", abs(ms.drift_from_grid([100.0, 110.5, 121.0, 131.5]) - 1.5) < 1e-9)

# H 계산
check("H_sample = for + 10 + D_s − D_e + 여유", ms.h_sample(60, 35, 0, 15) == 120)
check("H = max(모형, 표본)을 5초 올림", ms.h_final(120, 85) == 120 and ms.h_final(120, 121) == 125)
check("H 상한 150 초과는 None(멈춤)", ms.h_final(120, 151) is None and ms.h_final(120, 150) == 150)
check("반례: 보정 표본이 유리해도 H는 모형 하한 아래로 내려가지 않음", ms.h_final(120, ms.h_sample(60, 10, 10, 15)) == 120)

# 레이블·벡터
check("레이블 집합 {app, role, level}(내부 레이블 제외)", ms.label_set_ok(["app", "role", "level", "__stream_shard__"]))
check("service_name이 섞이면 거부", not ms.label_set_ok(["app", "role", "level", "service_name"]))
vec = ms.parse_vector({"data": {"result": [{"metric": {"role": "all"}, "value": [1, "2"]}]}})
check("벡터 파싱", vec == {"all": 2.0} and ms.parse_vector({"data": {"result": []}}) == {})
check("RFC3339 파싱(Z·오프셋·소수)", ms.parse_rfc3339("2026-10-15T12:00:00.5Z") == ms.parse_rfc3339("2026-10-15T21:00:00.5+09:00"))

# 스텁
port = 19391
st = ms.MetricsStub(port, "t" * 8)
st.start()


def get(auth, path="/actuator/prometheus"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return 0


check("지표 스텁: 올바른 Bearer 200·틀리면 401·무인증 401", get("Bearer " + "t" * 8) == 200 and get("Bearer x") == 401 and get(None) == 401)
st.stop()
check("지표 스텁 정지 = 연결 즉시 거부", get("Bearer " + "t" * 8) == 0)

hp = 19392
hk = ms.WebhookStub(hp)
hk.start()
body = json.dumps({"status": "firing", "alerts": [{"labels": {"slack_lab_rule": "recovery_needed", "role": "all"}}]}).encode()
req = urllib.request.Request(f"http://127.0.0.1:{hp}/alerts/grafana", data=body, method="POST", headers={"X-Alert-Secret": "s", "Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=3).read()
got = hk.snapshot()
check("웹훅 스텁: 헤더·경로·규칙·상태 기록", len(got) == 1 and got[0]["headers"].get("X-Alert-Secret") == "s" and got[0]["path"] == "/alerts/grafana"
      and ms.WebhookStub.rule_of(got[0]) == "recovery_needed" and ms.WebhookStub.status_of(got[0]) == "firing")
hk.stop()

print("전체", "PASS" if all(results) else "FAIL", sum(results), "/", len(results))
sys.exit(0 if all(results) else 1)
