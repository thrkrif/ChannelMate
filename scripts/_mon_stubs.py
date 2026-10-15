"""6단계 M48 검증 스텁: 기록용 Slack 스텁과 OpenAI 호환 LLM 스텁. 외부 호출 0, 127.0.0.1에만 열린다.

기록용 Slack 스텁은 경로·채널별 건수만 남기고 **본문은 저장하지 않는다**(질문·답변·event_id·채널 ID가 결과 파일에 들어가지 않게).
채널마다 동작을 제어 엔드포인트로 바꾼다: ok(정상) / fail(영구 실패: ok=false) / drop(요청을 받은 뒤 응답 없이 연결을 끊음 = 결과 불명).
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BOT_USER = "UBOTSTUB"
FAILURE_NOTICE_PREFIX = "죄송해요, 지금 답변을 만들지 못했어요"


def _send(handler, code, obj):
    out = json.dumps(obj).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(out)))
    handler.end_headers()
    handler.wfile.write(out)


class SlackRecorder:
    """Slack Web API 모사. 제어: POST /__control {"channel": "C…" | "*", "mode": "ok|fail|drop"}, 조회: GET /__counts."""

    def __init__(self, port, host="127.0.0.1"):
        self.port, self.host = port, host
        self.modes = {}  # channel -> mode, "*" = 기본
        self.counts = {}  # (path, channel) -> 건수
        self.threads = {}  # (channel, thread_ts) -> 봇 답글 수
        self.notices = {}
        self.seq = 0
        self.lock = threading.Lock()
        self.server = None

    def mode_for(self, channel):
        return self.modes.get(channel) or self.modes.get("*") or "ok"

    def count(self, path, channel=None):
        with self.lock:
            return sum(n for (p, c), n in self.counts.items() if p == path and (channel is None or c == channel))

    def summary(self):
        with self.lock:
            out = {}
            for (path, channel), n in self.counts.items():
                out.setdefault(path, {})[channel or "-"] = n
            return out

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
                try:
                    return json.loads(raw) if raw else {}
                except ValueError:
                    return {}

            def do_POST(self):  # noqa: N802
                path = urlparse(self.path).path
                body = self._body()
                if path == "/__control":
                    with outer.lock:
                        outer.modes[body.get("channel", "*")] = body.get("mode", "ok")
                    return _send(self, 200, {"ok": True})
                method = path.rsplit("/", 1)[-1]
                channel = body.get("channel")
                with outer.lock:
                    outer.counts[(method, channel)] = outer.counts.get((method, channel), 0) + 1
                mode = outer.mode_for(channel)
                if mode == "drop":
                    self.connection.close()  # 요청은 받았으나 응답이 없다 = 전송 결과 불명
                    return
                if mode == "fail":
                    return _send(self, 200, {"ok": False, "error": "not_in_channel"})
                if method == "chat.postMessage":
                    with outer.lock:
                        outer.seq += 1
                        ts = f"{int(time.time())}.{outer.seq:06d}"
                        if body.get("thread_ts"):
                            key = (channel, body["thread_ts"])
                            outer.threads[key] = outer.threads.get(key, 0) + 1
                            # 본문을 보관하지 않아도 실패 안내와 정상 답글을 대조할 수 있어야 한다.
                            outer.notices[key] = outer.notices.get(key, 0) + int(body.get("text", "").startswith(FAILURE_NOTICE_PREFIX))
                    return _send(self, 200, {"ok": True, "ts": ts, "channel": channel})
                if method == "auth.test":
                    return _send(self, 200, {"ok": True, "user_id": BOT_USER})
                return _send(self, 200, {"ok": True})

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                if u.path == "/__counts":
                    return _send(self, 200, outer.summary())
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                method = u.path.rsplit("/", 1)[-1]
                channel = q.get("channel")
                with outer.lock:
                    outer.counts[(method, channel)] = outer.counts.get((method, channel), 0) + 1
                mode = outer.mode_for(channel)
                if mode == "drop":
                    self.connection.close()
                    return
                if method == "conversations.replies" and mode != "fail":
                    n = outer.threads.get((channel, q.get("ts")), 0)
                    notices = outer.notices.get((channel, q.get("ts")), 0)
                    msgs = [{"ts": q.get("ts"), "text": "(스텁 부모)", "user": "UPARENT"}]
                    msgs += [{"ts": f"{q.get('ts')}.{i}", "text": FAILURE_NOTICE_PREFIX if i < notices else "(스텁 답글)",
                              "user": BOT_USER, "bot_id": "BSTUB"} for i in range(n)]
                    return _send(self, 200, {"ok": True, "messages": msgs})
                if mode == "fail":
                    return _send(self, 200, {"ok": False, "error": "not_in_channel"})
                return _send(self, 200, {"ok": True, "messages": []})

        self.server = ThreadingHTTPServer((self.host, self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


class LlmStub:
    """OpenAI 호환 /v1/chat/completions·/v1/models 모사. 제어: POST /__arm {"fail_next": N, "status": 503} (다음 N회 요청에 오류)."""

    def __init__(self, port, model="stub-model", host="127.0.0.1"):
        self.port, self.host, self.model = port, host, model
        self.fail_next = 0
        self.status = 503
        self.requests = 0
        self.generations = 0
        self.lock = threading.Lock()
        self.server = None

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if urlparse(self.path).path.endswith("/models"):
                    return _send(self, 200, {"object": "list", "data": [{"id": outer.model, "object": "model"}]})
                if urlparse(self.path).path == "/__stats":
                    return _send(self, 200, {"requests": outer.requests, "generations": outer.generations})
                return _send(self, 404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                path = urlparse(self.path).path
                if path == "/__arm":
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        body = {}
                    with outer.lock:
                        outer.fail_next = int(body.get("fail_next", 0))
                        outer.status = int(body.get("status", 503))
                    return _send(self, 200, {"ok": True})
                with outer.lock:
                    outer.requests += 1
                    if outer.fail_next > 0:
                        outer.fail_next -= 1
                        return _send(self, outer.status, {"error": {"message": "stub failure"}})
                    outer.generations += 1
                return _send(self, 200, {"id": "stub", "object": "chat.completion", "model": outer.model,
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "(스텁 답변) 확인했습니다."}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        self.server = ThreadingHTTPServer((self.host, self.port), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
