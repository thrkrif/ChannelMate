"""B4 앱 모드 ④ 유도·해소와 앱 30초 중단을 가짜 명령·가상 시계로 확인한다(컨테이너·네트워크 접근 0)."""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import _mon_app as app
import _mon_env as me
import _mon_model as model
import _mon_run as run


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


class Fake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.calls = []
        self.fails = set()
        self.targets = os.path.join(self.tmp.name, "targets.json")
        self.original = [{"targets": [f"{r}:9091"], "labels": {"role": r}} for r in app.ROLES]
        with open(self.targets, "w") as stream:
            json.dump(self.original, stream)
        self.slack = Slack()
        self.g = Mock()
        self.g.alert_view.return_value = ("", "normal", "ok", [])
        self.g.query.side_effect = self.query
        self.up = {r: 1.0 for r in app.ROLES}
        self.env = app.AppEnv(self.tmp.name, "proj", self.g, self.slack, "ALARM", "http://receiver", "secret", "app",
                              session=Mock(), cmd=self.cmd, targets_path=self.targets)
        for target, replacement in (("_mon_alarm.logs", lambda _: []), ("_mon_env.time.time", self.clock.now),
                                    ("_mon_env.time.sleep", self.clock.sleep)):
            item = patch(target, replacement)
            item.start()
            self.addCleanup(item.stop)

    def cmd(self, args):
        self.calls.append((self.clock.now(), args))
        key = " ".join(args[:3])
        if any(f in " ".join(args) for f in self.fails):
            return 1, ""
        if args[:2] == ["docker", "ps"]:
            service = next(a.split("=")[-1] for a in args if a.startswith("label=com.docker.compose.service="))
            return 0, "c-" + service + "\n"
        if args[:3] == ["docker", "network", "disconnect"]:
            self.up[self.role_of(args[-1])] = "absent"
        if args[:3] == ["docker", "network", "connect"]:
            self.up[self.role_of(args[-1])] = 1.0
        if args[:2] == ["docker", "stop"]:
            self.up[self.role_of(args[-1])] = 0.0
        if args[:2] == ["docker", "start"]:
            self.up[self.role_of(args[-1])] = 1.0
        return 0, ""

    @staticmethod
    def role_of(container):
        return container[2:]

    def query(self, ds, expr, t=None, path=None):
        role = expr.split('role="')[1].split('"')[0]
        # 대상 파일에서 빠진 역할은 시계열이 사라진다.
        with open(self.targets) as stream:
            present = {r["labels"]["role"] for r in json.load(stream)}
        value = self.up[role] if role in present else "absent"
        result = [] if value == "absent" else [{"value": [0, str(value)]}]
        return 200, {"data": {"result": result}}

    def fire_after(self, start_state, seconds):
        self.g.alert_view.side_effect = lambda title: (
            "", "firing" if title == "앱 관측 불가" and start_state() is not None
            and self.clock.now() >= start_state() + seconds else "normal", "ok", [])


