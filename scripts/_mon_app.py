"""B4 앱 모드 ④(앱 관측 불가) 유도·해소와 앱 30초 중단(C9.10) 환경.
명령 실행기(cmd)를 주입받아 단위 시험이 가짜 명령으로 조작 순서를 확인한다. 이 모듈은 컨테이너를 스스로 만들지 않고
이미 떠 있는 세션 프로젝트의 컨테이너에 연결·중지·시작만 한다(기동은 장부를 쓰는 s6-session 경로 몫).
"""
import json
import os
import subprocess

import _mon_env as me
import _mon_model as model
import _mon_run as run

ROLES = ("receiver", "worker", "reactor")
BLOCK_ROLES = ("receiver", "worker", "reactor", "receiver", "worker")  # 계획서: 수신→워커→반응→수신→워커
STOP_S = 30          # C9.10 중단 시간
RECOVER_S = 180      # C9.10 복구 상한
QUIET_BLOCK_A = 150  # 비발화 (a): 차단 없이 유지
QUIET_BLOCK_B = 40   # 비발화 (b): 차단 40초
CAL_SAMPLES = 3      # 보정 표본 수(정확히 3)
CAL_GRACE_S = 5      # 경계 밖 판정 전 관측 유예
CAL_ABSENT_E_S = 60  # 부재의 D_e는 모형 상한이 없어 관측 대기 상한을 따로 둔다


def real_cmd(args, timeout=60):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout


def role_for(rule, round_no):
    if rule == "rule4-absent":
        return ROLES[(round_no-1) % len(ROLES)]
    return BLOCK_ROLES[(round_no-1) % len(BLOCK_ROLES)]


