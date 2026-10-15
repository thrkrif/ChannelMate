"""M47 `replay`: 규칙 ②(LLM 최종 실패)를 실제로 발화시켜 Grafana 웹훅 페이로드를 캡처하고(I-9), 캡처한 본문을 앱의 `/alerts/grafana`에
최초 + 재전송 3회 보내 리포트가 정확히 1개인지 본다. 중간 기동 1회.

외부로 나가는 것은 없다: 앱의 Slack 기본 주소(`SLACK_BASEURL`)를 이 도구의 로컬 스텁으로 돌리므로 실제 Slack 채널에는 아무것도 가지 않는다.
실패 유도는 계획의 F-echo(OpenAI 호환 클라이언트 + 닫힌 포트, 모델 확인 끔)다 — echo 그대로 주소만 닫으면 실패가 나지 않는다.
비밀(서명 비밀·알람 시크릿)은 `.env`에서 메모리로만 읽고 출력하지 않는다. 캡처 본문은 저장소 밖 600 파일에만 둔다.
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _mon_spike as ms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEBHOOK_PORT = 18951
SLACK_PORT = 18952


class SlackStub:
    """chat.postMessage 등 모든 POST를 받아 본문을 기록하고 성공으로 답한다."""

    def __init__(self, port):
        self.port = port
        self.posts = []
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                if self.path.endswith("chat.postMessage"):
                    try:
                        body = json.loads(raw)
                    except ValueError:
                        body = {}
                    with outer.lock:
                        outer.posts.append(body)
                out = b'{"ok":true,"ts":"1.1"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return list(self.posts)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return -1


def signed_mention(signing_secret, channel):
    body = json.dumps({"type": "event_callback", "event_id": "EvM47Capture", "authorizations": [{"user_id": "UBOT"}],
                       "event": {"type": "app_mention", "channel": channel, "user": "U1", "text": "<@UBOT> 메모리 누수 원인이 뭐야",
                                 "ts": "300.1"}}, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(signing_secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    return body, {"Content-Type": "application/json", "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig}


def run_replay(rep, s6fn, mv):
    env_path = os.path.join(ROOT, ".env")
    token = mv.read_token(env_path)
    gpw = mv._env_file_value("GRAFANA_ADMIN_PASSWORD")
    signing = mv._env_file_value("SLACK_SIGNING_SECRET")
    alert_secret = mv._env_file_value("ALERT_SECRET")
    alert_channel = mv._env_file_value("ALERT_CHANNEL")
    missing = [k for k, v in (("MANAGEMENT_METRICS_TOKEN", token), ("GRAFANA_ADMIN_PASSWORD", gpw), ("SLACK_SIGNING_SECRET", signing),
                              ("ALERT_SECRET", alert_secret), ("ALERT_CHANNEL", alert_channel)) if not v]
    if missing:
        rep.add("사전 조건: .env 값", False, "비어 있는 키: " + ",".join(missing))
        return
    sdir = s6fn("start", "--id", "m47-replay").stdout.strip()
    hook = ms.WebhookStub(WEBHOOK_PORT)
    slack = None
    try:
        hook.start()
        slack = SlackStub(SLACK_PORT)
        build = subprocess.run([os.path.join(ROOT, "gradlew"), "bootJar", "-x", "test", "-q"], cwd=ROOT, capture_output=True, text=True)
        jar = mv.pick_jar(os.path.join(ROOT, "build", "libs"))
        rep.add("bootJar 빌드", build.returncode == 0 and bool(jar), build.stderr.strip()[:200])
        if not jar:
            return
        s6fn("compose", "--session", sdir, "--", "up", "-d", "--wait", "rabbitmq", "postgres")
        # F-echo: OpenAI 호환 클라이언트 + 닫힌 포트(모델 확인 끔) — 실패 단계, 모델 프로세스 0. Slack 호출은 로컬 스텁으로 돌린다.
        mv.spawn_app(sdir, jar, [env_path], {"APP_ROLE": "all", "LLM_CLIENT": "ollama", "LLM_BASE_URL": "http://localhost:9/v1",
                                             "LLM_VERIFYMODELONSTARTUP": "false", "CLASSIFICATION_ENABLED": "false",
                                             "RAG_ENABLED": "false", "SLACK_BASEURL": f"http://127.0.0.1:{SLACK_PORT}"}, "app")
        rep.add("앱(F-echo, 역할 all) /health 200", mv.host_health())
        env = dict(os.environ, S6_SESSION=sdir, SLACK_LAB_LOG_DIR_HOST=sdir,
                   GRAFANA_ALERT_WEBHOOK_URL=f"http://host.docker.internal:{WEBHOOK_PORT}/alerts/grafana")
        p = subprocess.run([os.path.join(ROOT, "scripts", "monitoring"), "start", "--host"], cwd=ROOT, env=env, capture_output=True, text=True)
        rep.add("scripts/monitoring start --host", p.returncode == 0, (p.stdout + p.stderr).strip()[-300:])
        if p.returncode != 0:
            return
        g = ms.Grafana(gpw)
        rep.add("Grafana 정상", bool(ms.wait_until(g.healthy, 120, 2)))

        # 규칙 ② 유도: 멘션 1건 → LLM 연결 실패 4시도(약 155초) → llm_final_failure 1
        body, headers = signed_mention(signing, "C0M47STUB")
        status = post(f"http://127.0.0.1:{mv.BUSINESS_PORT}/slack/events", body, headers)
        rep.add("서명된 멘션 수신 200(실패 유도)", status == 200, f"status={status}")
        t0 = time.time()
        got = ms.wait_until(lambda: next((h for h in hook.snapshot() if hook.rule_of(h) == "llm_final_failure"
                                          and hook.status_of(h) == "firing"), None), 420, 3)
        rep.add("규칙 ② 실제 발화 → 웹훅 수신", bool(got), f"경과={round(time.time() - t0)}초")
        if not got:
            return
        item = next(h for h in hook.snapshot() if hook.rule_of(h) == "llm_final_failure" and hook.status_of(h) == "firing")
        payload = item["body"]
        alerts = payload.get("alerts") or []
        a0 = alerts[0] if alerts else {}
        labels = a0.get("labels", {})
        rep.add("페이로드: 최상위 externalURL 문자열(R4)", isinstance(payload.get("externalURL"), str) and bool(payload.get("externalURL")),
                "없으면 설치 식별자 대안을 사용자에게 올린다")
        rep.add("페이로드: 경보별 status·labels(slack_lab_rule·severity·role='-')·startsAt", a0.get("status") == "firing"
                and labels.get("slack_lab_rule") == "llm_final_failure" and labels.get("severity") == "warning"
                and labels.get("role") == "-" and bool(a0.get("startsAt")), f"labels={sorted(labels)}")
        rep.add("웹훅 헤더에 X-Alert-Secret 전달(값은 출력하지 않음)", any(k.lower() == "x-alert-secret" for k in item["headers"]))

        # 캡처 저장(저장소 밖, 600) — 이후 재생에 쓴다
        cap_dir = os.path.join(os.path.expanduser("~/.slack-lab-secrets"), "m47")
        os.makedirs(cap_dir, mode=0o700, exist_ok=True)
        cap = os.path.join(cap_dir, f"rule2-{int(time.time())}.json")
        fd = os.open(cap, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        rep.add("캡처 본문을 저장소 밖 600 파일에 저장", oct(os.stat(cap).st_mode & 0o777) == "0o600", "경로는 출력하지 않는다(홈 아래 .slack-lab-secrets/m47)")

        # 재생: 최초 + 재전송 2회(= 같은 본문 3회) → 리포트 정확히 1
        before = len([x for x in slack.snapshot() if x.get("channel") == alert_channel])
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        codes = [post(f"http://127.0.0.1:{mv.BUSINESS_PORT}/alerts/grafana", data,
                      {"Content-Type": "application/json", "X-Alert-Secret": alert_secret}) for _ in range(3)]
        rep.add("재생 3회 모두 200", codes == [200, 200, 200], str(codes))
        ms.wait_until(lambda: len([x for x in slack.snapshot() if x.get("channel") == alert_channel]) > before, 30, 1)
        time.sleep(15)  # 재전송 뒤 늦게 도착하는 중복이 없는지 본다
        reports = [x for x in slack.snapshot() if x.get("channel") == alert_channel][before:]
        rep.add("알람 채널 리포트 정확히 1개(스텁)", len(reports) == 1, f"reports={len(reports)}")
        if reports:
            text = reports[0].get("text", "")
            rep.add("리포트는 템플릿(②는 모델 미호출): '[자동 요약]'·규칙=llm_final_failure, 'echo:'·검색 안내 푸터 없음",
                    "[자동 요약]" in text and "규칙=llm_final_failure" in text and "echo:" not in text
                    and "참고 문서" not in text and "문서 검색을 완료하지 못해" not in text and "관련 문서 근거를 찾지 못해" not in text,
                    "")
            rep.add("리포트에 event_id·시크릿 형태 없음", not re.search(r"alert-grafana-|xox[a-z]-|EvM47", text), "")
        log_path = os.path.join(sdir, "all", "slack-lab.log")
        lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines() if os.path.isfile(log_path) else []
        rag_lines = [ln for ln in lines if "metric=rag_result" in ln]
        rep.add("알람 경로 rag_result 줄 0(RAG 끔)", not rag_lines, f"lines={len(rag_lines)}")
        fallback = [ln for ln in lines if "알람 템플릿 폴백" in ln]
        rep.add("규칙 ②는 위임이 없어 폴백 줄도 0", not fallback, f"lines={len(fallback)}")
    finally:
        try:
            subprocess.run([os.path.join(ROOT, "scripts", "monitoring"), "stop"], cwd=ROOT,
                           env=dict(os.environ, S6_SESSION=sdir), capture_output=True, text=True)
        except OSError:
            pass
        hook.stop()
        if slack:
            slack.stop()
        p = s6fn("teardown", "--session", sdir, check=False)
        rep.add("teardown 최종 확인", p.returncode == 0, (p.stdout + p.stderr).strip()[:300])