class BlockTests(Fake):
    def test_block_fire_cuts_holds_and_reconnects_with_alias(self):
        t0 = self.env.induce("rule4-block", "fire", 1)
        self.assertEqual(t0, 1000)
        self.assertIn((1000, ["docker", "network", "disconnect", "proj_scrape", "c-receiver"]), self.calls)
        self.env.resolve("rule4-block", "fire", 1)
        connect = [c for t, c in self.calls if c[:3] == ["docker", "network", "connect"]]
        self.assertEqual(connect, [["docker", "network", "connect", "--alias", "receiver", "proj_scrape", "c-receiver"]])
        self.assertGreaterEqual(self.clock.t, 1000 + model.RULES["rule4-block"]["h_run"])
        m = self.env.measured("rule4-block")
        self.assertEqual(m["h"], 95)
        self.assertEqual(m["timeline"]["role"], "receiver")
        self.assertTrue(model.in_bounds("rule4-block", m["d_s"], m["d_e"]))

    def test_roles_follow_plan_order(self):
        self.assertEqual([app.role_for("rule4-block", i) for i in range(1, 6)],
                         ["receiver", "worker", "reactor", "receiver", "worker"])
        self.assertEqual([app.role_for("rule4-absent", i) for i in range(1, 4)], ["receiver", "worker", "reactor"])

    def test_full_fire_round_passes_within_budget(self):
        self.fire_after(lambda: self.env.cut_at, 70)
        self.clock.hooks.append(lambda: self.slack.counts.update(
            ALARM=int(self.env.cut_at is not None and self.clock.now() >= self.env.cut_at + 80)))
        r = run.run_round(self.env, "rule4-block", "fire", 1, 95, settle_s=1)
        self.assertEqual(r["verdict"], "pass", r)
        self.assertLessEqual(r["report_elapsed"], 103)
        self.assertEqual(r["measured"]["timeline"]["role"], "receiver")

    def test_late_report_fails(self):
        self.fire_after(lambda: self.env.cut_at, 70)
        self.clock.hooks.append(lambda: self.slack.counts.update(
            ALARM=int(self.env.cut_at is not None and self.clock.now() >= self.env.cut_at + 110)))
        r = run.run_round(self.env, "rule4-block", "fire", 1, 95, settle_s=1)
        self.assertEqual(r["verdict"], "fail")

    def test_quiet_odd_has_no_cut_and_mixes_mentions(self):
        self.env.normal_mentions = Mock()
        self.env.induce("rule4-block", "quiet", 1)
        self.env.resolve("rule4-block", "quiet", 1)
        self.env.normal_mentions.assert_called_once_with(5)
        self.assertFalse([c for _, c in self.calls if "network" in c])
        self.assertEqual(self.clock.t, 1000 + app.QUIET_BLOCK_A)

    def test_quiet_even_cuts_for_40_seconds(self):
        self.env.normal_mentions = Mock()
        self.env.induce("rule4-block", "quiet", 2)
        self.env.resolve("rule4-block", "quiet", 2)
        self.assertEqual(self.env.measured("rule4-block")["h"], 40)
        self.assertEqual(self.env.cut, None)
        self.assertEqual(self.env.timeline["role"], "worker")

    def test_cleanup_reconnects_after_exception(self):
        self.env.induce("rule4-block", "fire", 1)
        self.fails = set()
        self.env.cleanup()
        self.assertTrue([c for _, c in self.calls if c[:3] == ["docker", "network", "connect"]])
        self.assertIsNone(self.env.cut)
        self.assertEqual(self.slack.modes["CSTUBVERIFY"], "ok")

    def test_failed_cleanup_is_reported(self):
        self.env.induce("rule4-block", "fire", 1)
        self.fails = {"network connect"}
        with self.assertRaises(RuntimeError):
            self.env.cleanup()

    def test_duplicate_container_refuses(self):
        with patch.object(self.env, "cmd", return_value=(0, "a\nb\n")):
            with self.assertRaises(RuntimeError):
                self.env.induce("rule4-block", "fire", 1)


class AbsentTests(Fake):
    def test_absent_removes_only_one_role_and_restores_file(self):
        self.env.induce("rule4-absent", "fire", 2)
        with open(self.targets) as stream:
            roles = [r["labels"]["role"] for r in json.load(stream)]
        self.assertEqual(roles, ["receiver", "reactor"])
        self.env.resolve("rule4-absent", "fire", 2)
        with open(self.targets) as stream:
            self.assertEqual(json.load(stream), self.original)
        self.assertEqual(self.env.measured("rule4-absent")["h"], 120)
        self.assertFalse([c for _, c in self.calls if "network" in c])

    def test_cleanup_restores_file_after_exception(self):
        self.env.induce("rule4-absent", "fire", 1)
        self.env.cleanup()
        with open(self.targets) as stream:
            self.assertEqual(json.load(stream), self.original)

    def test_missing_role_entry_refuses_without_writing(self):
        with open(self.targets, "w") as stream:
            json.dump(self.original[:2], stream)
        with self.assertRaises(RuntimeError):
            self.env.induce("rule4-absent", "fire", 3)


