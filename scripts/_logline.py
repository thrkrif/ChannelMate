"""구조화 로그(6단계 M44)의 JSON 한 줄을 기존 소비자가 읽는 텍스트 줄(`<ISO 시각> <원문 msg>`)로 되돌린다.
p1-metrics·n8n-watch가 같은 복원을 쓰도록 한 곳에 둔다 — 두 소비자가 JSON과 옛 텍스트 로그를 같은 결과로 읽는 것이 M44의 계약이다.
JSON이 아닌 줄(옛 텍스트 로그·스택트레이스·혼합 입력)은 그대로 돌려준다. 표준 라이브러리만 쓴다(Python 3.9 호환).
"""
import json


def restore(line):
    stripped = line.lstrip()
    if not stripped.startswith("{"):
        return line
    try:
        obj = json.loads(stripped)
    except ValueError:
        return line
    if not isinstance(obj, dict) or "ts" not in obj or "msg" not in obj:
        return line
    ts = str(obj["ts"])
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    text = ts + " " + str(obj["msg"])
    exc = obj.get("exc")
    if exc:
        text += "\n" + str(exc).replace("\\n", "\n")  # 스택트레이스는 시각 없는 줄로 직전 줄에 귀속된다
    return text + ("\n" if line.endswith("\n") else "")


def restore_all(lines):
    for line in lines:
        yield restore(line)
