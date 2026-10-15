"""B3 실행기의 경계·배선을 가상 환경으로 확인해 측정 중인 프로세스에 접근하지 않는다."""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _mon_alarm as alarm
import _mon_env as me
import _mon_model as model
import _mon_run as run

loader = importlib.machinery.SourceFileLoader("alarm_mv", os.path.join(os.path.dirname(__file__), "monitoring-verify"))
mv = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(mv)


class Clock:
    def __init__(self):
        self.t = 1000.
        self.hooks = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        for hook in self.hooks:
            hook()


class Slack:
    def __init__(self):
        self.counts, self.modes = {}, {}

    def count(self, path, channel):
        return self.counts.get(channel, 0)

    def mode(self, channel, mode):
        self.modes[channel] = mode


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.rows = []
        self.g = Mock()
        self.g.alert_view.return_value = ("", "normal", "ok", [])
        self.g.query_range.return_value = (200, {"data": {"result": []}})
        self.session = SimpleNamespace(jar="prebuilt.jar", data={"jar_delay": 3, "mode": "echo"}, interruptions=[])
        self.session.restart_app = Mock()
        self.slack = Slack()
        self.env = me.RealEnv(self.tmp.name, "session-project", self.g, self.slack, "ALARM", "http://receiver", "secret", "host", session=self.session)
        for target, replacement in (("_mon_alarm.logs", lambda _: list(self.rows)),
                                    ("_mon_env.time.time", self.clock.now), ("_mon_env.time.sleep", self.clock.sleep)):
            item = patch(target, replacement)
            item.start()
            self.addCleanup(item.stop)

    def snapshot(self, ready=0, dlq=0, recovery=0, ts=None):
        row = {"ts": self.clock.now() if ts is None else ts, "event": "backlog_snapshot",
               "fields": {"queue_ready": ready, "dlq": dlq, "recovery": recovery}}
        self.rows.append(row)
        return row

    def begin(self, rule="rule1"):
        self.env.started = self.clock.now()
        self.env.active = rule
        self.env.history, self.env.fired, self.env.visible = [], {}, {}
        self.env.report_at = None
        self.env.timeline = {}
        self.env.first = self.env.cleared = None
        self.env.variant = "fire"

    def test_rule1_failure_list_close_and_snapshot_hold(self):
        def inject(n):
            self.assertEqual(self.slack.modes["CSTUBVERIFY"], "fail")
            self.env.event_id = "EvSynthetic"
            self.rows.append({"ts": self.clock.now(), "msg": "발신 실패 event_id=EvSynthetic"})
            self.snapshot(dlq=1)
        self.env.inject_requests = inject
        calls = []
        def cli(jar, role, args, sdir, project):
            self.assertEqual((jar, role, project), ("prebuilt.jar", "recovery", "session-project"))
            calls.append((list(args), self.clock.now()))
            start = self.clock.now()
            self.clock.sleep(3)
            if args[0] == "close":
                self.snapshot()
            return 0, "dlq DEAD SEND ERROR 0 date EvSynthetic", (start, self.clock.now())
        with patch.object(me, "db_target_check", return_value=(True, [])), patch.object(me, "run_jar_cli", side_effect=cli):
            t0 = self.env.induce("rule1", "fire", 1)
            self.env.resolve("rule1", "fire", 1)
        self.assertEqual(t0, 1000)
        self.assertEqual(calls, [(["list"], 1000), (["close", "EvSynthetic"], 1047)])
        measured = self.env.measured("rule1")
        self.assertEqual(measured["h"], 50)
        self.assertNotIn("EvSynthetic", json.dumps(measured))
        self.assertEqual(self.slack.modes["CSTUBVERIFY"], "ok")

    def test_rule1_idle_has_no_failure_and_zero_h(self):
        self.env.normal_mentions = Mock()
        self.env.inject_requests = Mock(side_effect=AssertionError("유도하면 안 됨"))
        self.env.induce("rule1", "quiet", 1)
        self.env.resolve("rule1", "quiet", 1)
        self.env.normal_mentions.assert_called_once_with(5)
        self.assertEqual(self.env.measured("rule1")["h"], 0)

    def test_rule1_late_report_delays_close_and_measures_actual_snapshot_h(self):
        self.begin()
        self.env.first = self.snapshot(dlq=1)
        self.env.close_id = "synthetic"
        self.env.induce = lambda *args: 1000
        self.g.alert_view.side_effect = lambda title: (
            "", "firing" if title == "복구 필요" and self.clock.now() >= 1033 else "normal", "ok", [])
        self.clock.hooks.append(lambda: self.slack.counts.update(ALARM=int(self.clock.now() >= 1073)))
        closed = []

        def close(args):
            closed.append(self.clock.now())
            self.env.db_checked = self.clock.now()
            self.clock.sleep(3)
            self.snapshot()
            return "", (closed[-1], self.clock.now())

        self.env.cli = close
        result = run.run_round(self.env, "rule1", "fire", 1, 50, settle_s=1)
        self.assertEqual(closed, [1073])
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual((result["h"], result["hold_elapsed"], result["measured"]["h"]), (50, 73, 76))
        self.assertEqual(result["measured"]["timeline"]["close_finished"], 1076)
        self.assertEqual(result["report_elapsed"], 73)

    def test_rule2_fire_uses_log_timestamp_and_echo_restore(self):
        def inject(n):
            self.env.event_id = "synthetic"
            self.clock.sleep(155)
            self.rows.append({"ts": self.clock.now(), "event": "llm_final_failure"})
        self.env.inject_requests = inject
        self.env.failure_counts = lambda *args: True
        self.assertEqual(self.env.induce("rule2", "fire", 1), 1155)
        self.env.resolve("rule2", "fire", 1)
        self.assertEqual([c.args[0] for c in self.session.restart_app.call_args_list], ["F-echo", "echo"])

    def test_rule2_quiet_arms_after_restart(self):
        order = []
        self.session.restart_app.side_effect = lambda mode: order.append(mode)
        self.env.normal_mentions = lambda n: order.append(n)
        self.env.failure_counts = lambda *args: True
        self.env.event_id = "synthetic"
        with patch.object(alarm.StubClient, "request", side_effect=lambda *a: order.append("arm")):
            self.env.induce("rule2", "quiet", 2)
        self.assertEqual(order, ["F-echo-stub", "arm", 1])

    def test_failure_counts_require_retries_metrics_and_notice(self):
        self.begin("rule2")
        def row(event=None, kind=None, msg=""):
            return {"ts": 1000, "event": event, "fields": {"kind": kind}, "msg": "event_id=EvSynthetic " + msg}
        self.rows.extend(row("processing_metric", "-") for _ in range(3))
        self.rows.extend(row(msg="재시도 예약") for _ in range(3))
        self.rows.append(row("llm_final_failure"))
        self.assertFalse(self.env.failure_counts("F-echo", "EvSynthetic"))
        self.rows.append(row("processing_metric", "failure_notice"))
        self.assertTrue(self.env.failure_counts("F-echo", "EvSynthetic"))

    def test_snapshot_h_and_loki_visibility_bounds(self):
        self.begin()
        self.env.first = self.snapshot(dlq=1, ts=1000)
        self.env.cleared = self.snapshot(ts=1050)
        self.env.visible = {1000: 1002, 1050: 1054}
        result = self.env.measured("rule1")
        self.assertEqual((result["h"], result["d_s"], result["d_e"], result["x"]), (50, 2, 4, 52))
        self.env.visible = {}
        self.assertNotIn("d_s", self.env.measured("rule1"))

    def test_observe_captures_transient_and_unexpected_firing(self):
        self.begin("rule2")
        self.session.interruptions = [61]
        self.g.alert_view.side_effect = lambda title: ("", "firing" if title in ("LLM 최종 실패", "앱 관측 불가") else "normal", "ok", [])
        self.slack.counts["ALARM"] = 1
        self.env.observe()
        self.g.alert_view.side_effect = None
        self.env.observe()
        self.assertEqual(self.env.other_fired("rule2"), [("rule4-host", 61)])
        self.assertEqual(self.env.report_at, 1000)
        self.assertEqual(self.env.history[0], (1000, "firing"))

    def test_loki_first_visibility_is_not_replaced(self):
        self.begin()
        row = self.snapshot(dlq=1)
        self.g.query_range.return_value = (200, {"data": {"result": [{"values": [["unused", json.dumps(row)]]}]}})
        self.clock.sleep(2)
        self.env.observe()
        self.clock.sleep(5)
        self.env.observe()
        self.assertEqual(self.env.visible[1000], 1002)

    def test_rule3_nine_depth_is_quiet_and_changed_depth_rejected(self):
        self.begin("rule3")
        self.env.variant, self.env.n = "quiet", 9
        self.env.timeline = {"t2": 1130}
        self.env.first = self.snapshot(ready=9, ts=1000)
        self.snapshot(ready=9, ts=1120)
        self.env.cleared = self.snapshot(ts=1140)
        result = self.env.measured("rule3")
        self.assertEqual(result["h"], 0)
        self.assertTrue(result["quiet_ok"])
        self.snapshot(ready=10, ts=1125)
        self.rows.sort(key=lambda r: r["ts"])
        self.assertFalse(self.env.measured("rule3")["quiet_ok"])

    def test_rule3_quiet_resume_drain_then_normal_mentions(self):
        self.begin("rule3")
        self.env.variant, self.env.n = "quiet", 10
        self.env.first = self.snapshot(ready=10)
        self.env.queue_processed = lambda: True
        self.env.timeline = {"t1": 1000}
        open(self.env.flag, "w").close()
        order = []
        def hooks():
            if not os.path.exists(self.env.flag) and not order:
                order.append("resumed")
                self.rows.append({"ts": self.clock.now(), "msg": "검증 소비 제어 상태=RUNNING"})
                self.snapshot()
                self.rows.extend({"ts": self.clock.now(), "event": "processing_metric"} for _ in range(10))
        self.clock.hooks.append(hooks)
        self.env.normal_mentions = lambda n: order.append(("normal", n))
        self.env.resolve("rule3", "quiet", 2)
        self.assertEqual(order, ["resumed", ("normal", 5)])
        self.assertGreaterEqual(self.env.timeline["replies_done"], self.env.timeline["t3"])

    def test_recovery_wrong_database_never_executes_jar(self):
        with patch.object(me, "db_target_check", return_value=(False, [])), patch.object(me, "run_jar_cli") as cli:
            with self.assertRaises(RuntimeError):
                self.env.cli(["close", "synthetic"])
            cli.assert_not_called()