class StopTests(Fake):
    def prepare(self, replies=1):
        self.env.inject_requests = lambda n: self.slack.counts.update(CSTUBVERIFY=self.slack.counts.get("CSTUBVERIFY", 0) + replies)
        self.env.health = lambda: True

    def test_stop_30s_start_and_single_reply_passes(self):
        self.prepare()
        code, results = app.run_app_stop(self.env)
        self.assertEqual(code, model.EXIT_OK, results)
        stops = [t for t, c in self.calls if c[:2] == ["docker", "stop"]]
        starts = [t for t, c in self.calls if c[:2] == ["docker", "start"]]
        self.assertEqual(len(stops), 3)
        self.assertEqual(len(starts), 3)
        self.assertGreaterEqual(min(starts) - max(stops), app.STOP_S)
        self.assertEqual(results[0]["replies"], 1)

    def test_duplicate_reply_fails(self):
        self.prepare(replies=2)
        code, results = app.run_app_stop(self.env)
        self.assertEqual(code, model.EXIT_FAIL)
        self.assertIn("답글=2", results[0]["reason"])

    def test_no_recovery_in_180_seconds_fails(self):
        self.prepare()
        self.env.all_up = lambda: False
        code, results = app.run_app_stop(self.env)
        self.assertEqual(code, model.EXIT_FAIL)
        self.assertIn("180", results[0]["reason"])

    def test_start_failure_still_restarts_stopped_roles(self):
        self.prepare()
        calls_before = len(self.calls)
        original = self.cmd
        def flaky(args):
            if args[:2] == ["docker", "start"] and "c-worker" in args and not getattr(self, "tried", False):
                self.tried = True
                self.calls.append((self.clock.now(), args))
                return 1, ""
            return original(args)
        self.env.cmd = flaky
        code, results = app.run_app_stop(self.env)
        self.assertEqual(code, model.EXIT_FAIL)
        self.assertEqual(results[0]["verdict"], "induction")
        restarted = {c[-1] for _, c in self.calls[calls_before:] if c[:2] == ["docker", "start"]}
        self.assertEqual(restarted, {"c-receiver", "c-worker", "c-reactor"})

    def test_double_rule4_firing_fails(self):
        self.prepare()
        seq = iter(["firing", "normal", "firing"] + ["normal"] * 400)
        self.g.alert_view.side_effect = lambda title: ("", next(seq) if title == "앱 관측 불가" else "normal", "ok", [])
        self.slack.counts["ALARM"] = 0
        code, results = app.run_app_stop(self.env)
        self.assertEqual(code, model.EXIT_FAIL)
        self.assertGreaterEqual(results[0]["rule4_firings"], 2)


