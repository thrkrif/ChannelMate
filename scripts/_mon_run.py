"""6단계 M48 `monitoring-verify run`의 회차 실행기: 환경(Env)을 주입받아 조작→유지→해소→관측→판정을 수행한다.
환경은 인터페이스(아래 Env)이고 실제 구현은 `_mon_env.RealEnv`, 단위 시험은 가상 시계와 모사 Grafana를 쓴다(외부 호출 0).
운영 지속 시간을 줄이는 인자는 없다(for·평가 간격·반복 시간은 파일 값 그대로이며 이 실행기는 읽지도 바꾸지도 않는다).
"""
import _mon_model as model

# 규칙 이름 ↔ Grafana 규칙 제목은 환경(Env.state)이 맞춘다.


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
    env.sleep(max(0.0, h - (env.now() - t0)))
    env.resolve(rule, variant, round_no)
    meas = env.measured(rule)
    res["measured"] = meas
    if meas:
        ds, de = meas.get("d_s"), meas.get("d_e")
        if ds is not None and de is not None and not model.in_bounds(rule, ds, de):
            res.update(verdict="bound", reason="모형 경계 초과(D_s·D_e)")
            return res
    if variant == "quiet" and not model.non_fire_scorable(rule, meas.get("h", h)):
        res.update(verdict="induction", reason="비발화 회차의 실측 H가 한계를 넘음(유도 문제)")
        return res

    budget = spec["deadline"] + settle_s if variant == "fire" else settle_s
    end = t0 + budget
    states = []
    fired_at = None
    while env.now() <= end:
        s = env.state(rule)
        states.append((round(env.now() - t0, 1), s))
        if s in ("nodata", "error"):
            res.update(verdict="observe", reason=f"관측 오류({s})")
            res["states"] = states
            return res
        if s == "firing" and fired_at is None:
            fired_at = env.now() - t0
        if variant == "fire" and fired_at is not None and env.reports(rule) >= 1:
            break
        env.sleep(poll_s)
    res["states"] = states
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
        # 예산 판정은 장애 성립 → 리포트까지: 리포트가 올라온 시각(= 폴링이 끝난 시각)을 기준으로 한다.
        elapsed = env.now() - t0
        res["report_elapsed"] = round(elapsed, 1)
        ok = fired_at is not None and posts == 1 and model.within_deadline(rule, elapsed)
        res.update(verdict="pass" if ok else "fail",
                   reason=None if ok else f"발화={fired_at is not None} 리포트={posts} 경과={elapsed:.0f}초(예산 {spec['deadline']})")
    else:
        ok = fired_at is None and posts == 0
        res.update(verdict="pass" if ok else "fail", reason=None if ok else f"비발화인데 발화={fired_at is not None} 리포트={posts}")
    return res


def run_rounds(env, rule, plan, wait_normal_s=120, reset_wait_s=model.RULE_RESET_WAIT_S):
    """plan: [(variant, h), …]. 회차 사이에 대상 규칙이 Normal이 되기를 기다리고(제한 시간 안, Error/NoData면 중단) 70초 대기한다.
    반환 (종료 코드, 결과 목록). 첫 비통과에서 멈추지 않고 가능한 만큼 모으되, bound/observe는 즉시 멈춘다."""
    results = []
    exit_code = model.EXIT_OK
    for i, (variant, h) in enumerate(plan, 1):
        r = run_round(env, rule, variant, i, h)
        results.append(r)
        if r["verdict"] == "bound":
            return model.EXIT_BOUND, results
        if r["verdict"] == "observe":
            return model.EXIT_OBSERVE, results
        if r["verdict"] in ("fail",):
            exit_code = model.EXIT_FAIL
        if r["verdict"] == "induction":
            exit_code = exit_code or model.EXIT_FAIL  # 유도 문제는 오판이 아니지만 합격도 아니다(회차 재실행 필요)
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
