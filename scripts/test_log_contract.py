"""구조화 로그 계약 검사(6단계 M44, C1 (1)(2)(5)(9)(10)). 실행:
  python3 scripts/test_log_contract.py                       (인자 없이: 고정 픽스처 + build.gradle 제거 접두 목록 계약)
  python3 scripts/test_log_contract.py <stdout 캡처> [<회전 파일>]   (실제 기동 캡처: JSON 파싱 실패 0·필수 필드 누락 0·금지 키 0,
                                                              파일이 있으면 같은 구간 stdout JSON과 차이 0)
JSON이 아닌 줄(Gradle 진행 표시 등)은 세되 실패로 보지 않는다. `{`로 시작하는데 파싱이 안 되면 실패다. 실패하면 종료 코드 1."""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REQUIRED = ("v", "ts", "level", "role", "logger", "msg")
FIELD_KEYS = {"queue_ready", "defer", "dead", "retry", "dlq", "recovery", "queue_wait_ms", "llm_ms", "send_ms", "answer_ms", "result",
              "kind", "elapsed_ms", "reason", "gen", "retries", "manual_run", "finalized", "negative_interval", "received_at_missing",
              "final_attempt"}
FORBIDDEN_FIELD_KEYS = {"event_id", "attempt_id", "channel", "question", "error", "text"}
EVENTS = {"backlog_snapshot", "backlog_unobserved", "llm_final_failure", "processing_metric", "rag_result"}
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
STRIP_PREFIXES = ("MANAGEMENT_", "LLM_", "GRAFANA_", "SLACK_LAB_")  # 계획 C1 (10)의 4개
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


def validate(lines):
    """(JSON 줄 수, 파싱 실패, 필수 필드 누락, 계약 위반 메시지 목록, 비JSON 줄 수)."""
    n_json = bad = missing = other = 0
    problems = []
    for raw in lines:
        line = raw.rstrip("\n")
        if not line.startswith("{"):
            other += 1
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            bad += 1
            continue
        n_json += 1
        if any(k not in obj for k in REQUIRED):
            missing += 1
            continue
        if obj["v"] != 1 or not TS_RE.match(str(obj["ts"])):
            problems.append("v/ts 형식")
        if "event" in obj and obj["event"] not in EVENTS:
            problems.append(f"알 수 없는 event {obj['event']}")
        fields = obj.get("fields", {})
        if fields and "event" not in obj:
            problems.append("event 없이 fields")
        for k, v in fields.items():
            if k not in FIELD_KEYS or k in FORBIDDEN_FIELD_KEYS:
                problems.append(f"허용되지 않은 fields 키 {k}")
            if not isinstance(v, (int, bool, str)) or isinstance(v, float):
                problems.append(f"fields 값 형식 {k}")
        if obj.get("event") == "backlog_unobserved" and set(fields) - {"reason"}:
            problems.append("backlog_unobserved에 reason 외 필드")
    return n_json, bad, missing, problems, other


def fixture_selftest():
    good = ['{"v":1,"ts":"2026-10-14T01:02:03.456Z","level":"INFO","role":"worker","logger":"a","msg":"처리 지표 event_id=E1 llm_ms=5",'
            '"event":"processing_metric","fields":{"llm_ms":5}}',
            '{"v":1,"ts":"2026-10-14T01:02:04.000Z","level":"ERROR","role":"worker","logger":"a","msg":"x","exc":"E: a\\n\\tat b"}',
            "> Task :bootRun"]
    n, bad, miss, probs, other = validate(good)
    check("픽스처: 정상 줄은 위반 0", (n, bad, miss, probs, other) == (2, 0, 0, [], 1), str((n, bad, miss, probs, other)))
    n, bad, miss, probs, _ = validate(['{"v":1,"ts":"x"', '{"v":1,"ts":"2026-10-14T01:02:03.456Z"}'])
    check("픽스처: 파싱 실패·필수 필드 누락을 센다", bad == 1 and miss == 1, str((bad, miss)))
    _, _, _, probs, _ = validate(['{"v":1,"ts":"2026-10-14T01:02:03.456Z","level":"INFO","role":"w","logger":"a","msg":"m",'
                                  '"event":"processing_metric","fields":{"event_id":"E1"}}'])
    check("픽스처: fields의 event_id는 위반", any("event_id" in p for p in probs), str(probs))
    _, _, _, probs, _ = validate(['{"v":1,"ts":"2026-10-14T01:02:03.456Z","level":"INFO","role":"w","logger":"a","msg":"m",'
                                  '"event":"backlog_unobserved","fields":{"queue_ready":1}}'])
    check("픽스처: backlog_unobserved에 reason 외 필드는 위반", bool(probs), str(probs))


def gradle_prefix_contract():
    text = open(os.path.join(ROOT, "build.gradle"), encoding="utf-8").read()
    m = re.search(r"testEnvStripPrefixes\s*=\s*\[([^\]]*)\]", text)
    found = tuple(re.findall(r"'([^']+)'", m.group(1))) if m else ()
    check("build.gradle 시험 환경 제거 접두 목록 = 계획의 4개", found == STRIP_PREFIXES, str(found))


def real_capture(path, file_path=None):
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    n, bad, miss, probs, other = validate(lines)
    print(f"  stdout JSON {n}줄, 비JSON {other}줄")
    check("실제 캡처: JSON 줄이 1줄 이상", n > 0)
    check("실제 캡처: JSON 파싱 실패 0", bad == 0, f"{bad}줄")
    check("실제 캡처: 필수 필드 누락 0", miss == 0, f"{miss}줄")
    check("실제 캡처: 계약 위반 0", not probs, str(probs[:5]))
    exc_lines = [json.loads(x) for x in lines if x.startswith("{") and '"exc"' in x]
    check("실제 캡처: 예외 포함 줄도 한 줄(exc가 문자열)", all(isinstance(o["exc"], str) for o in exc_lines))
    if file_path:
        flines = [x for x in open(file_path, encoding="utf-8", errors="replace").read().splitlines() if x.startswith("{")]
        fn, fbad, fmiss, fprobs, _ = validate(flines)
        check("회전 파일: 파싱 실패·필수 누락·위반 0", fn > 0 and fbad == fmiss == 0 and not fprobs, str((fn, fbad, fmiss)))
        stamps = sorted(json.loads(x)["ts"] for x in flines)
        lo, hi = stamps[0], stamps[-1]
        same = sorted(x for x in lines if x.startswith("{") and lo <= json.loads(x)["ts"] <= hi)
        diff = len(set(same) ^ set(flines))
        check("회전 파일 줄 = 같은 구간 stdout JSON(차이 0)", diff == 0, f"차이 {diff}줄 (stdout {len(same)}, 파일 {len(flines)})")


def main(argv):
    if not argv:
        fixture_selftest()
        gradle_prefix_contract()
    elif len(argv) <= 2:
        real_capture(argv[0], argv[1] if len(argv) > 1 else None)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    print("전체", "PASS" if all(results) else "FAIL", sum(results), "/", len(results))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
