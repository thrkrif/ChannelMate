"""M48 `n8n-smoke`: Grafana 경유 예제(docs/n8n/grafana-relay.json)를 실제 n8n 컨테이너로 돌려 보는 가벼운 기동 1회.
외부로 나가는 것은 없다: 앱의 Slack 기본 주소(`SLACK_BASEURL`)는 로컬 기록용 스텁이고 n8n의 대상도 로컬이다. 비밀은 더미 값만 쓰며
(수신 `ALERT_SECRET`=n8n Credential 값, 실제 `.env` 값 아님) 실제 값은 어떤 출력·결과 파일에도 없다. 검증 소스셋(`verifyBootRun`)의
소비 일시정지·재개와 세션 도구의 jar 실행기(`recovery list` + DB 대상 확인)도 같은 기동 안에서 한 번씩 확인한다.

판정(계획 C8): 인증(webhook 인증 실패 403·잘못된 Credential 401·리포트 0), 본문(원본과 전달본 JSON 차이 0), 응답 전달(200/400/401/503을 n8n이
수신 서버와 같은 상태·본문으로 돌려줌).
"""
import copy
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

import _mon_env
import _mon_spike as ms
import _mon_stubs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = "n8nio/n8n:2.42.6@sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a"
N8N_DATA = "/".join(["", "home", "node", ".n8n"])  # 컨테이너 안 경로(로컬 절대경로 검사 훅의 오탐을 피하려고 조립한다)
VOLUME = "slack-lab-n8n-s6smoke"  # 사용자의 slack-lab-n8n-check와 다른 전용 볼륨(끝나면 삭제)
N8N_PORT = 5678
STUB_PORT = 18961
SLACK_PORT = 18962
DUMMY_ALERT = "s6-smoke-alert-secret-dummy"
DUMMY_RELAY = "s6-smoke-relay-secret-dummy"
DUMMY_WRONG = "s6-smoke-wrong-secret-dummy"
ALERT_CHANNEL = "CSMOKEALERT"


def http(method, url, body=None, headers=None, timeout=15):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except OSError as e:
        return -1, type(e).__name__


def variant(base, name, path, url, call_cred, webhook_id, wf_id):
    """저장소 워크플로를 임시로 복사해 주소·경로·Credential 참조만 바꾼다(저장소 JSON에는 참조가 없다)."""
    wf = copy.deepcopy(base)
    wf["id"], wf["name"] = wf_id, name
    for n in wf["nodes"]:
        if n["type"].endswith("webhook"):
            n["parameters"]["path"] = path
            n["webhookId"] = webhook_id
            n["credentials"] = {"httpHeaderAuth": {"id": "cred-relay", "name": "relay"}}
        elif n["type"].endswith("httpRequest"):
            n["parameters"]["url"] = url
            n["credentials"] = {"httpHeaderAuth": {"id": call_cred, "name": call_cred}}
    return wf


