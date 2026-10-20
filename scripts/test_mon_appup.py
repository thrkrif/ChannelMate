"""B4 앱 세션 준비(`alarm-up --mode app`)의 연결 순서·부분 실패 정리를 가짜 io로 확인한다(컨테이너·프로세스·네트워크 접근 0)."""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock

import _mon_appup as up
import _mon_model as model


class FakeStub:
    def __init__(self, log):
        self.log = log

    def request(self, path, body=None):
        return {}

    def mode(self, channel, mode):
        self.log.append(("stub-mode", channel, mode))


class FakeIo:
    def __init__(self, sdir):
        self.sdir, self.root, self.project = sdir, "/repo", "proj"
        self.env = {k: "v" for k in up.REQUIRED}
        self.env["ALERT_CHANNEL"] = "CALARM"
        self.log, self.fail_at, self.busy, self.problems = [], None, set(), []
        self.t = 0.0

    def now(self):
        self.t += 1
        return self.t

    def ledger_line(self, value):
        self.log.append(("ledger", value["step"]))

    def _maybe(self, name):
        if self.fail_at == name:
            raise RuntimeError(name)

    def s6(self, *args):
        sub = args[args.index("app") + 1] if args[0] == "compose" else ""
        self.log.append(("s6", args[0], sub))
        return (1, "") if sub == "up" and self.fail_at == "compose-up" else (0, "")

    def run(self, args, env=None):
        name = "monitoring-" + args[1]
        self.log.append(("run", name))
        if self.fail_at == name:
            return 1, ""
        return 0, ""

    def mon_env(self):
        return {}

    def listening(self, port):
        return port in self.busy

    def spawn_stub(self, name, kind, port):
        self._maybe("stub-" + kind)
        self.log.append(("spawn", kind))
        return 100 + port % 10

    def kill(self, pid):
        self.log.append(("kill", pid))

    def slack(self, base):
        return FakeStub(self.log)

    def write_override(self, sdir, name, lines):
        self.log.append(("override", tuple(lines)))
        path = os.path.join(sdir, name)
        with open(path, "w") as stream:
            stream.write("services:\n  receiver:\n    environment:\n      A: b\n")
        return path

    def wait(self, predicate, seconds, reason):
        self._maybe(reason)
        if not predicate():
            raise RuntimeError(reason)

    def grafana_healthy(self):
        return True

    def up_value(self, role):
        return 1.0

    def receiver_health(self):
        return True

    def rules_problems(self):
        return self.problems


class AppUpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.io = FakeIo(self.tmp.name)
        self.up = up.AppUp(self.tmp.name, self.io)
        self.state = os.path.join(self.tmp.name, up.STATE_NAME)

    def kinds(self):
        return [e[:2] for e in self.io.log]

    def test_real_io_stubs_listen_for_container_requests(self):
        io = up.RealIo(self.tmp.name, Mock(), {}, "proj", Mock(), "/repo")
        io.s6 = Mock(return_value=(0, "123"))
        for kind, port in (("slack", up.SLACK_PORT), ("llm", up.LLM_PORT)):
            self.assertEqual(io.spawn_stub("app-" + kind, kind, port), 123)
            self.assertEqual(io.s6.call_args.args[-6:], ("stub", kind, "--port", str(port), "--host", "0.0.0.0"))

    def test_success_order_and_state_written_last(self):
        self.assertEqual(self.up.up(), model.EXIT_OK)
        flat = [(e[0], e[1]) for e in self.io.log if e[0] in ("spawn", "s6", "run")]
        self.assertEqual(flat, [("spawn", "slack"), ("spawn", "llm"), ("s6", "mark"), ("s6", "compose"), ("run", "monitoring-start")])
        ledger = [e[1] for e in self.io.log if e[0] == "ledger"]
        self.assertEqual(ledger[-1], "state-written")
        self.assertLess(ledger.index("rules-parity"), ledger.index("state-written"))
        with open(self.state) as stream:
            data = json.load(stream)
        self.assertTrue(data["ready"])
        self.assertEqual(data["project"], "proj")
        self.assertEqual(data["slack_url"], "http://127.0.0.1:18962")
        self.assertEqual(data["roles"], ["receiver", "worker", "reactor"])

    def test_override_points_apps_at_stub_and_mounts_logs(self):
        self.up.up()
        lines = next(e[1] for e in self.io.log if e[0] == "override")
        self.assertIn('SLACK_BASEURL: "http://host.docker.internal:18962"', lines)
        self.assertIn('SLACK_LAB_LOG_DIR: "/logs"', lines)
        with open(os.path.join(self.tmp.name, up.OVERRIDE_NAME)) as stream:
            text = stream.read()
        self.assertIn(self.tmp.name + ":/logs", text)

    def test_alert_and_test_channels_set_ok(self):
        self.up.up()
        modes = [e[1:] for e in self.io.log if e[0] == "stub-mode"]
        self.assertEqual(modes, [("CSTUBVERIFY", "ok"), ("CALARM", "ok")])

    def test_failure_rolls_back_in_reverse_and_never_writes_state(self):
        self.io.fail_at = "monitoring-start"
        self.assertEqual(self.up.up(), model.EXIT_FAIL)
        self.assertFalse(os.path.exists(self.state))
        # 모니터링 시작 실패 → 모니터링 stop → 앱 stop → override 삭제 → 스텁 종료(역순)
        order = [(e[0], e[1], e[2] if len(e) > 2 else None) for e in self.io.log if (e[0], e[1]) in
                 (("run", "monitoring-stop"), ("s6", "compose"), ("kill", 100 + 62 % 10), ("kill", 100 + 63 % 10))]
        self.assertEqual(order[-4:], [("run", "monitoring-stop", None), ("s6", "compose", "stop"), ("kill", 103, None), ("kill", 102, None)])
        kills = [e[1] for e in self.io.log if e[0] == "kill"]
        self.assertEqual(len(kills), 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, up.OVERRIDE_NAME)))

    def test_early_failure_only_undoes_what_was_done(self):
        self.io.fail_at = "stub-llm"
        self.assertEqual(self.up.up(), model.EXIT_FAIL)
        self.assertEqual([e for e in self.io.log if e[0] == "kill"], [("kill", 102)])
        self.assertFalse([e for e in self.io.log if e[0] == "s6"])
        self.assertFalse(os.path.exists(self.state))

    def test_compose_up_failure_still_stops_created_containers(self):
        self.io.fail_at = "compose-up"
        self.assertEqual(self.up.up(), model.EXIT_FAIL)
        self.assertTrue(any(e == ("s6", "compose", "stop") for e in self.io.log))
        self.assertNotIn(("run", "monitoring-stop"), self.io.log)   # 모니터링은 시작 전이므로 되돌릴 것이 없다
        self.assertFalse(os.path.exists(self.state))

    def test_readiness_timeout_rolls_back(self):
        self.io.fail_at = "Prometheus 대상 up 확인 실패"
        self.assertEqual(self.up.up(), model.EXIT_FAIL)
        self.assertIn(("run", "monitoring-stop"), self.io.log)
        self.assertFalse(os.path.exists(self.state))

    def test_rules_mismatch_exit3_and_rollback(self):
        self.io.problems = ["slack-lab-r4: for 불일치"]
        self.assertEqual(self.up.up(), model.EXIT_RULES)
        self.assertFalse(os.path.exists(self.state))
        self.assertIn(("run", "monitoring-stop"), self.io.log)

    def test_rollback_continues_after_a_failing_step_and_reports(self):
        self.io.fail_at = "Grafana 준비 실패"
        orig = self.io.run
        def run(args, env=None):
            if args[1] == "stop":
                self.io.log.append(("run", "monitoring-stop"))
                return 1, ""
            return orig(args, env)
        self.io.run = run
        code = self.up.up()
        self.assertEqual(code, model.EXIT_FAIL)
        self.assertEqual(len([e for e in self.io.log if e[0] == "kill"]), 2)   # 앞 단계 정리는 계속됐다

    def test_preconditions_touch_nothing(self):
        for mutate in (lambda: self.io.env.pop("MANAGEMENT_METRICS_TOKEN"),
                       lambda: self.io.env.update(ALERT_CHANNEL="CSTUBVERIFY"),
                       lambda: self.io.busy.add(8080)):
            self.setUp()
            mutate()
            with self.assertRaises(ValueError):
                self.up.up()
            self.assertEqual(self.io.log, [])
        self.setUp()
        open(self.state, "w").close()
        with self.assertRaises(ValueError):
            self.up.up()
        self.assertEqual(self.io.log, [])


if __name__ == "__main__":
    unittest.main()
