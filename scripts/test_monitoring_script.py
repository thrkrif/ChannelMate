"""scripts/monitoring 단위 검증(M46). PATH 앞의 가짜 `docker`로 argv를 캡처한다 — 데몬·네트워크·실제 .env 없음.
실행: python3 scripts/test_monitoring_script.py"""
import json
import os
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "monitoring")
results = []
TOKEN = "0123456789abcdef" * 4


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


class Env:
    def __init__(self, token=TOKEN, volumes=()):
        self.dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.dir, "calls.log")
        self.envfile = os.path.join(self.dir, "env")
        with open(self.envfile, "w") as f:
            f.write("SLACK_SIGNING_SECRET=a\nSLACK_BOT_TOKEN=b\nPOSTGRES_PASSWORD=c\nLLM_MODEL=m\nSLACK_TEST_CHANNEL=C1\n"
                    f"GRAFANA_ADMIN_PASSWORD=Gr4fana-Strong-Pass-99\n" + (f"MANAGEMENT_METRICS_TOKEN={token}\n" if token else ""))
        fake = os.path.join(self.bin, "docker")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> \"%s\"\n" % self.log)
            f.write("if [ \"$1\" = volume ] && [ \"$2\" = ls ]; then printf '%s'; fi\n" % "\\n".join(volumes))
            f.write("exit 0\n")
        os.chmod(fake, 0o755)
        self.secrets = os.path.join(self.dir, "secrets")
        self.mon = os.path.join(self.dir, "mon")

    def run(self, *args, extra=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SLACK_LAB_", "S6_", "MANAGEMENT_", "GRAFANA_"))}
        env.update({"PATH": self.bin + os.pathsep + env["PATH"], "SLACK_LAB_ENV_FILE": self.envfile, "SLACK_LAB_SECRET_DIR": self.secrets,
                    "SLACK_LAB_MON_DIR": self.mon})
        env.update(extra or {})
        p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)
        return p.returncode, p.stdout, p.stderr

    def calls(self):
        return open(self.log).read().splitlines() if os.path.exists(self.log) else []


e = Env()
rc, out, err = e.run("start", "--host")
calls = e.calls()
check("start --host 성공", rc == 0, out + err)
up = [c for c in calls if " up " in f" {c} "]
check("compose 호출은 -p 프로젝트·-f 두 파일·--profile monitoring 고정, 서비스 4개만 up", len(up) == 1
      and up[0].startswith("compose -p slack-lab -f compose.yaml -f compose.monitoring.yaml --profile monitoring up -d alloy loki prometheus grafana"), str(up))
tok = os.path.join(e.secrets, "metrics_token")
check("토큰 파일 600·저장소 밖, 내용은 .env의 값", os.path.isfile(tok) and stat.S_IMODE(os.stat(tok).st_mode) == 0o600 and open(tok).read() == TOKEN)
check("토큰 값이 출력·docker 인자에 없음", TOKEN not in out + err and not any(TOKEN in c for c in calls))
targets = json.load(open(os.path.join(e.mon, "targets", "targets.json")))
check("--host 대상 = host.docker.internal:9091 role=all", targets == [{"targets": ["host.docker.internal:9091"], "labels": {"role": "all"}}], str(targets))

rc, out, err = e.run("start", "--app")
targets = json.load(open(os.path.join(e.mon, "targets", "targets.json")))
check("--app 대상 = receiver·worker·reactor:9091 역할 레이블", [t["labels"]["role"] for t in targets] == ["receiver", "worker", "reactor"]
      and [t["targets"][0] for t in targets] == ["receiver:9091", "worker:9091", "reactor:9091"], str(targets))
check("모드가 바뀌면 Grafana 컨테이너를 먼저 제거(옛 규칙 파일 방지)", any(" rm -s -f grafana" in c for c in e.calls()))

e2 = Env(token="")
rc, out, err = e2.run("start", "--host")
check("토큰이 없으면 시작 거부(exit 1)·docker 호출 0", rc == 1 and not e2.calls(), out + err)