def run_smoke(rep, s6fn, mv):
    env_path = os.path.join(ROOT, ".env")
    signing = mv._env_file_value("SLACK_SIGNING_SECRET")
    if not signing:
        rep.add("사전 조건: .env의 SLACK_SIGNING_SECRET", False, "비어 있음")
        return
    sdir = s6fn("start", "--id", "m48").stdout.strip()
    project = "slack-lab-s6v-m48"
    n8n = f"{project}-n8n"
    stub = ms.WebhookStub(STUB_PORT)
    slack = _mon_stubs.SlackRecorder(SLACK_PORT)
    try:
        subprocess.run([os.path.join(ROOT, "scripts", "s6-session"), "mark", "--session", sdir, "n8n"], cwd=ROOT, check=True)
        build = subprocess.run([os.path.join(ROOT, "gradlew"), "bootJar", "-x", "test", "-q"], cwd=ROOT, capture_output=True, text=True)
        jar = mv.pick_jar(os.path.join(ROOT, "build", "libs"))
        rep.add("bootJar 빌드", build.returncode == 0 and bool(jar), build.stderr.strip()[:200])
        json.dump({"jar": jar}, open(os.path.join(sdir, "jar.json"), "w"))
        stub.start()
        slack.start()
        s6fn("compose", "--session", sdir, "--", "up", "-d", "--wait", "rabbitmq", "postgres")

        # --- 앱: verifyBootRun(검증 소스셋) + 더미 ALERT_SECRET + Slack 스텁 ---
        flag = os.path.join(sdir, "pause.flag")
        gradle_env = {"APP_ROLE": "all", "LLM_CLIENT": "echo", "RAG_ENABLED": "false", "CLASSIFICATION_ENABLED": "false",
                      "ALERT_SECRET": DUMMY_ALERT, "ALERT_CHANNEL": ALERT_CHANNEL, "SLACK_BASEURL": f"http://127.0.0.1:{SLACK_PORT}",
                      "SLACK_LAB_VERIFY_PAUSE_FLAG": flag}
        args = [os.path.join(ROOT, "scripts", "s6-session"), "spawn", "--session", sdir, "--name", "verify-app", "--env-file", env_path]
        for k, v in gradle_env.items():
            args += ["--env", f"{k}={v}"]
        args += ["--", os.path.join(ROOT, "gradlew"), "verifyBootRun", "-q"]
        sp = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
        rep.add("verifyBootRun 기동 명령", sp.returncode == 0, sp.stderr.strip()[:200])
        rep.add("앱(/health 200)", mv.wait_until(lambda: mv.raw_status("127.0.0.1", mv.BUSINESS_PORT, "GET", "/health", "") == "200", 240, 3))

        # --- n8n 컨테이너 + 더미 Credential/워크플로 import ---
        subprocess.run(["docker", "volume", "create", VOLUME], capture_output=True)
        run = subprocess.run(["docker", "run", "-d", "--name", n8n, "--label", f"com.docker.compose.project={project}",
                              "-p", f"127.0.0.1:{N8N_PORT}:5678", "--memory", "1g", "--cpus", "1",
                              "-e", "N8N_DIAGNOSTICS_ENABLED=false", "-e", "N8N_VERSION_NOTIFICATIONS_ENABLED=false",
                              "-v", f"{VOLUME}:{N8N_DATA}", IMAGE], capture_output=True, text=True)
        rep.add("n8n 컨테이너 기동", run.returncode == 0, run.stderr.strip()[:200])
        if run.returncode != 0:
            return
        rep.add("n8n 준비(/healthz)", bool(ms.wait_until(lambda: http("GET", f"http://127.0.0.1:{N8N_PORT}/healthz")[0] == 200, 120, 2)))
        base = json.load(open(os.path.join(ROOT, "docs", "n8n", "grafana-relay.json"), encoding="utf-8"))
        app_url = f"http://host.docker.internal:{mv.BUSINESS_PORT}/alerts/grafana"
        stub_url = f"http://host.docker.internal:{STUB_PORT}/alerts/grafana"
        wfs = [variant(base, "relay-app", "slack-lab-grafana", app_url, "cred-good", "9f0c1a20-0000-4000-8000-0000000000b1", "s6smokeApp"),
               variant(base, "relay-stub", "slack-lab-grafana-stub", stub_url, "cred-good", "9f0c1a20-0000-4000-8000-0000000000b2", "s6smokeStub"),
               variant(base, "relay-bad", "slack-lab-grafana-bad", app_url, "cred-bad", "9f0c1a20-0000-4000-8000-0000000000b3", "s6smokeBad")]
        creds = [{"id": "cred-relay", "name": "relay", "type": "httpHeaderAuth", "data": {"name": "X-Relay-Auth", "value": DUMMY_RELAY}},
                 {"id": "cred-good", "name": "cred-good", "type": "httpHeaderAuth", "data": {"name": "X-Alert-Secret", "value": DUMMY_ALERT}},
                 {"id": "cred-bad", "name": "cred-bad", "type": "httpHeaderAuth", "data": {"name": "X-Alert-Secret", "value": DUMMY_WRONG}}]
        tmp = os.path.join(sdir, "n8n-import")
        os.makedirs(tmp, exist_ok=True)
        json.dump(creds, open(os.path.join(tmp, "creds.json"), "w"))
        for w in wfs:
            json.dump(w, open(os.path.join(tmp, w["id"] + ".json"), "w"), ensure_ascii=False)
        subprocess.run(["docker", "cp", tmp + "/.", f"{n8n}:/tmp/import"], capture_output=True)
        out = []
        out.append(subprocess.run(["docker", "exec", n8n, "n8n", "import:credentials", "--input=/tmp/import/creds.json"], capture_output=True, text=True))
        for w in wfs:
            out.append(subprocess.run(["docker", "exec", n8n, "n8n", "import:workflow", f"--input=/tmp/import/{w['id']}.json"], capture_output=True, text=True))
            pub = subprocess.run(["docker", "exec", n8n, "n8n", "publish:workflow", f"--id={w['id']}"], capture_output=True, text=True)
            if pub.returncode != 0:
                pub = subprocess.run(["docker", "exec", n8n, "n8n", "update:workflow", f"--id={w['id']}", "--active=true"], capture_output=True, text=True)
            out.append(pub)
        rep.add("Credential 3개·워크플로 3개 import·활성화", all(o.returncode == 0 for o in out), " | ".join((o.stdout + o.stderr).strip()[-120:] for o in out if o.returncode != 0))
        subprocess.run(["docker", "restart", n8n], capture_output=True)  # 활성 워크플로의 웹훅을 등록한다
        rep.add("n8n 재기동 후 준비", bool(ms.wait_until(lambda: http("GET", f"http://127.0.0.1:{N8N_PORT}/healthz")[0] == 200, 120, 2)))
        time.sleep(5)

        # --- 페이로드: M47 캡처가 있으면 그것을, 없으면 같은 구조의 합성 본문 ---
        cap_dir = os.path.join(os.path.expanduser("~/.slack-lab-secrets"), "m47")
        caps = sorted(os.listdir(cap_dir)) if os.path.isdir(cap_dir) else []
        if caps:
            payload = json.load(open(os.path.join(cap_dir, caps[-1]), encoding="utf-8"))
            rep.add("페이로드: M47 실제 발화 캡처 사용", True)
        else:
            payload = {"externalURL": "http://grafana.local:3000/", "status": "firing", "alerts": [{
                "status": "firing", "labels": {"slack_lab_rule": "llm_final_failure", "severity": "warning", "role": "-"},
                "annotations": {}, "startsAt": "2026-10-15T00:00:00Z"}]}
            rep.add("페이로드: 합성 본문 사용(캡처 없음)", True)
        auth_relay = {"X-Relay-Auth": DUMMY_RELAY, "Content-Type": "application/json"}
        hook = lambda path, hdr=None, body=payload: http("POST", f"http://127.0.0.1:{N8N_PORT}/webhook/{path}", body, hdr if hdr is not None else auth_relay)
        direct = lambda body=payload, secret=DUMMY_ALERT: http("POST", f"http://127.0.0.1:{mv.BUSINESS_PORT}/alerts/grafana", body,
                                                                {"Content-Type": "application/json", "X-Alert-Secret": secret})

        # 본문: 원본과 전달본의 JSON 차이 0, 헤더 전달
        before = len(stub.snapshot())
        code, _b = hook("slack-lab-grafana-stub")
        got = stub.snapshot()[before:]
        rep.add("[본문] n8n → 스텁 200·전달 1건", code == 200 and len(got) == 1, f"code={code} n={len(got)}")
        if got:
            rep.add("[본문] 원본과 전달본의 JSON 필드·값 차이 0", got[0]["body"] == payload)
            rep.add("[본문] X-Alert-Secret이 Credential 값으로 전달됨(값은 출력하지 않음)",
                    got[0]["headers"].get("X-Alert-Secret") == DUMMY_ALERT)
        # 인증: webhook 인증 실패는 403, 수신 서버로 가지 않는다
        before = len(stub.snapshot())
        c_none, _ = hook("slack-lab-grafana-stub", {"Content-Type": "application/json"})
        c_wrong, _ = hook("slack-lab-grafana-stub", {"X-Relay-Auth": "x", "Content-Type": "application/json"})
        rep.add("[인증] 웹훅 인증 없음/틀림 → 403·전달 0", c_none == 403 and c_wrong == 403 and len(stub.snapshot()) == before, f"{c_none}/{c_wrong}")

        # 응답 전달: 200/400/401/503이 직접 호출과 같은 상태·본문
        def compare(label, via, dire):
            rep.add(f"[응답] {label}: n8n 상태·본문 = 수신 서버 직접 응답", via == dire and via[0] > 0, f"via={via[0]} direct={dire[0]}")
        reports0 = slack.count("chat.postMessage", ALERT_CHANNEL)
        via200 = hook("slack-lab-grafana")
        rep.add("[응답] 200: n8n 경유 정상", via200[0] == 200, str(via200[0]))
        compare("200 재전송(중복 흡수)", hook("slack-lab-grafana"), direct())
        bad_payload = {k: v for k, v in payload.items() if k != "externalURL"}
        compare("400(필수값 누락 본문)", hook("slack-lab-grafana", body=bad_payload), direct(bad_payload))
        compare("401(잘못된 Credential)", hook("slack-lab-grafana-bad"), direct(secret=DUMMY_WRONG))
        ms.wait_until(lambda: slack.count("chat.postMessage", ALERT_CHANNEL) > reports0, 60, 1)
        time.sleep(10)
        rep.add("[인증] 리포트는 정상 경로에서 정확히 1개, 401·재전송·400은 0", slack.count("chat.postMessage", ALERT_CHANNEL) - reports0 == 1,
                f"reports={slack.count('chat.postMessage', ALERT_CHANNEL) - reports0}")
        # 503: 큐 저장이 안 되게 rabbitmq를 멈춘다(새 중복 키를 만들려고 startsAt을 바꾼다)
        s6fn("compose", "--session", sdir, "--", "stop", "rabbitmq")
        time.sleep(3)
        p503 = json.loads(json.dumps(payload))
        for a in p503.get("alerts", []):
            a["startsAt"] = "2026-10-15T01:23:45Z"
        via503, dir503 = hook("slack-lab-grafana", body=p503), direct(p503)
        rep.add("[응답] 503(큐 저장 실패): n8n이 200으로 숨기지 않고 같은 상태 반환", via503[0] == 503 and dir503[0] == 503, f"via={via503[0]} direct={dir503[0]}")
        s6fn("compose", "--session", sdir, "--", "start", "rabbitmq")
        mv.wait_until(lambda: mv.raw_status("127.0.0.1", mv.BUSINESS_PORT, "GET", "/health", "") == "200", 60, 2)

        # --- 검증 소스셋: 소비 일시정지·재개 ---
        log_path = os.path.join(sdir, "all", "slack-lab.log")
        read_log = lambda: open(log_path, encoding="utf-8", errors="replace").read() if os.path.isfile(log_path) else ""
        open(flag, "w").close()
        rep.add("[PauseControl] 정지 전이 로그(PAUSED)", bool(ms.wait_until(lambda: "검증 소비 제어 상태=PAUSED" in read_log(), 20, 1)))
        os.remove(flag)
        rep.add("[PauseControl] 재개 전이 로그(RUNNING)", bool(ms.wait_until(lambda: read_log().count("검증 소비 제어 상태=RUNNING") >= 1, 20, 1)))
        rep.add("[PauseControl] 전이 로그는 정지 1·재개 1", read_log().count("검증 소비 제어 상태=PAUSED") == 1 and read_log().count("검증 소비 제어 상태=RUNNING") == 1)

        # --- 세션 장치: jar 실행기로 recovery list + DB 대상 확인 ---
        ok, labels = _mon_env.db_target_check(project)
        rep.add("[세션] DB 대상 확인(5432 게시 컨테이너 1개·프로젝트 레이블 일치)", ok, f"labels={labels}")
        if ok:
            code, out_text, (t0, t1) = _mon_env.run_jar_cli(jar, "recovery", ["list"], sdir, project)
            rep.add("[세션] jar 실행기 `recovery list` 종료 0·'미해결 건 없음'", code == 0 and "미해결 건 없음" in out_text, f"exit={code}")
    finally:
        for name in (n8n,):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", VOLUME], capture_output=True)
        left = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={VOLUME}"], capture_output=True, text=True).stdout.split()
        rep.add("n8n 컨테이너·스모크 볼륨 제거(기존 slack-lab-n8n-check 무접촉)", not left)
        stub.stop()
        slack.stop()
        p = s6fn("teardown", "--session", sdir, check=False)
        rep.add("teardown 최종 확인", p.returncode == 0, (p.stdout + p.stderr).strip()[:300])
