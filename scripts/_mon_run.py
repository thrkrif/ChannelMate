"""6단계 M48 `monitoring-verify run`의 회차 실행기: 환경(Env)을 주입받아 조작→유지→해소→관측→판정을 수행한다.
환경은 인터페이스(아래 Env)이고 실제 구현은 `_mon_env.RealEnv`, 단위 시험은 가상 시계와 모사 Grafana를 쓴다(외부 호출 0).
운영 지속 시간을 줄이는 인자는 없다(for·평가 간격·반복 시간은 파일 값 그대로이며 이 실행기는 읽지도 바꾸지도 않는다).
"""
import _mon_model as model

# 규칙 이름 ↔ Grafana 규칙 제목은 환경(Env.state)이 맞춘다.


class ObservationError(RuntimeError):
    pass


class Env:
    """실행기가 쓰는 환경 인터페이스. 구현체는 이 메서드만 제공하면 된다."""

    def now(self):  # 단조 시계(초)
        raise NotImplementedError

    def sleep(self, seconds):
        raise NotImplementedError

    def induce(self, rule, variant, round_no):
        """장애 유도 시작. 반환: 조작 시작 시각(없으면 None)."""
        raise NotImplementedError

    def resolve(self, rule, variant, round_no):
        """해소 조작."""
        raise NotImplementedError

    def state(self, rule):
        """대상 규칙의 현재 상태: 'normal' | 'pending' | 'firing' | 'nodata' | 'error'."""
        raise NotImplementedError

    def reports(self, rule):
        """이 회차에서 알람 채널(기록용 Slack 스텁)에 올라간 리포트 수."""
        raise NotImplementedError

    def other_fired(self, rule):
        """대상이 아닌 규칙이 이번 회차에 발화한 것의 (규칙 이름, 중단 시간 초 또는 None) 목록."""
        return []

    def measured(self, rule):
        """수집 지연 관측값 {'d_s':…, 'd_e':…, 'h':…}. 알 수 없으면 비어 있음."""
        return {}


def run_round(env, rule, variant, round_no, h, settle_s=60, poll_s=1.0):
    """회차 하나. variant: 'fire' | 'quiet'. 반환 결과 딕트(판정·시각·사유). 규칙은 결과 파일에 남길 값만 담는다."""
    spec = model.RULES[rule]
    res = {"rule": rule, "variant": variant, "round": round_no, "h": h, "verdict": None, "reason": None, "firing_elapsed": None}
    t_established = env.induce(rule, variant, round_no)
    t0 = env.now() if t_established is None else t_established
    states = []
    fired_at = next((t-t0 for t, state in getattr(env, "history", []) if state == "firing"), None)
    report_at = None
    observation_error = None
    if variant == "fire":
        while True:
            s = env.state(rule)
            states.append((round(env.now() - t0, 1), s))
            if s in ("nodata", "error"):
                observation_error = s
                break
            if s == "firing" and fired_at is None:
                fired_at = env.now() - t0
            posts = env.reports(rule)
            if posts >= 1 and report_at is None:
                report_at = env.now()
            elapsed = env.now() - t0
            if elapsed >= spec["deadline"] or (elapsed >= h and posts >= 1):
                break
            next_check = min(h, spec["deadline"]) if elapsed < h else spec["deadline"]
            env.sleep(min(poll_s, max(0.0, t0 + next_check - env.now())))
        res["hold_elapsed"] = round(env.now() - t0, 1)
    elif not getattr(env, "manages_hold", False):
        env.sleep(max(0.0, h - (env.now() - t0)))
    elif rule == "rule1" and not env.first:
        env.sleep(h)
    env.resolve(rule, variant, round_no)
    meas = env.measured(rule)
    res["measured"] = meas
    # fire의 H는 하한이므로 리포트 예산까지 연장한 시간을 보정 상한으로 거절하지 않는다.
    if (h if variant == "fire" else meas.get("h", 0)) > model.H_CEILING:
        res.update(verdict="bound", reason="실측 H가 150초 상한을 넘음")
        return res
    if meas:
        ds, de = meas.get("d_s"), meas.get("d_e")
        if ds is not None and de is not None and not model.in_bounds(rule, ds, de):
            res.update(verdict="bound", reason="모형 경계 초과(D_s·D_e)")
            return res
    if variant == "quiet" and (not model.non_fire_scorable(rule, meas.get("h", h)) or meas.get("quiet_ok") is False):
        res.update(verdict="induction", reason="비발화 회차의 실측 H가 한계를 넘음(유도 문제)")
        return res

    budget = spec["deadline"] + settle_s if variant == "fire" else settle_s
    end = max(t0 + budget, env.now() + settle_s)
    if fired_at is None:
        fired_at = next((t-t0 for t, state in getattr(env, "history", []) if state == "firing"), None)
    if observation_error or any(state in ("nodata", "error") for _, state in getattr(env, "history", [])):
        res.update(verdict="observe", reason="유도·유지 중 관측 오류", states=states)
        return res
    while env.now() <= end:
        s = env.state(rule)
        states.append((round(env.now() - t0, 1), s))
        if s in ("nodata", "error"):
            res.update(verdict="observe", reason=f"관측 오류({s})")
            res["states"] = states
            return res
        if s == "firing" and fired_at is None:
            fired_at = env.now() - t0
        if variant == "fire":
            posts = env.reports(rule)
            if posts >= 1 and report_at is None:
                report_at = env.now()
            if fired_at is not None and posts >= 1 and not getattr(env, "manages_hold", False):
                break
        env.sleep(poll_s)
    if getattr(env, "manages_hold", False):
        states = [(round(t-t0, 1), state) for t, state in env.history] + states
        states.sort(key=lambda item: item[0])
        first_firing = next((t for t, state in states if state == "firing"), None)
        if first_firing is not None:
            fired_at = first_firing if fired_at is None else min(fired_at, first_firing)
        if any(state in ("nodata", "error") for _, state in states):
            res.update(verdict="observe", reason="회차 중 관측 오류", states=states)
            return res
    res["states"] = states
    if getattr(env, "manages_hold", False):
        res["measured"] = env.measured(rule)
        meas = res["measured"]
        ds, de = meas.get("d_s"), meas.get("d_e")
        if ds is not None and de is not None and not model.in_bounds(rule, ds, de):
            res.update(verdict="bound", reason="모형 경계 초과(D_s·D_e)")
            return res
    posts = env.reports(rule)
    res["reports"] = posts
    for other, interruption in env.other_fired(rule):
        kind = model.classify_unexpected(rule, other, interruption)
        if kind == "induction":
            res.update(verdict="induction", reason=f"예상 외 발화({other}) 중단 {interruption}초 — 유도 문제")
            return res
        if kind in ("defect", "unknown"):
            res.update(verdict="fail", reason=f"예상 외 발화({other}) {kind}")
            return res
    if variant == "fire":
        res["firing_elapsed"] = None if fired_at is None else round(fired_at, 1)
        # 유지 중 도착한 리포트도 예산에 맞게 판정하려고 첫 관측 시각을 쓴다.
        first_report = getattr(env, "report_at", None)
        if first_report is None:
            first_report = report_at if report_at is not None else env.now()
        elapsed = first_report - t0
        res["report_elapsed"] = round(elapsed, 1)
        ok = fired_at is not None and posts == 1 and model.within_deadline(rule, elapsed)
        res.update(verdict="pass" if ok else "fail",
                   reason=None if ok else f"발화={fired_at is not None} 리포트={posts} 경과={elapsed:.0f}초(예산 {spec['deadline']})")
    else:
        ok = fired_at is None and posts == 0
        res.update(verdict="pass" if ok else "fail", reason=None if ok else f"비발화인데 발화={fired_at is not None} 리포트={posts}")
    return res