class WiringTests(unittest.TestCase):
    def test_run_writes_only_sanitized_results_and_returns_exit(self):
        with tempfile.TemporaryDirectory() as sdir, patch.object(me, "read_env_file", return_value={}), patch.object(alarm, "AlarmSession") as session, \
                patch.object(alarm.ms, "Grafana") as graf, patch.object(run, "run_rounds", return_value=(5, [{"verdict": "bound"}])) as rounds:
            graf.return_value.healthy.return_value = True
            graf.return_value.request.return_value = (200, [])
            with patch.object(model, "parity", return_value=[]):
                code = mv.cmd_run(["--mode", "host", "--scenario", "rule1-fire", "--session", sdir])
            self.assertEqual(code, 5)
            self.assertEqual(rounds.call_args.args[2], model.build_plan("rule1-fire"))
            with open(os.path.join(sdir, "run-rule1-fire.json")) as stream:
                self.assertEqual(json.load(stream)["exit"], 5)
            session.return_value.real_env.assert_called_once()

    def test_unsupported_real_runs_and_invalid_repeat(self):
        for scenario, mode in (("dashboard", "host"), ("app-stop", "app"), ("rule4-fire", "app")):
            self.assertEqual(mv.cmd_run(["--mode", mode, "--scenario", scenario]), 2)
        self.assertEqual(mv.cmd_run(["--mode", "host", "--scenario", "rule1-fire", "--repeat", "bad"]), 2)

    def test_alarm_up_requires_existing_session(self):
        self.assertEqual(mv.cmd_alarm_up(["--session", "missing-session"]), 2)

    def test_alarm_up_wiring(self):
        with tempfile.TemporaryDirectory() as sdir, patch.object(alarm, "AlarmSession") as session:
            with open(os.path.join(sdir, "ledger.json"), "w") as stream:
                json.dump({}, stream)
            session.return_value.up.return_value = 3
            self.assertEqual(mv.cmd_alarm_up(["--session", sdir]), 3)


