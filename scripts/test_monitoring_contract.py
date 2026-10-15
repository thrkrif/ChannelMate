"""모니터링 스택 계약 검사(6단계 M46, 합격선 C4). 데몬·네트워크 없이 파일과 `docker compose config`(메모리 파싱, 값 출력 없음)만 본다.
실행: python3 scripts/test_monitoring_contract.py   (실패하면 종료 코드 1)

YAML 라이브러리를 가정하지 않는다: Loki·Prometheus·Grafana 프로비저닝 파일은 JSON 문법으로 쓴 YAML이라 `json`으로 읽고,
compose는 `docker compose config --format json`으로 읽는다.
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INFRA = os.path.join(ROOT, "infra", "monitoring")
ALERT = os.path.join(INFRA, "grafana", "provisioning", "alerting")
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


def load(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return json.load(f)


def text(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def compose_config(env_extra, files=("compose.yaml", "compose.monitoring.yaml"), profiles=("app", "monitoring")):
    cmd = ["docker", "compose", "-p", "slack-lab-s6v-contract"]
    for f in files:
        cmd += ["-f", f]
    for p in profiles:
        cmd += ["--profile", p]
    cmd += ["config", "--format", "json"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GRAFANA_", "POSTGRES_", "SLACK_LAB_", "MANAGEMENT_"))}
    env.update(env_extra)
    # 저장소의 .env를 읽지 않게 빈 env 파일을 지정한다(값이 출력·시험에 섞이지 않는다)
    cmd[2:2] = ["--env-file", os.devnull]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


FIXTURE = {"POSTGRES_PASSWORD": "x", "GRAFANA_ADMIN_PASSWORD": "x"}

# ---------------------------------------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------------------------------------
rc, out, err = compose_config(FIXTURE)
check("compose 두 파일 + 프로필 config 성공", rc == 0, err[-200:])
cfg = json.loads(out) if rc == 0 else {"services": {}, "networks": {}}
svc = cfg["services"]
MON = ("alloy", "loki", "prometheus", "grafana")

# 이미지: 정식 패치 태그 + 다중 아키텍처 digest, latest 0
for name in MON:
    image = svc.get(name, {}).get("image", "")
    check(f"{name} 이미지 = 패치 태그@sha256 (latest 아님)", re.search(r":v?\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$", image) is not None and ":latest" not in image, image)
raw_compose = text("compose.monitoring.yaml")
check("compose.monitoring.yaml에 latest 태그 0", ":latest" not in raw_compose)

# 메모리 상한
MIB = 1024 * 1024
want_mem = {"alloy": 256, "loki": 1024, "prometheus": 512, "grafana": 384}
check("mem_limit 256/1024/512/384MiB", all(int(svc.get(n, {}).get("mem_limit", 0)) == want_mem[n] * MIB for n in MON),
      str({n: svc.get(n, {}).get("mem_limit") for n in MON}))

# 보존
prom_cmd = " ".join(svc.get("prometheus", {}).get("command", []))
check("Prometheus 보존 7일 또는 2GiB", "--storage.tsdb.retention.time=7d" in prom_cmd and "--storage.tsdb.retention.size=2GB" in prom_cmd)
loki = load("infra", "monitoring", "loki", "loki.yaml")
check("Loki 보존 168시간·compactor 삭제 켬", loki["limits_config"]["retention_period"] == "168h" and loki["compactor"]["retention_enabled"] is True)
check("Loki 서비스 이름 레이블 자동 생성 끔(레이블 = app·role·level만)", loki["limits_config"].get("discover_service_name") == [])

# 볼륨·포트·인증
check("전용 볼륨 4개", sorted(k for k in cfg.get("volumes", {}) if k.startswith("mon-")) == ["mon-alloy", "mon-grafana", "mon-loki", "mon-prometheus"])
pubs = {n: svc.get(n, {}).get("ports", []) for n in MON}
g_ports = pubs["grafana"]
check("Grafana만 127.0.0.1:3000 게시", len(g_ports) == 1 and g_ports[0].get("host_ip") == "127.0.0.1" and str(g_ports[0].get("published")) == "3000"
      and all(not pubs[n] for n in ("alloy", "loki", "prometheus")), str(pubs))
genv = svc.get("grafana", {}).get("environment", {})
check("Grafana 익명·가입 끔", genv.get("GF_AUTH_ANONYMOUS_ENABLED") == "false" and genv.get("GF_USERS_ALLOW_SIGN_UP") == "false")
rc_no, _, err_no = compose_config({"POSTGRES_PASSWORD": "x"})
check("GRAFANA_ADMIN_PASSWORD 없으면 모니터링 config 실패(:?)", rc_no != 0 and "GRAFANA_ADMIN_PASSWORD" in err_no, err_no[-160:])
rc_base, _, err_base = compose_config({"POSTGRES_PASSWORD": "x"}, files=("compose.yaml",), profiles=("app",))
check("기본 스택(compose.yaml 단독)은 GRAFANA 변수 없이 config 성공", rc_base == 0, err_base[-160:])
check("compose.yaml에 include 0", not re.search(r"^include:", text("compose.yaml"), re.M))
check(".dockerignore ∋ .runtime", ".runtime" in text(".dockerignore").split())

# 네트워크 소속표
nets = {n: sorted(svc.get(n, {}).get("networks", {}).keys()) for n in list(MON) + ["rabbitmq", "postgres", "receiver", "worker", "reactor"]}
table = {"rabbitmq": ["default"], "postgres": ["default"], "receiver": ["alerts", "default", "scrape"], "worker": ["default", "scrape"],
         "reactor": ["default", "scrape"], "prometheus": ["monitoring", "scrape"], "grafana": ["alerts", "monitoring"], "loki": ["monitoring"],
         "alloy": ["monitoring"]}
check("서비스×네트워크 소속표 일치", nets == table, str(nets))
check("Prometheus ∉ default·alerts, Grafana ∉ scrape, app 3역할 ∋ default",
      "default" not in nets["prometheus"] and "alerts" not in nets["prometheus"] and "scrape" not in nets["grafana"]
      and all("default" in nets[r] for r in ("receiver", "worker", "reactor")))
check("monitoring 네트워크는 internal", cfg.get("networks", {}).get("monitoring", {}).get("internal") is True)
check("scrape·alerts는 internal 아님", not cfg["networks"].get("scrape", {}).get("internal") and not cfg["networks"].get("alerts", {}).get("internal"))

# 마운트
def mounts(name):
    return svc.get(name, {}).get("volumes", [])


check("docker.sock 마운트 0", all("docker.sock" not in json.dumps(mounts(n)) for n in MON))
alloy_log = [m for m in mounts("alloy") if m.get("target") == "/var/log/slack-lab"]
check("Alloy 로그 마운트는 읽기 전용", len(alloy_log) == 1 and alloy_log[0].get("read_only") is True, str(alloy_log))
check("Prometheus 토큰은 파일 마운트(읽기 전용)이고 설정 파일에 값이 없음",
      any(m.get("target") == "/run/secrets/metrics_token" and m.get("read_only") for m in mounts("prometheus"))
      and "credentials_file" in text("infra", "monitoring", "prometheus", "prometheus.yml") and "credentials\"" not in text("infra", "monitoring", "prometheus", "prometheus.yml"))
check("앱 서비스에 Loki 주소·push 설정 0", all("loki" not in json.dumps(svc.get(r, {}).get("environment", {})).lower() for r in ("receiver", "worker", "reactor")))

# Prometheus 설정
prom = load("infra", "monitoring", "prometheus", "prometheus.yml")
job = prom["scrape_configs"][0]
check("follow_redirects false·scrape_timeout 5s·간격 10s·file_sd refresh 10s",
      job["follow_redirects"] is False and job["scrape_timeout"] == "5s" and job["scrape_interval"] == "10s"
      and job["file_sd_configs"][0]["refresh_interval"] == "10s", str(job)[:200])
check("Prometheus 경로 /actuator/prometheus·Bearer 파일", job["metrics_path"] == "/actuator/prometheus" and job["authorization"]["type"] == "Bearer")

# Alloy
alloy = text("infra", "monitoring", "alloy", "config.alloy")
paths = re.findall(r'"__path__"\s*=\s*"([^"]+)"', alloy)
check("Alloy 정확 경로 4개(글롭 없음)", sorted(paths) == sorted(f"/var/log/slack-lab/{r}/slack-lab.log" for r in ("receiver", "worker", "reactor", "all")), str(paths))
check("Alloy tail_from_end·stage.timestamp·filename 레이블 제거", "tail_from_end = true" in alloy and "stage.timestamp" in alloy
      and 'stage.label_drop' in alloy and '"filename"' in alloy)

# ---------------------------------------------------------------------------------------------------------
# 규칙·정책·연락 지점
# ---------------------------------------------------------------------------------------------------------
host = load("infra", "monitoring", "grafana", "provisioning", "alerting", "rules-host.json")
app = load("infra", "monitoring", "grafana", "provisioning", "alerting", "rules-app.json")
hr = host["groups"][0]["rules"]
ar = app["groups"][0]["rules"]
check("규칙 4개·UID 집합 동일(host/app)", [r["uid"] for r in hr] == [r["uid"] for r in ar] == [f"slack-lab-r{i}" for i in range(1, 5)])
TIME_KEYS = ("for", "interval", "group_wait", "group_interval", "repeat_interval")


def time_values(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in TIME_KEYS:
                out.append((k, v))
            time_values(v, out)
    elif isinstance(obj, list):
        for v in obj:
            time_values(v, out)
    return out


check("두 규칙 파일의 시간 값 동일, 평가 10초", time_values(host, []) == time_values(app, []) and host["groups"][0]["interval"] == "10s")
only4 = [i for i in range(4) if json.dumps(hr[i], sort_keys=True) != json.dumps(ar[i], sort_keys=True)]
check("host/app은 ④의 식 한 줄만 다름", only4 == [3] and [d for d in hr[3]["data"] if d["refId"] != "A"] == [d for d in ar[3]["data"] if d["refId"] != "A"])
fors = {r["uid"]: r["for"] for r in ar}
check("지속 시간 ①30s ②0s ③2m ④1m", fors == {"slack-lab-r1": "30s", "slack-lab-r2": "0s", "slack-lab-r3": "2m", "slack-lab-r4": "1m"}, str(fors))
check("규칙 파일·정책 시간 키에 $ 0", all("$" not in str(v) for _k, v in time_values(host, []) + time_values(app, []) + time_values(load("infra", "monitoring", "grafana", "provisioning", "alerting", "policies.json"), [])))
check("NoData=NoData·실행 오류=Error 상태", all(r["noDataState"] == "NoData" and r["execErrState"] == "Error" for r in ar))

# 질의 형태
def query_models(rule):
    return [d for d in rule["data"] if d["datasourceUid"] in ("slack-lab-loki", "slack-lab-prom")]


loki_ok = all(d["model"]["queryType"] == "instant" for r in ar for d in query_models(r) if d["datasourceUid"] == "slack-lab-loki")
prom_ok = all(d["model"].get("instant") is True and d["model"].get("range") is False for r in ar for d in query_models(r) if d["datasourceUid"] == "slack-lab-prom")
reduce_ok = all(d["model"].get("reducer") == "last" for r in ar for d in r["data"] if d["model"].get("type") == "reduce")
check("질의 queryType instant·Reduce last", loki_ok and prom_ok and reduce_ok)
check("조건 줄: ①E ②C ③C ④C", [r["condition"] for r in ar] == ["E", "C", "C", "C"])

# 식 구조 검사(형태 식별)
INVALID = 'unless on(role) (sum by (role) (count_over_time('
SEL = '{app="slack-lab",role=~"worker|all"}'


def shape(expr):
    """승인된 형태 ID를 돌려준다. 식별 못 하면 None."""
    if re.fullmatch(r'max by \(role\) \(\(1 - up\{[^}]*\}\) or absent\(.*\)\)', expr) or re.fullmatch(r'max by \(role\) \(\(up\{[^}]*\} == bool 0\) or absent\(.*\)\)', expr):
        return "P-max"
    if expr.startswith("(sum(count_over_time(") and "vector(0)" in expr and "and on()" in expr:
        return "L-count"
    if expr.startswith("sum(count_over_time(") and " - sum(count_over_time(" in expr:
        return "L-count"
    if expr.startswith("max by (role) (last_over_time(") and "unwrap" in expr:
        return "L-outer"
    if expr.startswith("(") and "last_over_time(" in expr and "unwrap" in expr and "bool 1" in expr:
        return "L-inner"
    if expr.startswith("last_over_time(") and re.search(r"unwrap \w+ \[30s\]\) by \(role\) unless on\(role\)", expr):
        return "L-inner"
    return None


def common_ok(expr, kind):
    if re.search(r"\|\s*json\s*(\||\[|\)|$)", expr):
        return False  # bare | json
    for m in re.finditer(r"\|\s*json\s+([^|]+)", expr):
        if "=" not in m.group(1):
            return False
    if re.search(r"unwrap \w+\s*\|\s*__error__", expr) or "__error__" in expr:
        return False
    if kind in ("L-inner", "L-outer"):
        # 관측 실패 줄 무효화 절 + 부분 스냅샷 무효화 절(스파이크 SP-5 발견: 빠진 키의 unwrap은 Error가 아니라 줄을 조용히 건너뛰어 직전 값을 돌려준다)
        key = re.search(r"unwrap (\w+) \[30s\]", expr)
        partial = key and re.search(r'unless on\(role\) \(sum by \(role\) \(count_over_time\([^)]*\|= "backlog_snapshot" \| json event="event", %s="fields\.%s" \| event="backlog_snapshot" \| %s="" \[30s\]\)\) > 0\)' % ((key.group(1),) * 3), expr)
        return INVALID in expr and expr.count("backlog_unobserved") >= 2 and bool(partial)
    return True


exprs = {r["uid"]: next(d["model"]["expr"] for d in r["data"] if d["refId"] == "A") for r in ar}
shapes = {u: shape(e) for u, e in exprs.items()}
check("규칙 식 형태 식별(①L-inner ②L-count ③L-inner ④P-max)", shapes == {"slack-lab-r1": "L-inner", "slack-lab-r2": "L-count", "slack-lab-r3": "L-inner", "slack-lab-r4": "P-max"}, str(shapes))
check("공통 식 검사(bare json·__error__ 금지, 적체 질의에 무효화 절)", all(common_ok(exprs[u], shapes[u]) for u in exprs))
r1b = next(d["model"]["expr"] for d in ar[0]["data"] if d["refId"] == "B")
check("① 질의 B(recovery)도 같은 형태·무효화 절", shape(r1b) == "L-inner" and common_ok(r1b, "L-inner"))
# 형태별 픽스처(통과 1·실패 1 이상)
PARTIAL_Q = ' unless on(role) (sum by (role) (count_over_time({app="slack-lab"} |= "backlog_snapshot" | json event="event", queue_ready="fields.queue_ready" | event="backlog_snapshot" | queue_ready="" [30s])) > 0)'
good_outer = 'max by (role) (last_over_time({app="slack-lab"} |= "backlog_snapshot" | json event="event", queue_ready="fields.queue_ready" | event="backlog_snapshot" | unwrap queue_ready [30s])) unless on(role) (sum by (role) (count_over_time({app="slack-lab"} |= "backlog_unobserved" | json event="event" | event="backlog_unobserved" [30s])) > 0)'
good_outer = good_outer + PARTIAL_Q
bad_outer = good_outer.replace('json event="event", queue_ready="fields.queue_ready"', "json")
check("형태 L-outer 픽스처: 통과 1·bare json 실패 1", shape(good_outer) == "L-outer" and common_ok(good_outer, "L-outer") and not common_ok(bad_outer, "L-outer"))
bad_inner = "max by (role) (" + exprs["slack-lab-r3"] + ")"
no_partial = exprs["slack-lab-r3"].split(" unless on(role) (sum by (role) (count_over_time({app=\"slack-lab\",role=~\"worker|all\"} |= \"backlog_snapshot\"")[0]
check("부분 스냅샷 무효화 절이 빠진 식은 공통 검사 실패", shape(no_partial) == "L-inner" and not common_ok(no_partial, "L-inner"))
check("형태 L-inner 픽스처: 바깥 max by가 붙은 식은 L-inner가 아님", shape(exprs["slack-lab-r3"]) == "L-inner" and shape(bad_inner) != "L-inner")
check("형태 식별 불가 식은 None(실패)", shape("up == 0") is None)
r4_alt = 'max by (role) ((up{job="slack-lab"} == bool 0) or absent(up{job="slack-lab",role="receiver"}) or absent(up{job="slack-lab",role="worker"}) or absent(up{job="slack-lab",role="reactor"}))'
check("형태 P-max 대안식 식별", shape(r4_alt) == "P-max")
check("④ app 식은 역할 3개 absent, host 식은 role=all 하나",
      all(f'absent(up{{job="slack-lab",role="{r}"}})' in exprs["slack-lab-r4"] for r in ("receiver", "worker", "reactor"))
      and 'role="all"' in next(d["model"]["expr"] for d in hr[3]["data"] if d["refId"] == "A") and "receiver" not in next(d["model"]["expr"] for d in hr[3]["data"] if d["refId"] == "A"))

# 정책·연락 지점
pol = load("infra", "monitoring", "grafana", "provisioning", "alerting", "policies.json")["policies"][0]
route = pol["routes"][0]
check("정책 group_by [slack_lab_rule, role]·group_wait 5s·group_interval 1m·repeat_interval 1h",
      pol["group_by"] == ["slack_lab_rule", "role"] and pol["group_wait"] == "5s" and pol["group_interval"] == "1m" and pol["repeat_interval"] == "1h"
      and route["group_by"] == ["slack_lab_rule", "role"] and route["group_wait"] == "5s" and route["group_interval"] == "1m" and route["repeat_interval"] == "1h")
check("웹훅 경로 매처 slack_lab_rule=~.+ 이고 NoData·Error 제외", ["slack_lab_rule", "=~", ".+"] in route["object_matchers"]
      and ["alertname", "!~", "DatasourceNoData|DatasourceError"] in route["object_matchers"] and route["receiver"] == "slack-lab-webhook")
cps = load("infra", "monitoring", "grafana", "provisioning", "alerting", "contact-points.json")["contactPoints"]
hook = next(c for c in cps if c["name"] == "slack-lab-webhook")["receivers"][0]
check("연락 지점: 웹훅 URL은 환경 변수, 사용자 헤더 X-Alert-Secret은 환경 변수 참조(리터럴 비밀 없음)",
      hook["type"] == "webhook" and hook["settings"]["url"] == "${GRAFANA_ALERT_WEBHOOK_URL}" and hook["settings"]["headers"] == {"X-Alert-Secret": "$__env{ALERT_SECRET}"})
check("기본 정책 수신자는 기본 연락 지점(slack-lab-null)이며 실재", pol["receiver"] in [c["name"] for c in cps])

# 데이터 소스·대시보드
ds = load("infra", "monitoring", "grafana", "provisioning", "datasources", "datasources.yaml")["datasources"]
check("데이터 소스 UID 고정(slack-lab-loki·slack-lab-prom)", sorted(d["uid"] for d in ds) == ["slack-lab-loki", "slack-lab-prom"])
dash_files = [f for f in os.listdir(os.path.join(INFRA, "grafana", "dashboards")) if f.endswith(".json")]
dash = load("infra", "monitoring", "grafana", "dashboards", "slack-lab.json")
check("대시보드 1개·패널 8개", dash_files == ["slack-lab.json"] and len(dash["panels"]) == 8)
backlog_targets = [t for t in dash["panels"][0]["targets"]]
check("패널 1(적체) 질의 6개 모두 backlog_unobserved 무효화 절 포함", len(backlog_targets) == 6 and all(INVALID in t["expr"] and "backlog_unobserved" in t["expr"] for t in backlog_targets))
all_exprs = [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]
check("대시보드 질의에 bare `| json` 0·`__error__` 0", all(not re.search(r"\|\s*json\s*(\||\[|\)|$)", e) and "__error__" not in e for e in all_exprs))
need = ['kind="answer"', 'gen="0"', 'manual_run!="true"', 'result="Delivered"']
p2, p4 = dash["panels"][1]["targets"][0]["expr"], dash["panels"][3]["targets"][0]["expr"]
check("패널 2·4(큐 대기·답변)는 첫 정상 답변 필터(p1-metrics 대응표)", all(n in p2 and n in p4 for n in need) and "unwrap queue_wait_ms" in p2 and "unwrap answer_ms" in p4)
p3 = dash["panels"][2]["targets"][0]["expr"]
check("패널 3(LLM 시도)은 llm_ms >= 0인 모든 시도", "llm_ms >= 0" in p3 and "unwrap llm_ms" in p3 and 'kind="answer"' not in p3)
p5 = dash["panels"][4]["targets"][0]["expr"]
check("패널 5는 시도 단위 결과×종류 분포", "sum by (result, kind)" in p5 and "processing_metric" in p5)
check("패널 6은 rag_result 분포", "sum by (result)" in dash["panels"][5]["targets"][0]["expr"] and "rag_result" in dash["panels"][5]["targets"][0]["expr"])
check("패널 7은 up, 패널 8은 알람 목록", dash["panels"][6]["targets"][0]["expr"] == 'up{job="slack-lab"}' and dash["panels"][7]["type"] == "alertlist")

# scripts/monitoring 계약
mon = text("scripts", "monitoring")
check("scripts/monitoring: 기본 스택 down -v 0·볼륨은 mon-* 4개만 지움", "down -v" not in mon and '"down"' not in mon and "--volumes" not in mon
      and 'VOLUMES = ("mon-alloy", "mon-loki", "mon-prometheus", "mon-grafana")' in mon, "")
check("scripts/monitoring: -f 쌍·프로필 고정", 'FILES = ("compose.yaml", "compose.monitoring.yaml")' in mon and '"--profile", "monitoring"' in mon)

print("전체", "PASS" if all(results) else "FAIL", sum(results), "/", len(results))
sys.exit(0 if all(results) else 1)