def run_rounds(env, rule, plan, wait_normal_s=360, reset_wait_s=model.RULE_RESET_WAIT_S):
    """plan: [(variant, h), …]. 회차 사이에 대상 규칙이 Normal이 되기를 기다리고(제한 시간 안, Error/NoData면 중단) 70초 대기한다.
    반환 (종료 코드, 결과 목록). 미달 뒤 같은 유도를 반복하지 않도록 첫 비통과에서 멈춘다."""
    results = []
    exit_code = model.EXIT_OK
    for i, (variant, h) in enumerate(plan, 1):
        try:
            if i == 1 and hasattr(env, "prepare"):
                env.prepare(rule)
            r = run_round(env, rule, variant, i, h)
        except ObservationError:
            r = {"rule": rule, "variant": variant, "round": i, "verdict": "observe", "reason": "회차 시작 전 관측 오류"}
        except (RuntimeError, OSError, ValueError):
            r = {"rule": rule, "variant": variant, "round": i, "verdict": "induction", "reason": "유도·해소 확인 실패"}
        finally:
            if hasattr(env, "cleanup"):
                try:
                    env.cleanup()
                except (RuntimeError, OSError, ValueError):
                    r = {"rule": rule, "variant": variant, "round": i, "verdict": "observe", "reason": "회차 정리 실패"}
        results.append(r)
        if r["verdict"] == "bound":
            return model.EXIT_BOUND, results
        if r["verdict"] == "observe":
            return model.EXIT_OBSERVE, results
        if r["verdict"] == "fail":
            return model.EXIT_FAIL, results
        if r["verdict"] == "induction":
            return model.EXIT_FAIL, results
        if i < len(plan):
            end = env.now() + wait_normal_s
            while env.state(rule) != "normal":
                if env.now() > end:
                    return model.EXIT_OBSERVE, results
                if env.state(rule) in ("nodata", "error"):
                    return model.EXIT_OBSERVE, results
                env.sleep(1.0)
            env.sleep(reset_wait_s)
    return exit_code, results