class AppEnv(me.RealEnv):
    """④ 전용. 상태·리포트·관측은 RealEnv를 그대로 쓰고 유도·해소·측정만 바꾼다."""

    log_selector = '{app="slack-lab",role=~"receiver|worker|reactor"}'

    def __init__(self, *args, cmd=real_cmd, targets_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cmd, self.targets_path = cmd, targets_path
        self.cut = None          # (역할, 컨테이너) 차단 중인 대상
        self.removed = None      # 대상 파일에서 뺀 역할 항목 원본
        self.original_targets = None
        self.role = None
        self.up_seen = {}
        self.cut_at = self.restore_at = None
        self.h_override = None   # 실측 보정으로 확정한 발화 유지 시간(없으면 모형 값)

    def rows(self):
        from _mon_alarm import logs, timestamp
        # 역할별 마운트에 기록되므로 호스트용 all 경로로는 앱 사건을 관측할 수 없다.
        return [r for r in logs(self.sdir, ROLES) if timestamp(r["ts"]) >= self.started]

    # --- 명령 보조 -------------------------------------------------------------------------------------
    def containers(self, role, all_states=False):
        rc, out = self.cmd(["docker", "ps", *(["-a"] if all_states else []), "-q",
                            "--filter", f"label=com.docker.compose.project={self.project}",
                            "--filter", f"label=com.docker.compose.service={role}"])
        ids = out.split()
        if rc or len(ids) != 1:
            raise RuntimeError(f"{role} 컨테이너 확인 실패(정확히 1개여야 한다)")
        return ids[0]

    def scrape_net(self):
        return self.project + "_scrape"

    def disconnect(self, role):
        cid = self.containers(role)
        rc, _ = self.cmd(["docker", "network", "disconnect", self.scrape_net(), cid])
        if rc:
            raise RuntimeError("scrape 네트워크 차단 실패")
        self.cut = (role, cid)

    def reconnect(self):
        if not self.cut:
            return
        role, cid = self.cut
        # 별칭이 없으면 Prometheus가 역할 이름으로 대상을 못 찾아 복구가 거짓으로 실패한다.
        rc, _ = self.cmd(["docker", "network", "connect", "--alias", role, self.scrape_net(), cid])
        if rc:
            raise RuntimeError("scrape 네트워크 복구 실패")
        self.cut = None

    def read_targets(self):
        with open(self.targets_path, encoding="utf-8") as stream:
            return json.load(stream)

    def write_targets(self, data):
        tmp = self.targets_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(tmp, self.targets_path)

    def remove_target(self, role):
        data = self.read_targets()
        kept = [row for row in data if row.get("labels", {}).get("role") != role]
        if len(kept) != len(data) - 1:
            raise RuntimeError("대상 파일에서 역할 항목을 정확히 1개 찾지 못함")
        self.original_targets = data
        self.write_targets(kept)
        self.removed = role

    def restore_targets(self):
        if self.original_targets is not None:
            self.write_targets(self.original_targets)
            self.original_targets = None
            self.removed = None

    # --- up 가시화 관측(D_s·D_e) -------------------------------------------------------------------------
    def up_value(self, role):
        code, body = self.g.query("prom", f'up{{job="slack-lab",role="{role}"}}')
        if code != 200 or not isinstance(body, dict):
            return None
        result = body.get("data", {}).get("result", [])
        return float(result[0]["value"][1]) if result else "absent"

    def observe(self):
        super().observe()
        if not self.active or not self.role:
            return
        now = self.now()
        value = self.up_value(self.role)
        if self.cut_at is not None and "down" not in self.up_seen and value in (0.0, "absent"):
            self.up_seen["down"] = now
        if self.restore_at is not None and "up" not in self.up_seen and value == 1.0:
            self.up_seen["up"] = now

    # --- 인터페이스 -----------------------------------------------------------------------------------
    def induce(self, rule, variant, round_no):
        if rule not in ("rule4-block", "rule4-absent"):
            raise ValueError("앱 모드에서 지원하지 않는 규칙")
        self.started = self.now()
        self.history, self.fired, self.visible = [], {}, {}
        self.report_at = None
        # observe()는 active(=rule4)로, 실행기는 시나리오 규칙 이름으로 리포트 수를 읽으므로 기준을 둘 다 둔다.
        self.base_reports[rule] = self.base_reports["rule4"] = self.slack.count("chat.postMessage", self.channel)
        self.active = "rule4"
        self.variant, self.round_no = variant, round_no
        self.first = self.cleared = None
        self.mention_ids = set()
        self.timeline = {}
        self.up_seen, self.cut_at, self.restore_at = {}, None, None
        self.role = role_for(rule, round_no)
        self.timeline["role"] = self.role
        self.slack.mode("CSTUBVERIFY", "ok")
        if variant == "quiet" and round_no % 2:
            # (a) 차단 없이 유지: 정상 멘션 5건을 유지 구간 안에 섞는다(①②④ 스펙 순서).
            self.normal_mentions(5)
            self.timeline["t_start"] = self.now()
            return self.now()
        if rule == "rule4-absent":
            self.remove_target(self.role)
        else:
            self.disconnect(self.role)
        self.cut_at = self.now()
        self.timeline["t_cut"] = self.cut_at
        if variant == "quiet":
            self.normal_mentions(5)
        return self.cut_at

    def resolve(self, rule, variant, round_no):
        hold = (self.h_override or model.RULES[rule]["h_run"]) if variant == "fire" else (QUIET_BLOCK_A if round_no % 2 else QUIET_BLOCK_B)
        start = self.cut_at if self.cut_at is not None else self.timeline.get("t_start", self.now())
        self.sleep(max(0, start + hold - self.now()))
        if self.cut_at is not None:
            self.restore_at = self.now()
            self.timeline["t_restore"] = self.restore_at
            if rule == "rule4-block":
                self.reconnect()
            else:
                self.restore_targets()
            # 복구 뒤 up=1 가시화와 Normal 복귀를 기다린다. 못 보면 다음 회차를 막는 것이 맞다.
            self.wait(lambda: "up" in self.up_seen, 90, "복구 뒤 up=1 가시화 확인 실패")
        self.observe()

    # --- 차단 보정(session calibrate): 실제 차단·복구로 D_s·D_e 표본 하나를 잰다 ---------------------------------------
    def seen(self, key, seconds):
        end = self.now() + seconds
        while self.now() <= end:
            self.observe()
            if key in self.up_seen:
                return True
            self.sleep(1)
        return key in self.up_seen

    def calibrate_sample(self, rule, round_no):
        """표본 하나. 반환 {'role','d_s','d_e'} 또는 {'role','bound':사유}. 어떤 경우에도 cleanup()으로 그 역할을 복원한다.
        관측하지 못한 지연은 경계(D_s,max·D_e,max + 5초 유예)를 넘은 것으로 본다 — 못 본 값을 0으로 채우지 않는다."""
        spec = model.RULES[rule]
        self.started = self.now()
        self.history, self.fired, self.visible = [], {}, {}
        self.report_at = None
        self.base_reports["rule4"] = self.slack.count("chat.postMessage", self.channel)
        self.active = "rule4"
        self.role = role_for(rule, round_no)
        self.up_seen, self.cut_at, self.restore_at = {}, None, None
        out = {"role": self.role}
        try:
            if rule == "rule4-absent":
                self.remove_target(self.role)
            else:
                self.disconnect(self.role)
            self.cut_at = self.now()
            if not self.seen("down", spec["d_s_max"] + CAL_GRACE_S):
                out["bound"] = "D_s 관측 실패 또는 경계 초과"
                return out
            out["d_s"] = self.up_seen["down"] - self.cut_at
            self.restore_at = self.now()
            if rule == "rule4-absent":
                self.restore_targets()
            else:
                self.reconnect()
            if not self.seen("up", (spec["d_e_max"] or CAL_ABSENT_E_S) + CAL_GRACE_S):
                out["bound"] = "D_e 관측 실패 또는 경계 초과"
                return out
            out["d_e"] = self.up_seen["up"] - self.restore_at
            return out
        finally:
            self.cleanup()

    def measured(self, rule):
        result = {"h": 0, "timeline": self.timeline}
        if self.cut_at is not None and self.restore_at is not None:
            result["h"] = self.restore_at - self.cut_at
            if "down" in self.up_seen:
                result["d_s"] = self.up_seen["down"] - self.cut_at
            if "up" in self.up_seen:
                result["d_e"] = self.up_seen["up"] - self.restore_at
            if "d_s" in result and "d_e" in result:
                result["x"] = model.x_len(result["h"], result["d_s"], result["d_e"])
        return result

    def other_fired(self, rule):
        return [(key, None) for key in self.fired if key != "rule4"]

    def cleanup(self):
        """예외가 났어도 차단·대상 제거를 되돌린다. 되돌리기 실패는 호출자에게 알려 회차를 멈춘다."""
        errors = []
        for step in (self.reconnect, self.restore_targets):
            try:
                step()
            except (RuntimeError, OSError, ValueError) as e:
                errors.append(str(e))
        self.slack.mode("CSTUBVERIFY", "ok")
        self.active = None
        self.role = None
        if errors:
            raise RuntimeError("; ".join(errors))

    # --- 앱 30초 중단 (C9.10) ----------------------------------------------------------------------------
    def all_up(self):
        for role in ROLES:
            if self.up_value(role) != 1.0:
                return False
        return True

    def app_stop(self):
        """역할 3개를 STOP_S초 중지했다가 시작한다. 반환 결과 딕트(verdict pass|fail|observe|induction)."""
        res = {"rule": None, "variant": "stop", "round": 1, "verdict": None, "reason": None}
        self.started = self.now()
        self.history, self.fired, self.visible = [], {}, {}
        self.report_at, self.role = None, None
        self.base_reports["rule4"] = self.slack.count("chat.postMessage", self.channel)
        self.active = "rule4"
        self.mention_ids = set()
        self.timeline = {}
        self.slack.mode("CSTUBVERIFY", "ok")
        stopped = []
        try:
            ids = {role: self.containers(role) for role in ROLES}
            for role in ROLES:
                rc, _ = self.cmd(["docker", "stop", ids[role]])
                if rc:
                    raise RuntimeError("앱 역할 중지 실패")
                stopped.append(role)
            t_stop = self.now()
            self.sleep(STOP_S)
            t_start = self.now()
            res["stop_s"] = round(t_start - t_stop, 1)
            for role in ROLES:
                rc, _ = self.cmd(["docker", "start", ids[role]])
                if rc:
                    raise RuntimeError("앱 역할 시작 실패")
            stopped.clear()
            self.timeline.update(t_stop=t_stop, t_start=t_start)
            end = t_start + RECOVER_S
            while not self.all_up():
                if self.now() > end:
                    res.update(verdict="fail", reason=f"{RECOVER_S}초 안에 전 역할 up=1 복구 실패")
                    return res
                self.sleep(1)
            res["recover_s"] = round(self.now() - t_start, 1)
            # 답글 중복 0: 복구 뒤 합성 멘션 1건에 답글이 정확히 1개여야 한다(소비자 재연결 뒤 중복 전달 검사).
            self.wait(lambda: self.health(), 90, "수신 health 복구 확인 실패")
            base = self.slack.count("chat.postMessage", "CSTUBVERIFY")
            self.inject_requests(1)
            self.wait(lambda: self.slack.count("chat.postMessage", "CSTUBVERIFY") >= base + 1, 60, "복구 뒤 답글 누락")
            self.sleep(5)
            replies = self.slack.count("chat.postMessage", "CSTUBVERIFY") - base
            res["replies"] = replies
            firings = sum(1 for prev, cur in zip([("", "normal")] + self.history, self.history)
                          if cur[1] == "firing" and prev[1] != "firing")
            posts = self.reports("rule4")
            res["rule4_firings"], res["reports"] = firings, posts
            ok = replies == 1 and firings <= 1 and posts <= 1
            res.update(verdict="pass" if ok else "fail",
                       reason=None if ok else f"답글={replies} ④발화={firings} 리포트={posts}")
            return res
        finally:
            # 중간 실패에도 중지한 역할은 되살린다(컨테이너가 꺼진 채 남으면 다음 시나리오 전체가 오염된다).
            for role in stopped:
                try:
                    self.cmd(["docker", "start", self.containers(role, all_states=True)])
                except (RuntimeError, OSError):
                    pass
            self.active = None

    def health(self):
        import urllib.request
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=3) as response:
                return response.status == 200
        except OSError:
            return False


