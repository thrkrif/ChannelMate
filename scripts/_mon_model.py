"""6단계 M48 검증 모형(순수 함수, 외부 호출 0): Grafana 평가 격자·발화 조건·발화 유지 시간 H의 산출과 안전 경계, 시나리오 표, 판정 보조.
`scripts/monitoring-verify`와 단위 시험(가상 시계)이 함께 쓴다. 숫자의 근거는 계획서 "장애 성립·유지 정의와 지연 예산"이다.

용어(섞지 않는다): H = 조작 구간 길이, X = 저장소에 조건 참으로 보이는 데이터 구간 길이 = H + D_e − D_s,
D_s·D_e = 데이터 시각 기준 수집 지연(평가 위상과 무관), L_s·L_e = 평가 시각 기준 지연(결과 파일에만 기록).
"""
import math

STEP = 10  # Grafana 평가 간격(초). 운영 값 그대로다.

# 종료 코드(계획서 monitoring-verify 절 + 모형 경계 초과)
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_RULES, EXIT_OBSERVE, EXIT_BOUND = 0, 1, 2, 3, 4, 5

# 규칙별 지속(for), 장애 성립→리포트 예산(초), D-8 (a) 실행 유지 시간(모형 하한 + 여유), 모형 경계 안의 지연 상한
RULES = {
    "rule1": {"for": 30, "deadline": 83, "h_run": 50, "d_s_max": 5, "d_e_max": 5, "margin": 5, "quiet_h": 20, "quiet_cap": 150},
    "rule2": {"for": 0, "deadline": 33, "h_run": None, "d_s_max": 5, "d_e_max": 5, "margin": 0, "quiet_h": None, "quiet_cap": None},
    "rule3": {"for": 120, "deadline": 174, "h_run": 140, "d_s_max": 5, "d_e_max": 5, "margin": 5, "quiet_h": 110, "quiet_cap": 130},
    "rule4-block": {"for": 60, "deadline": 103, "h_run": 95, "d_s_max": 15, "d_e_max": 15, "margin": 10, "quiet_h": 40, "quiet_cap": 150},
    "rule4-absent": {"for": 60, "deadline": 123, "h_run": 120, "d_s_max": 35, "d_e_max": None, "margin": 15, "quiet_h": None, "quiet_cap": 150},
}
H_CEILING = 150  # D-8 (a): 넘으면 멈춤(경계 재정의는 사용자 결정)
NON_FIRE_LIMIT = {"rule1": 25, "rule3": 115}  # 비발화 회차의 실측 H가 for−5를 넘으면 채점하지 않고 "유도 문제"
RECOVERY_S = (30, 45)  # 관측 실패 줄 뒤 패널·규칙 ③ 회복 시간 R(단일 정의)
RULE_RESET_WAIT_S = 130  # 같은 규칙 Normal 뒤 알림 그룹이 비도록 기다리는 시간: 해소 재전송 보존 10초 + 다음 플러시 ≤60초 + 빈 그룹 정리 ≤30초에 여유(70초는 실측으로 부족했다, D-15)


def eval_count(start, end, phase, step=STEP):
    """반열린 구간 [start, end)에 들어 있는 평가 시각(phase + k·step)의 수."""
    if end <= start:
        return 0
    return max(0, math.ceil((end - phase) / step) - math.ceil((start - phase) / step))


def fires(for_s, start, end, phase, step=STEP):
    """조건이 참인 데이터 구간 [start, end)에서 Firing이 되는가. 첫 참 평가에서 Pending, for 뒤의 평가에서 Firing이므로
    연속 참 평가가 for/step + 1회 이상 필요하다(for=0이면 첫 참 평가에서 바로 Firing)."""
    return eval_count(start, end, phase, step) >= for_s // step + 1


def x_len(h, d_s, d_e):
    """데이터 구간 길이 X = H + D_e − D_s."""
    return h + d_e - d_s


def h_lower_bound(for_s, d_s_max, d_e_min=0):
    """모든 위상에서 발화를 보장하는 H 하한: H ≥ for + 10 + D_s − D_e."""
    return for_s + STEP + d_s_max - d_e_min


def h_sample(for_s, d_s_max_obs, d_e_min_obs, margin):
    """보정 표본에서 계산한 H 후보(경계 확인용): 표본 극값만으로 정하면 안전하지 않다(H = max 규칙의 이유)."""
    return for_s + STEP + d_s_max_obs - d_e_min_obs + margin