e3 = Env(token="not-hex")
rc, out, err = e3.run("start", "--host")
check("토큰 형식이 틀리면 시작 거부·값 비출력", rc == 1 and "not-hex" not in out + err and not e3.calls(), out + err)

e4 = Env()
rc, out, err = e4.run("stop")
c = e4.calls()
check("stop은 rm -s -f 4개만(볼륨·down 없음)", rc == 0 and len(c) == 1 and c[0].endswith("rm -s -f alloy loki prometheus grafana") and "down" not in c[0] and " -v" not in c[0], str(c))

e5 = Env(volumes=("slack-lab_mon-alloy", "slack-lab_mon-loki", "slack-lab_mon-prometheus", "slack-lab_mon-grafana", "slack-lab_postgres-data", "slack-lab_rabbitmq-data"))
rc, out, err = e5.run("reset")
check("reset은 --confirm 없으면 거부(exit 2)·docker 호출 0", rc == 2 and not e5.calls())
rc, out, err = e5.run("reset", "--confirm")
c = e5.calls()
rm = [x for x in c if x.startswith("volume rm")]
check("reset --confirm: 모니터링 볼륨 4개만 volume rm(postgres·rabbitmq 유지)", len(rm) == 1 and all(v in rm[0] for v in ("mon-alloy", "mon-loki", "mon-prometheus", "mon-grafana"))
      and "postgres" not in rm[0] and "rabbitmq" not in rm[0], str(c))
check("reset은 down을 부르지 않음", not any(" down" in x for x in c))

e6 = Env(volumes=("slack-lab_postgres-data",))
e6.run("reset", "--confirm")
check("모니터링 볼륨이 없으면 volume rm 호출 0", not any(x.startswith("volume rm") for x in e6.calls()))

rc, out, err = e.run("start")
check("start에 모드가 없으면 exit 2", rc == 2)
rc, out, err = e.run("bogus")
check("알 수 없는 명령은 exit 2", rc == 2)

# 프로젝트 이름: 환경 변수
e7 = Env()
e7.run("stop", extra={"SLACK_LAB_COMPOSE_PROJECT": "slack-lab-s6v-x"})
check("SLACK_LAB_COMPOSE_PROJECT가 -p에 반영", e7.calls() and e7.calls()[0].startswith("compose -p slack-lab-s6v-x "))

# 호스트 모드의 로그 touch 보조 프로세스(Docker Desktop bind mount 캐시 우회)
import time  # noqa: E402

e8 = Env()
logdir = os.path.join(e8.dir, "logs")
os.makedirs(os.path.join(logdir, "all"))
logfile = os.path.join(logdir, "all", "slack-lab.log")
open(logfile, "w").write("x\n")
os.utime(logfile, (1000000000, 1000000000))
rc, out, err = e8.run("start", "--host", extra={"SLACK_LAB_LOG_DIR_HOST": logdir})
pidfile = os.path.join(e8.mon, "touch.pid")
pid = int(open(pidfile).read()) if os.path.isfile(pidfile) else 0
time.sleep(2.5)
check("--host: touch 프로세스가 로그 mtime을 갱신(내용·크기 무변경)", rc == 0 and pid > 0 and os.stat(logfile).st_mtime > 1000000000 + 100 and open(logfile).read() == "x\n")
e8.run("stop", extra={"SLACK_LAB_LOG_DIR_HOST": logdir})
time.sleep(1.2)
try:
    os.kill(pid, 0)
    alive = True
except OSError:
    alive = False
check("stop이 touch 프로세스를 끄고 PID 파일을 지움", not alive and not os.path.exists(pidfile))
e9 = Env()
e9.run("start", "--app")
check("--app 모드는 touch 프로세스를 두지 않음(컨테이너 안에서 쓰는 파일은 캐시 문제 없음)", not os.path.exists(os.path.join(e9.mon, "touch.pid")))

print("전체", "PASS" if all(results) else "FAIL", sum(results), "/", len(results))
sys.exit(0 if all(results) else 1)