def run_app_stop(env):
    """app-stop 시나리오 한 번. 반환 (종료 코드, 결과 목록). 예외도 결과로 남기고 정리한다."""
    try:
        r = env.app_stop()
    except run.ObservationError:
        r = {"variant": "stop", "round": 1, "verdict": "observe", "reason": "관측 오류"}
    except (RuntimeError, OSError, ValueError) as e:
        r = {"variant": "stop", "round": 1, "verdict": "induction", "reason": f"중단·복구 조작 실패({type(e).__name__})"}
    code = {"pass": model.EXIT_OK, "observe": model.EXIT_OBSERVE}.get(r["verdict"], model.EXIT_FAIL)
    return code, [r]


class AppSession:
    """앱 모드 세션 정보. alarm-state와 달리 앱 기동은 이 모듈이 하지 않고, 세션이 기록한 app-state.json만 읽는다."""

    def __init__(self, sdir):
        import _mon_spike as ms
        self.sdir = os.path.abspath(sdir)
        with open(os.path.join(sdir, "ledger.json"), encoding="utf-8") as stream:
            self.project = json.load(stream)["project"]
        self.env = me.read_env_file(os.path.join(me.ROOT, ".env"))
        path = os.path.join(sdir, "app-state.json")
        if not os.path.isfile(path):
            raise ValueError("app-state.json이 없다(B4 앱 세션 준비가 필요하다)")
        with open(path, encoding="utf-8") as stream:
            self.data = json.load(stream)
        self.grafana = ms.Grafana(self.env.get("GRAFANA_ADMIN_PASSWORD", ""))
        self.tick = lambda: None
        self.sleep = None
        self.interruptions = []

    def targets_path(self):
        base = os.environ.get("SLACK_LAB_MON_DIR") or os.path.join(me.ROOT, ".runtime", "monitoring", self.project)
        return os.path.join(base, "targets", "targets.json")

    def real_env(self):
        import _mon_alarm
        if not self.data.get("ready") or self.data.get("project") != self.project:
            raise ValueError("앱 세션 준비 완료 표시가 필요하다")
        slack = _mon_alarm.StubClient(self.data["slack_url"])
        return AppEnv(self.sdir, self.project, self.grafana, slack, self.env["ALERT_CHANNEL"],
                      self.data["receiver_url"], self.env["SLACK_SIGNING_SECRET"], "app",
                      session=self, targets_path=self.targets_path())


