"""6단계 M48: `monitoring-verify run`의 실제 환경(RealEnv)과 jar 실행기. 단위 시험은 `_mon_run.Env` 모사를 쓰고, 이 모듈의 외부 접속 부분은
M49a 세션에서 처음 실사용한다(그래서 규칙 ①②·앱 중단·대시보드 시나리오의 유도 조작은 아직 연결하지 않았다 — 부채 목록 참조).
비밀값(.env)은 메모리에서만 쓰고 결과 파일·argv·출력에 넣지 않는다.
"""
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
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
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
    """Grafana API(상태)·기록용 Slack 스텁(리포트 수)·플래그 파일/Docker(유도)를 실제로 다루는 환경. 규칙 ③·④만 유도 조작이 연결돼 있다."""

    def __init__(self, sdir, project, grafana, slack_stub, alert_channel, receiver_url, signing_secret, mode,
                 bot_user=None, role_targets=None):
        self.sdir, self.project, self.g, self.slack, self.channel = sdir, project, grafana, slack_stub, alert_channel
        self.url, self.secret, self.mode = receiver_url.rstrip("/"), signing_secret, mode
        self.flag = os.path.join(sdir, "pause.flag")
        self.base_reports = {}
        self.targets = role_targets or ["receiver", "worker", "reactor"]
        self.seq = 0

    def now(self):
        return time.time()

    def sleep(self, s):
        time.sleep(max(0.0, s))

    # --- 관측 ---
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

    # --- 유도 ---
    def _mention(self, question="로그 적체 원인이 뭐야"):
        """서명이 유효한 합성 app_mention 1건(기록용 스텁 채널, 실제 Slack 아님). 이벤트 ID는 호출마다 다르다."""
        self.seq += 1
        body = json.dumps({"type": "event_callback", "event_id": f"EvS6Verify{int(time.time())}_{self.seq}",
                           "authorizations": [{"user_id": "UBOTSTUB"}],
                           "event": {"type": "app_mention", "channel": "CSTUBVERIFY", "user": "U1", "text": f"<@UBOTSTUB> {question}",
                                     "ts": f"{int(time.time())}.{self.seq:06d}"}}, ensure_ascii=False).encode("utf-8")
        stamp = str(int(time.time()))
        sig = "v0=" + hmac.new(self.secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
        req = urllib.request.Request(self.url + "/slack/events", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Slack-Request-Timestamp": stamp, "X-Slack-Signature": sig})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except Exception:  # noqa: BLE001
            return -1

    def inject_requests(self, n):
        """큐가 정지된 동안 합성 요청 n건(③ 유도). 모두 200이어야 한다."""
        return [self._mention() for _ in range(n)]

    def induce(self, rule, variant, round_no):
        self.base_reports[rule] = self.slack.count("chat.postMessage", self.channel)
        if rule == "rule3":
            open(self.flag, "w").close()  # 정지: PauseControl이 소비자를 닫는다
            time.sleep(2)  # 전이 반영(폴링 500ms) 대기
            n = 10 if variant == "fire" else (9 if round_no % 2 else 10)  # 비발화 (a) 9건 / (b) 10건 번갈아
            codes = self.inject_requests(n)
            if any(c != 200 for c in codes):
                raise RuntimeError("합성 요청 중 200이 아닌 응답이 있었다")
            return time.time()
        if rule in ("rule4-block", "rule4-absent"):
            raise NotImplementedError("④ 유도 조작은 역할별 대상을 받아 M49a 첫 회차에서 연결한다")
        raise NotImplementedError(f"{rule} 유도 조작은 M49a에서 연결한다(부채)")

    def resolve(self, rule, variant, round_no):
        if rule == "rule3" and os.path.exists(self.flag):
            os.remove(self.flag)
            return
        raise NotImplementedError(f"{rule} 해소 조작은 M49a에서 연결한다(부채)")


def ensure_session_dir(sdir):
    if not os.path.isdir(sdir):
        raise RuntimeError("세션 디렉터리가 없다: 먼저 `monitoring-verify session start --id <ID>`")
    return sdir
