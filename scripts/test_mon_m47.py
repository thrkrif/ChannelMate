#!/usr/bin/env python3
"""M47 `replay` 보조 함수의 단위 시험(외부 호출 없음)."""
import hashlib
import hmac
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _mon_m47 as m47  # noqa: E402


class SignedMentionTest(unittest.TestCase):
    def test_서명은_슬랙_규칙_v0_타임스탬프_본문의_HMAC이다(self):
        body, headers = m47.signed_mention("sekret", "C0X")
        ts = headers["X-Slack-Request-Timestamp"]
        want = "v0=" + hmac.new(b"sekret", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Slack-Signature"], want)

    def test_본문은_app_mention_이벤트이고_채널을_담는다(self):
        body, _ = m47.signed_mention("s", "C0X")
        ev = json.loads(body)["event"]
        self.assertEqual((ev["type"], ev["channel"]), ("app_mention", "C0X"))


class SlackStubTest(unittest.TestCase):
    def test_chat_postMessage만_기록하고_ok를_돌려준다(self):
        stub = m47.SlackStub(18953)
        try:
            self.assertEqual(m47.post("http://127.0.0.1:18953/chat.postMessage", b'{"channel":"C1","text":"t"}',
                                      {"Content-Type": "application/json"}), 200)
            self.assertEqual(m47.post("http://127.0.0.1:18953/reactions.add", b"{}", {"Content-Type": "application/json"}), 200)
            self.assertEqual([p.get("channel") for p in stub.snapshot()], ["C1"])
        finally:
            stub.stop()


if __name__ == "__main__":
    unittest.main()
