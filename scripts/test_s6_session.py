"""scripts/s6-session 단위 시험: PATH 앞에 가짜 docker·ollama·lsof·gradlew를 두고 argv를 캡처한다(외부 호출 0, 실제 컨테이너·모델 무접촉).
실행: python3 scripts/test_s6_session.py (실패하면 종료 코드 1)"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "s6-session")
results = []

FAKE_DOCKER = r'''#!/bin/sh
echo "docker $*" >> "$FAKE_DIR/calls.log"
case "$1 $2" in
  "volume ls")
    cat "$FAKE_DIR/volumes" 2>/dev/null ;;
  "volume rm") shift 2; for v in "$@"; do grep -vx "$v" "$FAKE_DIR/volumes" > "$FAKE_DIR/v.tmp"; mv "$FAKE_DIR/v.tmp" "$FAKE_DIR/volumes"; done ;;
  "network ls") cat "$FAKE_DIR/networks" 2>/dev/null ;;
  "network rm") : > "$FAKE_DIR/networks" ;;
  "ps -aq") cat "$FAKE_DIR/containers" 2>/dev/null ;;
  "rm -f") : > "$FAKE_DIR/containers" ;;
  *)
    if [ "$1" = "compose" ]; then
      case " $* " in
        *" down "*)
          [ -n "$FAKE_DOWN_FAIL" ] && { echo "boom" >&2; exit 1; }
          : > "$FAKE_DIR/containers"; : > "$FAKE_DIR/networks"
          case " $* " in *" -v "*) : > "$FAKE_DIR/volumes" ;; esac ;;
        *" up "*) echo c1 > "$FAKE_DIR/containers"; echo n1 > "$FAKE_DIR/networks"; echo "vol_pg" > "$FAKE_DIR/volumes" ;;
      esac
    fi ;;
esac
exit 0
'''
FAKE_OLLAMA = r'''#!/bin/sh
echo "ollama $*" >> "$FAKE_DIR/calls.log"
if [ "$1" = "ps" ]; then
  echo "NAME ID SIZE PROCESSOR UNTIL"
  [ -f "$FAKE_DIR/ollama_loaded" ] && cat "$FAKE_DIR/ollama_loaded"
fi
if [ "$1" = "stop" ]; then : > "$FAKE_DIR/ollama_loaded"; fi
exit 0
'''
FAKE_LSOF = r'''#!/bin/sh
echo "lsof $*" >> "$FAKE_DIR/calls.log"
[ -f "$FAKE_DIR/listen" ] && cat "$FAKE_DIR/listen"
exit 0
'''
FAKE_GRADLEW = '#!/bin/sh\necho "gradlew $*" >> "$FAKE_DIR/calls.log"\n'


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


class Env:
    def __init__(self):
        self.root = tempfile.mkdtemp()
        self.fake = os.path.join(self.root, "fake")
        self.bin = os.path.join(self.root, "bin")
        os.makedirs(self.fake)
        os.makedirs(self.bin)
        for name, body in (("docker", FAKE_DOCKER), ("ollama", FAKE_OLLAMA), ("lsof", FAKE_LSOF), ("gradlew", FAKE_GRADLEW)):
            p = os.path.join(self.bin, name)
            open(p, "w").write(body)
            os.chmod(p, 0o755)
        self.env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], FAKE_DIR=self.fake,
                        S6_SESSION_ROOT=os.path.join(self.root, "sessions"), S6_GRADLEW=os.path.join(self.bin, "gradlew"),
                        S6_TERM_WAIT="1")
        self.env.pop("S6_SESSION", None)

    def run(self, *args, **extra):
        env = dict(self.env, **extra)
        p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True, env=env)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def calls(self):
        path = os.path.join(self.fake, "calls.log")
        return open(path).read().splitlines() if os.path.exists(path) else []

    def clear(self):
        open(os.path.join(self.fake, "calls.log"), "w").close()

    def set(self, name, text):
        open(os.path.join(self.fake, name), "w").write(text)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


def start(e, sid="t1", *flags):
    rc, out, err = e.run("start", "--id", sid, *flags)
    return rc, out, err


# --- 시작 -----------------------------------------------------------------------------------------
e = Env()
rc, sdir, err = start(e)
check("start: 세션 디렉터리와 장부 생성, 프로젝트 이름 접두", rc == 0 and os.path.isfile(os.path.join(sdir, "ledger.json"))
      and json.load(open(os.path.join(sdir, "ledger.json")))["project"] == "slack-lab-s6v-t1", err)
e.set("volumes", "slack-lab-s6v-t2_postgres-data\n")
rc, _, err = start(e, "t2")
check("start: 같은 접두 볼륨이 있으면 거부", rc == 1 and "slack-lab-s6v-t2_postgres-data" in err, err)
e.set("volumes", "")
rc, _, err = start(e, "bad id!")
check("start: 이상한 기동 ID는 거부", rc == 2, err)

# --- 모니터링 미사용 세션: 장부 구성 그대로 down, 모니터링 호출 0 ----------------------------------------
e.clear()
rc, _, err = e.run("compose", "--session", sdir, "--", "-f", "compose.yaml", "--profile", "app", "up", "-d", "rabbitmq", "postgres")
check("compose: 장부에 쓰고 같은 -p로 실행", rc == 0 and any(c.startswith("docker compose -p slack-lab-s6v-t1 -f compose.yaml --profile app up") for c in e.calls()), "\n".join(e.calls()))
ledger = json.load(open(os.path.join(sdir, "ledger.json")))
check("장부: -f·--profile 기록, 모니터링 사용 아님", ledger["files"] == ["compose.yaml"] and ledger["profiles"] == ["app"] and not ledger["monitoring"])
e.clear()
rc, out, err = e.run("teardown", "--session", sdir)
calls = "\n".join(e.calls())
check("teardown: 장부 구성 그대로 down -v(볼륨 삭제 기본)", rc == 0 and "docker compose -p slack-lab-s6v-t1 -f compose.yaml --profile app down --remove-orphans -v" in calls, out + err + calls)
check("teardown: 모니터링 미사용이면 compose.monitoring.yaml·scripts/monitoring 호출 0", "compose.monitoring.yaml" not in calls and "scripts/monitoring" not in calls and "monitoring" not in calls, calls)
check("teardown: gradle 데몬 정지·ollama 점검 호출", "gradlew --stop" in calls and "ollama ps" in calls, calls)
check("teardown: 마지막에 알림 문구", "남은 것 없음" in out, out)
check("teardown: 결과 파일 problems 비어 있음", json.load(open(os.path.join(sdir, "result.json")))["problems"] == [])
e.close()

# --- down 실패 → 레이블 기반 정리 -----------------------------------------------------------------------
e = Env()
rc, sdir, _ = start(e)
e.run("compose", "--session", sdir, "--", "up", "-d")
e.clear()
rc, out, err = e.run("teardown", "--session", sdir, FAKE_DOWN_FAIL="1")
calls = "\n".join(e.calls())
check("down 실패 시 레이블 정리로 넘어가 컨테이너·네트워크 제거", rc == 0 and "docker rm -f" in calls and "docker network rm" in calls
      and "label=com.docker.compose.project=slack-lab-s6v-t1" in calls, out + err + calls)
e.close()

# --- 보존 선택: -v 0, 볼륨 이름 기록 ----------------------------------------------------------------------
e = Env()
rc, sdir, _ = start(e, "keep", "--keep-volumes")
e.run("compose", "--session", sdir, "--", "up", "-d")
e.clear()
rc, out, err = e.run("teardown", "--session", sdir)
calls = e.calls()
check("보존 선택 시 down에 -v 없음", rc == 0 and not any(" down " in c and " -v" in c for c in calls) and not any(c.startswith("docker volume rm") for c in calls), "\n".join(calls))
check("보존 선택 시 남은 볼륨 이름을 결과 파일·알림에 기록", "vol_pg" in out and json.load(open(os.path.join(sdir, "result.json")))["kept_volumes"] == ["vol_pg"], out)
e.close()

# --- PID 장부: 장부의 프로세스만 정지, 장부 밖 프로세스 무접촉 ------------------------------------------------
e = Env()
rc, sdir, _ = start(e)
outsider = subprocess.Popen(["sleep", "60"], start_new_session=True)
rc, pid, err = e.run("spawn", "--session", sdir, "--name", "sleeper", "--env", "FOO=1", "--", "sleep", "60")
check("spawn: PID 기록", rc == 0 and pid.isdigit() and json.load(open(os.path.join(sdir, "ledger.json")))["pids"][0]["pid"] == int(pid), err)
rc, out, err = e.run("teardown", "--session", sdir)
time.sleep(0.3)
def is_alive(p):
    try:
        os.kill(p, 0)
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(p)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")
check("teardown: 장부의 프로세스는 종료", rc == 0 and not is_alive(int(pid)), out + err)
check("teardown: 장부 밖 프로세스는 건드리지 않음", is_alive(outsider.pid))
os.kill(outsider.pid, signal.SIGKILL)
outsider.wait()
e.close()

# --- 최종 확인 실패 → exit 1 ----------------------------------------------------------------------------
e = Env()
rc, sdir, _ = start(e)
e.run("compose", "--session", sdir, "--", "up", "-d")
e.set("listen", "java 1 u 1 u IPv6 0t0 TCP *:8080 (LISTEN)\n")
rc, out, err = e.run("teardown", "--session", sdir)
check("리스너 8080이 남으면 exit 1과 사유 출력", rc == 1 and "8080" in out, out + err)
e.set("listen", "")
e.set("ollama_loaded", "qwen2.5:3b abc 2GB 100% CPU 4 minutes from now\n")
rc, out, err = e.run("teardown", "--session", sdir)
calls = "\n".join(e.calls())
check("ollama 모델이 올라와 있으면 stop 호출", "ollama stop qwen2.5:3b" in calls and rc == 0, out + err + calls)
e.close()

# --- 모니터링 사용 표시 ---------------------------------------------------------------------------------
e = Env()
rc, sdir, _ = start(e)
e.run("compose", "--session", sdir, "--", "-f", "compose.yaml", "-f", "compose.monitoring.yaml", "--profile", "monitoring", "up", "-d")
ledger = json.load(open(os.path.join(sdir, "ledger.json")))
check("compose.monitoring.yaml/profile 사용은 장부에 monitoring=true", ledger["monitoring"] is True and "compose.monitoring.yaml" in ledger["files"])
e.close()

rc = subprocess.run([sys.executable, SCRIPT, "bogus"], capture_output=True, text=True).returncode
check("알 수 없는 명령은 종료 코드 2", rc == 2)

print("전체", "PASS" if all(results) else "FAIL", sum(results), "/", len(results))
sys.exit(0 if all(results) else 1)