class ManagedRoundTests(unittest.TestCase):
    def environment(self, quiet=False):
        clock = Clock()
        env = SimpleNamespace(manages_hold=True, now=clock.now, sleep=clock.sleep, first=True,
                              report_at=1020, history=[(1010, "firing"), (1030, "normal")])
        env.induce = lambda *a: 1000
        env.resolve = lambda *a: clock.sleep(50)
        env.measured = lambda *a: {"h": 20 if quiet else 50}
        env.state = lambda *a: "normal"
        env.reports = lambda *a: 0 if quiet else 1
        env.other_fired = lambda *a: []
        return env

    def test_firing_during_hold_uses_first_report_time(self):
        result = run.run_round(self.environment(), "rule1", "fire", 1, 50)
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["report_elapsed"], 20)
        self.assertIn((10, "firing"), result["states"])

    def test_quiet_rejects_transient_firing(self):
        result = run.run_round(self.environment(True), "rule1", "quiet", 1, 20)
        self.assertEqual(result["verdict"], "fail")

    def test_induction_failure_stops_and_cleans_up(self):
        env = self.environment()
        env.induce = Mock(side_effect=RuntimeError("비밀 원문"))
        env.cleanup = Mock()
        code, results = run.run_rounds(env, "rule1", [("fire", 50)] * 5)
        self.assertEqual(code, model.EXIT_FAIL)
        self.assertEqual(len(results), 1)
        self.assertNotIn("비밀 원문", json.dumps(results))
        env.cleanup.assert_called_once()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sdir = self.tmp.name
        self.path = os.path.join(self.sdir, "ledger.json")
        with open(self.path, "w") as stream:
            json.dump({"project": "slack-lab-s6v-test", "pids": []}, stream)
        with open(os.path.join(self.sdir, "jar.json"), "w") as stream:
            json.dump({"jar": "prebuilt.jar"}, stream)
        self.values = {key: "dummy" for key in ("ALERT_CHANNEL", "ALERT_SECRET", "SLACK_SIGNING_SECRET",
                                                "GRAFANA_ADMIN_PASSWORD", "MANAGEMENT_METRICS_TOKEN")}
        env_patch = patch.object(me, "read_env_file", return_value=self.values)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.mv = Mock()
        self.mv.s6.return_value.stdout = "123"
        self.mv.lsof_listen.return_value = ""
        self.session = alarm.AlarmSession(self.sdir, self.mv)

    def test_up_preserves_secret_and_binds_slack_loopback(self):
        self.session.slack = Mock(base="http://127.0.0.1:18962")
        self.session.spawn = Mock()
        self.session.restart_app = Mock()
        self.session.wait = Mock()
        with patch.object(alarm.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as command, \
                patch.object(me, "db_target_check", return_value=(True, [])), \
                patch.object(me, "run_jar_cli", return_value=(0, "미해결 건 없음", (1, 4))), \
                patch.object(alarm, "parity", return_value=[]):
            self.assertEqual(self.session.up(), 0)
        self.assertEqual(self.session.data["jar_delay"], 3)
        command_env = command.call_args.kwargs["env"]
        self.assertEqual(command_env["GRAFANA_ALERT_WEBHOOK_URL"], "http://host.docker.internal:8080/alerts/grafana")
        self.assertEqual(command_env["SLACK_LAB_LOG_DIR_HOST"], self.sdir)
        self.assertNotIn("dummy", str(command.call_args.args))
        self.assertNotIn("dummy", json.dumps(self.session.data))
        commands = [c.args[1] for c in self.session.spawn.call_args_list]
        self.assertEqual([c[-1] for c in commands], ["18962", "18963"])
        self.assertTrue(all("--host" not in c for c in commands))
        self.mv.s6.assert_any_call("compose", "--session", self.sdir, "--", "up", "-d", "--wait", "rabbitmq", "postgres")

    def test_up_refuses_busy_port_before_infrastructure(self):
        self.mv.lsof_listen.return_value = "busy"
        with self.assertRaises(ValueError):
            self.session.up()
        self.mv.s6.assert_not_called()

    def test_restart_registers_jvm_without_infrastructure_change(self):
        self.session.spawn = Mock()
        self.session.wait = Mock()
        with patch.object(alarm, "logs", return_value=[]), \
                patch.object(alarm.subprocess, "run", return_value=SimpleNamespace(stdout="456\n")):
            self.session.restart_app("F-echo")
        settings = self.session.spawn.call_args.args[2]
        self.assertEqual(settings["LLM_CLIENT"], "ollama")
        self.assertEqual(settings["LLM_BASE_URL"], "http://localhost:9/v1")
        self.assertEqual(settings["RETRY_MAXRETRIES"], "3")
        self.assertEqual(settings["EXPERIMENT_HALTAFTERSEND"], "false")
        self.assertEqual(self.session.spawn.call_args.args[1], ["./gradlew", "verifyBootRun", "-q"])
        self.mv.s6.assert_not_called()
        with open(self.path) as stream:
            entry = json.load(stream)["pids"][-1]
        self.assertEqual((entry["pid"], entry["signal_pid"]), (456, True))

    def test_restore_signals_only_owned_jvm(self):
        self.session.data["jvm_pid"] = 456
        with open(self.path, "w") as stream:
            json.dump({"project": "slack-lab-s6v-test", "pids": [{"pid": 456, "name": "alarm-app-jvm", "signal_pid": True}]}, stream)
        self.session.spawn = Mock()
        self.session.wait = Mock()
        with patch.object(alarm, "logs", return_value=[]), patch.object(alarm.os, "kill") as kill, \
                patch.object(alarm.subprocess, "run", return_value=SimpleNamespace(stdout="789\n")):
            self.session.restart_app("echo")
        kill.assert_called_once_with(456, alarm.signal.SIGTERM)
        self.assertEqual(self.session.data["jvm_pid"], 789)
        self.assertEqual(self.session.data["mode"], "echo")
        self.assertEqual(len(self.session.interruptions), 1)

    def test_incomplete_state_cannot_create_real_environment(self):
        with self.assertRaises(ValueError):
            self.session.real_env()


if __name__ == "__main__":
    unittest.main()
