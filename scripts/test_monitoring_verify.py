"""scripts/monitoring-verify 단위 검증(M45 범위: mgmt 판정 로직). 외부 호출 없음. 실행: python3 scripts/test_monitoring_verify.py"""
import importlib.machinery
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

print("전체", "PASS" if all(results) else "FAIL", sum(results), "/", len(results))
sys.exit(0 if all(results) else 1)