class CalibrationTests(Fake):
    """실제 차단 보정 경로: 가짜 명령·가상 시계. 지연은 query 응답 시각으로 흉내 낸다."""

    def delayed(self, d_s, d_e):
        base = self.query
        marks = {}

        def query(ds, expr, t=None, path=None):
            role = expr.split('role="')[1].split('"')[0]
            code, body = base(ds, expr)
            cut, back = self.env.cut_at, self.env.restore_at
            if cut is not None and back is None and self.clock.now() < cut + d_s:
                body = {"data": {"result": [{"value": [0, "1.0"]}]}}      # 아직 down이 안 보임
            if back is not None and self.clock.now() < back + d_e:
                body = {"data": {"result": []}}                            # 아직 up이 안 보임
            return code, body
        self.g.query.side_effect = query

    def test_exactly_three_real_samples_with_role_restore(self):
        code, result = app.collect_calibration(self.env, "rule4-block")
        self.assertEqual(code, model.EXIT_OK)
        self.assertTrue(result["measured"])
        self.assertEqual([r["role"] for r in result["samples"]], ["receiver", "worker", "reactor"])
        self.assertEqual(len(result["samples"]), app.CAL_SAMPLES)
        self.assertEqual(result["h"], 95)
        disconnects = [c[-1] for _, c in self.calls if c[:3] == ["docker", "network", "disconnect"]]
        connects = [c[-1] for _, c in self.calls if c[:3] == ["docker", "network", "connect"]]
        self.assertEqual(disconnects, ["c-receiver", "c-worker", "c-reactor"])
        self.assertEqual(connects, disconnects)        # 역할별 복원, 순서도 같다
        self.assertIsNone(self.env.cut)

    def test_absent_restores_targets_each_sample(self):
        code, result = app.collect_calibration(self.env, "rule4-absent")
        self.assertEqual(code, model.EXIT_OK)
        self.assertEqual(result["h"], 120)
        with open(self.targets) as stream:
            self.assertEqual(json.load(stream), self.original)
        self.assertFalse([c for _, c in self.calls if "network" in c])

    def test_bound_exceeded_stops_without_more_samples_and_restores(self):
        self.delayed(d_s=20, d_e=0)                      # 차단 D_s 경계 15 초과
        code, result = app.collect_calibration(self.env, "rule4-block")
        self.assertEqual(code, model.EXIT_BOUND)
        self.assertEqual(len(result["samples"]), 1)
        self.assertIsNone(result["h"])
        self.assertIsNone(self.env.cut)                  # 중단했어도 복원됨
        self.assertEqual(len([c for _, c in self.calls if c[:3] == ["docker", "network", "disconnect"]]), 1)

    def test_late_up_visibility_is_bound_not_zero(self):
        self.delayed(d_s=0, d_e=40)                      # D_e 경계 15(+유예 5) 초과
        code, result = app.collect_calibration(self.env, "rule4-block")
        self.assertEqual(code, model.EXIT_BOUND)
        self.assertNotIn("d_e", result["samples"][0])

    def test_restore_failure_stops_as_observe(self):
        self.fails = {"network connect"}
        code, result = app.collect_calibration(self.env, "rule4-block")
        self.assertEqual(code, model.EXIT_OBSERVE)
        self.assertEqual(len(result["samples"]), 1)

    def test_samples_flag_is_not_accepted_as_calibration(self):
        with open(os.path.join(self.tmp.name, "calibration-rule4-block.json"), "w") as stream:
            json.dump({"rule": "rule4-block", "measured": False, "exit": 0, "h": 95, "samples": [{"d_s": 1, "d_e": 1}] * 3}, stream)
        self.assertIsNone(app.calibration_h(self.tmp.name, "rule4-block"))

    def test_only_complete_measured_result_is_accepted(self):
        code, result = app.collect_calibration(self.env, "rule4-block")
        path = os.path.join(self.tmp.name, "calibration-rule4-block.json")
        with open(path, "w") as stream:
            json.dump(result, stream)
        self.assertEqual(app.calibration_h(self.tmp.name, "rule4-block"), 95)
        result["samples"] = result["samples"][:2]
        with open(path, "w") as stream:
            json.dump(result, stream)
        self.assertIsNone(app.calibration_h(self.tmp.name, "rule4-block"))
        self.assertIsNone(app.calibration_h(self.tmp.name, "rule4-absent"))

    def test_unsupported_rule_rejected(self):
        with self.assertRaises(ValueError):
            app.collect_calibration(self.env, "rule1")

    def test_hold_override_used_in_fire_resolve(self):
        self.env.h_override = 105
        self.env.induce("rule4-block", "fire", 1)
        self.env.resolve("rule4-block", "fire", 1)
        self.assertEqual(self.env.measured("rule4-block")["h"], 105)


if __name__ == "__main__":
    unittest.main()
