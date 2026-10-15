"""scripts/p1-load의 스텁 모드 검증(6단계 M48 C8). 외부 호출 0: Slack API 기본 주소를 로컬 기록용 스텁으로 돌려
실제 채널에 부모 메시지를 올리지 않는다. 실행: python3 scripts/test_p1_load.py"""
import os
import re
import socket
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _mon_stubs  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load(argv_extra=(), env=None):
    """p1-load를 main() 없이 exec로 불러온다. 실제 .env의 값 대신 더미를 환경에 먼저 둔다."""
    os.environ.update(SLACK_BOT_TOKEN="dummy", SLACK_SIGNING_SECRET="dummy", SLACK_TEST_CHANNEL="CDUMMY")
    for k in ("SLACK_BASEURL",):
        os.environ.pop(k, None)
    os.environ.update(env or {})
    old = sys.argv
    sys.argv = ["p1-load", *argv_extra]
    try:
        src = open(os.path.join(HERE, "p1-load"), encoding="utf-8").read()
        src = re.sub(r"\nmain\(\)\s*$", "\n", src)
        ns = {"__file__": os.path.join(HERE, "p1-load")}
        exec(compile(src, "p1-load", "exec"), ns)
        ns["_argv_after"] = list(sys.argv)
        return ns
    finally:
        sys.argv = old


# 외부 접속이 실제로 일어나면 실패하도록 slack.com 접속을 막는다
real_open = urllib.request.urlopen


def guarded(req, *a, **k):
    url = req if isinstance(req, str) else req.full_url
    if "slack.com" in url:
        raise AssertionError("slack.com으로 나가는 요청이 있었다")
    return real_open(req, *a, **k)


urllib.request.urlopen = guarded

port = free_port()
stub = _mon_stubs.SlackRecorder(port).start()
try:
    ns = load(["--slack-base-url", f"http://127.0.0.1:{port}"])
    check("--slack-base-url이 API 기본 주소가 된다", ns["API_BASE"] == f"http://127.0.0.1:{port}", ns["API_BASE"])
    check("모드 파싱 전에 인자를 떼어 낸다(sys.argv에 남지 않음)", ns["_argv_after"] == ["p1-load"], str(ns["_argv_after"]))
    ts = ns["parent"]("스텁 점검")
    check("부모 메시지는 스텁에만 간다(실제 채널 0)", stub.count("chat.postMessage", "CDUMMY") == 1 and ts)
    check("봇 사용자 조회도 스텁", ns["bot_user"]() == _mon_stubs.BOT_USER and stub.count("auth.test") == 1)
    check("답글 조회는 스텁이 센 봇 답글 수를 돌려준다(0개)", ns["replies"](ts) == [])
    import json
    r = urllib.request.Request(f"http://127.0.0.1:{port}/chat.postMessage",
                               data=json.dumps({"channel": "CDUMMY", "thread_ts": ts, "text": "x"}).encode(), method="POST",
                               headers={"Content-Type": "application/json"})
    real_open(r, timeout=5).read()
    check("봇 답글 1개가 스레드 조회에 보인다", len(ns["replies"](ts)) == 1)
    summary = stub.summary()
    check("스텁 요약에 본문이 없다(경로·채널별 건수만)", all(isinstance(v, int) for d in summary.values() for v in d.values()), str(summary))
    r = urllib.request.Request(f"http://127.0.0.1:{port}/chat.postMessage",
                               data=json.dumps({"channel": "CDUMMY", "thread_ts": ts,
                                                "text": _mon_stubs.FAILURE_NOTICE_PREFIX + " 원문 비보관"}).encode(), method="POST",
                               headers={"Content-Type": "application/json"})
    real_open(r, timeout=5).read()
    from unittest.mock import patch
    with patch.object(ns["time"], "sleep", lambda _s: None):
        audit = ns["audit"]("안내 구분", [ts], settle=0)
    check("기록용 스텁이 본문 없이 실패 안내를 구분한다", audit["failure_notice"] == 1 and audit["dup"] == 1)
    check("안내 원문이 스텁 상태에 남지 않는다", "원문 비보관" not in repr(vars(stub)))
finally:
    stub.stop()

ns2 = load(env={"SLACK_BASEURL": "http://127.0.0.1:1/api"})
check("환경변수 SLACK_BASEURL도 따른다", ns2["API_BASE"] == "http://127.0.0.1:1/api", ns2["API_BASE"])
ns3 = load()
check("지정이 없으면 실제 Slack 주소(기존 동작)", ns3["API_BASE"] == "https://slack.com/api")
import tempfile
from unittest.mock import patch
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "summary.json")
    ns3["ENV"]["P1_SUMMARY_OUT"] = path
    ns3["ENV"]["P1_REPLY_TIMEOUT"] = "240"
    waited = []
    ns3.update(parent=lambda _label: "dummy-ts", send=lambda *args: (200, 1),
               wait_replies=lambda tss, timeout: waited.append(timeout) or {"dummy-ts": 1})
    with patch.object(ns3["time"], "sleep", lambda _s: None):
        ns3["run_seq"](1)
    check("순차 답글 대기는 설정한 240초를 따른다", waited == [240])
    ns3["run_seq"] = lambda n: (["dummy-ts"], [200])
    ns3["audit"] = lambda *args, **kw: {"threads": 1, "ok": 1, "missing": 0, "dup": 0, "failure_notice": 0, "language_violation": 0}
    with patch.object(sys, "argv", ["p1-load", "seq", "1"]):
        ns3["main"]()
    saved = json.load(open(path, encoding="utf-8"))
    check("완료 요약은 원본 식별자 없이 HTTP 실패·건수만 저장한다", saved == {"summary": ns3["audit"](), "http_errors": 0}
          and "dummy-ts" not in json.dumps(saved) and not os.path.exists(path + ".tmp"))
print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