def collect_calibration(env, rule):
    """실제 차단·복구 표본을 정확히 3개 수집해 H를 확정한다. 반환 (종료 코드, 결과 딕트).
    경계 초과 표본이 나오면 남은 표본을 재지 않고 즉시 EXIT_BOUND(채점 아님). 복원 실패는 EXIT_OBSERVE로 멈춘다."""
    if rule not in ("rule4-block", "rule4-absent"):
        raise ValueError("차단 보정은 rule4-block·rule4-absent만 지원한다")
    samples, detail = [], []
    result = {"rule": rule, "measured": True, "samples": detail}
    for i in range(1, CAL_SAMPLES + 1):
        try:
            row = env.calibrate_sample(rule, i)
        except RuntimeError as e:
            detail.append({"role": role_for(rule, i), "error": type(e).__name__})
            result.update(exit=model.EXIT_OBSERVE, h=None, reason="유도·복원 실패로 보정 중단")
            return model.EXIT_OBSERVE, result
        detail.append(row)
        if "bound" in row:
            result.update(exit=model.EXIT_BOUND, h=None, reason=row["bound"])
            return model.EXIT_BOUND, result
        if not model.in_bounds(rule, row["d_s"], row["d_e"]):
            row["bound"] = "모형 경계 초과(D_s·D_e)"
            result.update(exit=model.EXIT_BOUND, h=None, reason=row["bound"])
            return model.EXIT_BOUND, result
        samples.append((row["d_s"], row["d_e"]))
    code, h = model.calibrate(rule, samples)
    result.update(exit=code, h=h, reason=None if code == model.EXIT_OK else "최종 H가 150초 상한 초과")
    return code, result


def calibration_h(sdir, rule):
    """세션의 실측 보정 결과 H. 입력 표본(`--samples`)·표본 수 3 아님·실패 기록은 인정하지 않는다(None)."""
    path = os.path.join(sdir, f"calibration-{rule}.json")
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return None
    ok = (data.get("measured") is True and data.get("rule") == rule and data.get("exit") == model.EXIT_OK
          and isinstance(data.get("samples"), list) and len(data["samples"]) == CAL_SAMPLES
          and all("d_s" in row and "d_e" in row for row in data["samples"]) and isinstance(data.get("h"), int))
    return data["h"] if ok else None