def h_final(h_model, h_samp, ceiling=H_CEILING):
    """H = max(모형, 표본)을 5초 단위로 올림. 상한을 넘으면 None(멈춤)."""
    value = int(-(-max(h_model, h_samp) // 5) * 5)
    return None if value > ceiling else value


def in_bounds(rule, d_s, d_e):
    """관측한 D_s·D_e가 모형 경계 안인가. 밖이면 채점하지 않고 멈춘다(종료 코드 EXIT_BOUND). D_e < 0은 시계 문제다."""
    r = RULES[rule]
    return d_e >= 0 and d_s >= 0 and d_s <= r["d_s_max"] and (r["d_e_max"] is None or d_e <= r["d_e_max"])


def calibrate(rule, samples):
    """보정 표본 [(D_s, D_e), …] → (종료 코드, H). 표본이 경계 밖이면 EXIT_BOUND, 최종 H가 상한을 넘으면 EXIT_BOUND."""
    r = RULES[rule]
    if not samples or any(not in_bounds(rule, ds, de) for ds, de in samples):
        return EXIT_BOUND, None
    samp = h_sample(r["for"], max(ds for ds, _ in samples), min(de for _, de in samples), r["margin"])
    final = h_final(r["h_run"], samp)
    return (EXIT_OK, final) if final is not None else (EXIT_BOUND, None)


def quiet_x_max(rule, h=None):
    """비발화 유지 시간에서 X의 최댓값(경계 안 지연 최악): H + D_e,max − D_s,min(0)."""
    r = RULES[rule]
    h = r["quiet_h"] if h is None else h
    return h + (r["d_e_max"] or 0)


def quiet_guaranteed(rule, h=None):
    """비발화 보장: 어떤 위상에서도 구간 안 평가 수가 for/step + 1 미만 ⇔ X_max ≤ for."""
    return quiet_x_max(rule, h) <= RULES[rule]["for"]


def simulate_fire(rule, h, d_s, d_e, phase):
    """한 회차 모사: 조작 시작 t_c=0, 해소 t_r=h. 조건 참인 데이터 구간은 [d_s, h + d_e)."""
    return fires(RULES[rule]["for"], d_s, h + d_e, phase)


def within_deadline(rule, elapsed_s):
    """장애 성립부터 리포트까지 내부 지연 예산(단계별 최악 합산) 안인가. 합격선이 아니라 경고 기준이다."""
    return elapsed_s <= RULES[rule]["deadline"]


# C9 9.4 합격선(장애 성립 → 리포트 ≤180초)과 규칙별 내부 예산은 다른 기준이다. ③은 예산 174가 합격선 180보다 6초 빡빡해서
# 예산을 판정에 쓰면 합격선을 바꾼 것과 같아진다(M49a S1a 1회차 175.2초). 다른 규칙은 예산이 합격선보다 훨씬 작아 기존 판정을 유지한다.
ACCEPT_S = {"rule3": 180}


def accept_s(rule):
    return ACCEPT_S.get(rule, RULES[rule]["deadline"])


def within_accept(rule, elapsed_s):
    return elapsed_s <= accept_s(rule)


def classify_unexpected(target_rule, other_rule, interruption_s=None):
    """예상 외 발화 분류(K17). 반환: 'ok' | 'induction'(회차 재실행, 오판 아님) | 'defect'(오판 1, exit 1) | 'unknown'(exit 1)."""
    if other_rule == target_rule:
        return "ok"
    if target_rule == "rule2" and other_rule == "rule4-host":
        if interruption_s is not None and interruption_s >= 60:
            return "induction"
        return "defect"
    return "unknown"


def non_fire_scorable(rule, measured_h):
    """비발화 회차의 실측 H가 한계를 넘으면(스냅샷 주기가 늘어난 경우) 채점하지 않는다."""
    limit = NON_FIRE_LIMIT.get(rule)
    return True if limit is None else measured_h <= limit


def longest_run(series, predicate):
    """[(t, v), …] 시간순에서 predicate(v)가 연속 참인 가장 긴 구간 길이(초). 샘플 간격으로 이어 붙인다."""
    best, run_start, last_t = 0.0, None, None
    for t, v in series:
        if predicate(v):
            run_start = t if run_start is None else run_start
            best = max(best, t - run_start)
        else:
            run_start = None
        last_t = t
    return best


def rule3_quiet_ok(depth_series, n, hold):
    """③ 비발화 시각표 합격: 정지 구간 큐 깊이는 N 그대로이고 queue_ready ≥ 10 연속 구간이 for(120초) 미만이다."""
    run = longest_run(depth_series, lambda v: v >= 10)
    return run < RULES["rule3"]["for"], run


# 실패 유도 표의 실패 멘션 1건당 기대 사건 수 (B-2)
FAILURE_EXPECT = {
    "F-echo": {"attempts": 4, "retry_scheduled": 3, "final_failure": 1, "notices": 1, "replies": 0, "generations": None},
    "F-echo-stub": {"attempts": 2, "retry_scheduled": 1, "final_failure": 0, "notices": 0, "replies": 1, "generations": 2},
    "F-ollama": {"attempts": 4, "retry_scheduled": 3, "final_failure": 1, "notices": 1, "replies": 0, "generations": None},
}


def expected_failure_counts(kind, n=1, rag=False):
    base = FAILURE_EXPECT[kind]
    out = {k: (None if v is None else v * n) for k, v in base.items()}
    if rag and kind == "F-ollama":
        out["rag_result"] = out["attempts"]  # RAG 행은 시도마다 rag_result 1줄
    return out


def compare_counts(expected, observed):
    """기대와 관측 사건 수의 차이. 모르는(None) 기대는 건너뛴다. 반환: {키: (기대, 관측)} — 비어 있으면 일치."""
    return {k: (v, observed.get(k)) for k, v in expected.items() if v is not None and observed.get(k) != v}


def count_reports(posts, fire_expected=1):
    """리포트 채널 게시 수 판정: 발화 회차는 정확히 1(재생 3회 뒤에도 1), 비발화·해제·잘못된 인증은 0."""
    return posts == fire_expected


# --- 규칙·지속 시간 일치 검사 (run 시작 시 Grafana API의 규칙 = 파일, 다르면 exit 3) -------------------------
RULE_UID = {"slack-lab-r1": "rule1", "slack-lab-r2": "rule2", "slack-lab-r3": "rule3", "slack-lab-r4": "rule4"}


def parse_duration(value):
    """'30s'·'2m'·'1h' 또는 나노초 정수(Grafana 프로비저닝 API)를 초로. 해석 못 하면 None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value / 1e9 if value >= 1_000_000 else float(value)
    if isinstance(value, str) and value[:-1].replace(".", "", 1).isdigit() and value[-1] in "smh":
        return float(value[:-1]) * {"s": 1, "m": 60, "h": 3600}[value[-1]]
    return None


def rules_from_files(docs):
    """프로비저닝 파일(여러 개의 JSON 문서) → {uid: {'title', 'for', 'interval'}}."""
    out = {}
    for doc in docs:
        for g in doc.get("groups", []):
            for r in g.get("rules", []):
                out[r["uid"]] = {"title": r["title"], "for": parse_duration(r.get("for")), "interval": parse_duration(g.get("interval"))}
    return out


def parity(file_rules, api_rules):
    """파일 규칙과 Grafana API의 규칙이 uid·제목·지속(for)에서 같은가, 그리고 파일의 for가 모형(RULES)과 같은가. 반환: 불일치 문장 목록."""
    problems = []
    api = {r.get("uid"): r for r in api_rules}
    for uid, f in file_rules.items():
        a = api.get(uid)
        if a is None:
            problems.append(f"{uid}: Grafana에 없음")
            continue
        if a.get("title") != f["title"]:
            problems.append(f"{uid}: 제목 불일치")
        if parse_duration(a.get("for")) != f["for"]:
            problems.append(f"{uid}: for 불일치(파일 {f['for']}초, API {parse_duration(a.get('for'))}초)")
        key = RULE_UID.get(uid)
        if key and f["for"] != float(RULES[key]["for"] if key != "rule4" else 60):
            problems.append(f"{uid}: 파일 for가 계획값과 다름")
        if f["interval"] != float(STEP):
            problems.append(f"{uid}: 평가 간격이 {STEP}초가 아님")
    for uid in api:
        if uid not in file_rules and uid in RULE_UID:
            problems.append(f"{uid}: 파일에 없는 규칙이 Grafana에 있음")
    return problems


# --- 시나리오 표: 이름 → (규칙 키, 실행 방식, 변형) 그리고 회차 계획 ------------------------------------------
SCENARIOS = {
    "rule1-fire": ("rule1", "host", "fire"), "rule1-quiet": ("rule1", "host", "quiet"),
    "rule2-fire": ("rule2", "host", "fire"), "rule2-quiet": ("rule2", "host", "quiet"),
    "rule3-fire": ("rule3", "host", "fire"), "rule3-quiet": ("rule3", "host", "quiet"),
    "rule4-fire": ("rule4-block", "app", "fire"), "rule4-quiet": ("rule4-block", "app", "quiet"),
    "rule4-absent": ("rule4-absent", "app", "fire"),
    "app-stop": (None, "app", "stop"), "dashboard": (None, "host", "panels"),
}
# 비발화 유지 시간: (a) 홀수 회차, (b) 짝수 회차(③은 (a) 9건 130초 / (b) 10건 110초, ①은 (a) 둘 다 0 150초 / (b) 1을 20초)
QUIET_HOLDS = {"rule1": (150, 20), "rule3": (130, 110), "rule4-block": (150, 40)}


def build_plan(scenario, repeat=5, h_override=None):
    """회차 계획 [(variant, H)]. 발화 H는 D-8 (a) 실행 값(보정으로 올린 값이 있으면 h_override). 지속 시간을 줄이는 경로는 없다."""
    rule, _mode, variant = SCENARIOS[scenario]
    if rule is None:
        return [(variant, 0)] * (1 if scenario == "dashboard" else 1)
    spec = RULES[rule]
    if variant == "fire":
        h = h_override if h_override is not None else (spec["h_run"] or 0)
        return [("fire", h)] * repeat
    holds = QUIET_HOLDS.get(rule)
    if holds is None:
        return [("quiet", 0)] * repeat
    return [("quiet", holds[0] if i % 2 == 0 else holds[1]) for i in range(repeat)]
