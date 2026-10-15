"""C9.11: 기록된 창과 실제 패널 질의를 비교한다. 결과에는 집계값만 남긴다."""
import datetime as dt
import glob
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import Counter

import _mon_model as model
from _logline import restore

KEYS = ('queue_ready', 'defer', 'dead', 'retry', 'dlq', 'recovery')
LABELS = {'정상 답변': ('Delivered', 'answer'), '실패 안내': ('Delivered', 'failure_notice'),
          '재시도 요청': ('RetryRequested', '-'), 'DLQ(실패 확정)': ('Failed', '-'), '결과 불명': ('Unknown', '-')}


class ObservationError(Exception):
    pass


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec='microseconds')


def read_logs(paths):
    records, ordered = [], []
    for path in paths:
        first = None
        with open(path, encoding='utf-8', errors='replace') as stream:
            for raw in stream:
                try:
                    obj = json.loads(raw)
                    t = dt.datetime.fromisoformat(obj['ts'].replace('Z', '+00:00')).timestamp()
                    if obj.get('role') not in ('all', 'worker'):
                        continue
                    text = restore(raw)
                    fields = dict(re.findall(r'(\w+)=(\S+)', text))
                    records.append((t, obj.get('event'), fields))
                    first = t if first is None else min(first, t)
                except (ValueError, KeyError, TypeError):
                    raise ObservationError('구조화 로그 형식 오류') from None
        ordered.append((first if first is not None else math.inf, path))
    return sorted(records, key=lambda r: r[0]), [p for _, p in sorted(ordered)]


def select_windows(records):
    metrics = sorted({t + .001 for t, e, _ in records if e == 'processing_metric'})
    if not metrics:
        raise ObservationError('처리 지표가 있는 창 부족')
    candidates = set(metrics[:1] + metrics[-1:])
    if len(candidates) < 2:
        candidates.add(metrics[0] + 1)
    coverage = {}
    for name, predicate in (
        ('관측 실패', lambda e, f: e == 'backlog_unobserved'),
        ('부분 스냅샷', lambda e, f: e == 'backlog_snapshot' and any(k not in f for k in KEYS)),
        ('알람 리포트', lambda e, f: 'alert' in (e or '') or 'report' in (e or '')),
        ('negative_interval', lambda e, f: f.get('negative_interval') == 'true'),
    ):
        found = [t for t, e, f in records if predicate(e, f)]
        coverage[name] = '포함' if found else '해당 창 없음'
        if found:
            candidates.add(found[0] + .001)
    snapshots = sorted({t for t, e, _ in records if e == 'backlog_snapshot'})
    anchors = sorted(set([records[0][0], *snapshots]))
    gaps = [a + 30.001 for a, b in zip(anchors, anchors[1:] + [records[-1][0]]) if b - a > 30]
    coverage['30초 초과 공백'] = '포함' if gaps else '해당 창 없음'
    if gaps:
        candidates.add(gaps[0])
    return sorted(candidates), coverage


def percentile(values, interpolate=False):
    if not values:
        return None
    s = sorted(values)
    if not interpolate:
        return s[math.ceil(.95 * len(s)) - 1]
    pos = .95 * (len(s) - 1)
    lo = math.floor(pos)
    weight = pos - lo
    return s[lo] * (1 - weight) + s[math.ceil(pos)] * weight


def expected(records, t):
    rows = [(ts, e, f) for ts, e, f in records if t - 300 <= ts <= t]
    samples = {2: [], 3: [], 4: []}
    counts, rag = Counter(), Counter()
    for _, event, f in rows:
        if event == 'processing_metric':
            result, kind = f.get('result'), f.get('kind', '-')
            if result not in ('Delivered', 'RetryRequested', 'Failed', 'Unknown'):
                raise ObservationError('처리 결과 형식 오류')
            counts[(result, kind)] += 1
            if int(f.get('llm_ms', -1)) >= 0:
                samples[3].append(int(f['llm_ms']))
            if result == 'Delivered' and kind == 'answer' and f.get('gen') == '0' and f.get('manual_run') != 'true':
                for panel, key in ((2, 'queue_wait_ms'), (4, 'answer_ms')):
                    if int(f.get(key, -1)) >= 0:
                        samples[panel].append(int(f[key]))
        elif event == 'rag_result':
            if f.get('result') not in ('found', 'none', 'unavailable'):
                raise ObservationError('검색 결과 형식 오류')
            rag[f['result']] += 1
    recent = [(ts, e, f) for ts, e, f in rows if ts > t - 30]
    snaps = [(ts, f) for ts, e, f in rows if e == 'backlog_snapshot']
    backlog, states = {}, {}
    for k in KEYS:
        if any(e == 'backlog_unobserved' for _, e, _ in recent):
            states[k] = '관측 불가'
        elif any(e == 'backlog_snapshot' and k not in f for _, e, f in recent):
            states[k] = '키 누락'
        elif not any(e == 'backlog_snapshot' for _, e, _ in recent):
            states[k] = 'stale' if snaps else '스냅샷 없음'
        else:
            states[k] = '유효'
            backlog[k] = int(snaps[-1][1][k])
    common = all(s == '유효' for s in states.values())
    latest = {k: int(v) for k, v in (snaps[-1][1] if snaps else {}).items() if k in KEYS}
    return {'samples': samples, 'counts': counts, 'rag': rag, 'backlog': backlog, 'states': states,
            'common': common, 'latest': latest, 'negative': sum(f.get('negative_interval') == 'true' for _, _, f in rows)}


def parse_metrics(output):
    series = {}
    for key, n, p95 in re.findall(r'^\s*(queue_wait_ms|llm_attempt_ms|answer_ms)\s+n=(\d+)(?:\s+p50=\S+\s+p95=(\S+))?', output, re.M):
        series[key] = (int(n), float(p95) if p95 else None)
    if len(series) != 3 or '⑤ 처리 결과 분포' not in output or '⑥ RAG' not in output:
        raise ObservationError('p1-metrics 출력 형식 오류')
    attempts = output.split('시도 단위:', 1)[1].split('이벤트 단위', 1)[0]
    counts = {}
    for label, n in re.findall(r'^\s+(.+): (\d+)\s*$', attempts, re.M):
        if label not in LABELS:
            raise ObservationError('p1-metrics 결과 형식 오류')
        counts[LABELS[label]] = int(n)
    rag = re.search(r'found=(\d+) none=(\d+) unavailable=(\d+)', output)
    if not rag and 'RAG 호출 0' not in output:
        raise ObservationError('p1-metrics 검색 출력 형식 오류')
    backlog_text = output.split('① 최신 적체 스냅샷', 1)[1].split('② 큐 대기', 1)[0]
    backlog = {k: int(v) for k, v in re.findall(r'(queue_ready|defer|dead|retry|dlq|recovery)=(\d+)', backlog_text)}
    return {'series': series, 'counts': counts, 'rag': dict(zip(('found', 'none', 'unavailable'), map(int, rag.groups()))) if rag else {},
            'backlog': backlog, 'invalid': '성능 측정은 무효' in output}


def p1_metrics(root, paths, t):
    try:
        p = subprocess.run([sys.executable, os.path.join(root, 'scripts', 'p1-metrics'), '--since', '5m', '--now', iso(t), *paths],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise ObservationError('p1-metrics 실행 오류') from None
    if p.returncode:
        raise ObservationError('p1-metrics 실행 오류')
    return parse_metrics(p.stdout)


def vector(graf, ds, expr, t, labels):
    code, body = graf.query(ds, expr, t=t)
    if code != 200 or not isinstance(body, dict) or body.get('status') != 'success':
        raise ObservationError('데이터소스 질의 오류')
    data = body.get('data', {})
    if data.get('resultType') != 'vector' or not isinstance(data.get('result'), list):
        raise ObservationError('즉시 질의 응답 형식 오류')
    out = {}
    for item in data['result']:
        key = tuple(item.get('metric', {}).get(k, '') for k in labels)
        value = float(item['value'][1])
        if key in out or not math.isfinite(value):
            raise ObservationError('중복 또는 유효하지 않은 시리즈')
        out[key] = value
    return out


def compare_window(records, t, panels, graf, metrics):
    exp = expected(records, t)
    comparisons = []

    def add(panel, name, want, got):
        comparisons.append({'panel': panel, 'metric': name, 'expected': want, 'observed': got, 'pass': want == got})

    for panel, key in ((2, 'queue_wait_ms'), (3, 'llm_attempt_ms'), (4, 'answer_ms')):
        samples = exp['samples'][panel]
        add(panel, 'p1 표본 수·nearest-rank p95', [len(samples), percentile(samples)], list(metrics['series'][key]))
        got = vector(graf, 'loki', panels[panel]['targets'][0]['expr'], t, ('role',))
        want = {('all',): percentile(samples, True)} if samples else {}
        add(panel, '동일 식 p95', list(want.values()), list(got.values()) if set(got) == set(want) else ['시리즈 불일치'])
    normalized = Counter()
    for (result, kind), n in exp['counts'].items():
        normalized[(result, kind if result == 'Delivered' else '-')] += n
    add(5, 'p1 시도 건수', sorted(normalized.items()), sorted(metrics['counts'].items()))
    got = vector(graf, 'loki', panels[5]['targets'][0]['expr'], t, ('result', 'kind'))
    add(5, '결과별 시도 건수', sorted(exp['counts'].items()), sorted(got.items()))
    add(6, 'p1 검색 건수', {k: exp['rag'][k] for k in ('found', 'none', 'unavailable')},
        {k: metrics['rag'].get(k, 0) for k in ('found', 'none', 'unavailable')})
    got = vector(graf, 'loki', panels[6]['targets'][0]['expr'], t, ('result',))
    add(6, '검색 건수', sorted(((k,), v) for k, v in exp['rag'].items()), sorted(got.items()))
    add(1, 'p1 최신 스냅샷', exp['latest'], metrics['backlog'])
    for target in panels[1]['targets']:
        key = target['legendFormat']
        got = vector(graf, 'loki', target['expr'], t, ('role',))
        want = {('all',): exp['backlog'][key]} if key in exp['backlog'] else {}
        add(1, key + ':' + exp['states'][key], sorted(want.items()), sorted(got.items()))
    add(2, 'negative_interval 측정 무효', bool(exp['negative']), metrics['invalid'])
    return {'T': iso(t), 'common_valid': exp['common'], 'states': exp['states'],
            'measurement': '측정 무효' if exp['negative'] else '유효', 'comparisons': comparisons,
            'verdict': 'pass' if all(c['pass'] for c in comparisons) else 'fail'}


def current(panels, graf, t):
    up = vector(graf, 'prom', panels[7]['targets'][0]['expr'], t, ('role',))
    code, body = graf.request('GET', '/api/datasources/proxy/uid/slack-lab-prom/api/v1/targets')
    if code != 200 or not isinstance(body, dict) or body.get('status') != 'success':
        raise ObservationError('현재 scrape 상태 관측 오류')
    targets = body.get('data', {}).get('activeTargets', [])
    actual = {}
    for target in targets:
        if target.get('labels', {}).get('job') == 'slack-lab':
            role = target['labels'].get('role', '')
            if (role,) in actual or target.get('health') not in ('up', 'down'):
                raise ObservationError('현재 scrape 대상 형식 오류')
            actual[(role,)] = int(target['health'] == 'up')
    if not actual or not up:
        raise ObservationError('현재 scrape 대상 없음')
    code, rules = graf.request('GET', '/api/v1/provisioning/alert-rules')
    if code != 200 or not isinstance(rules, list):
        raise ObservationError('규칙 목록 관측 오류')
    titles = {r['title'] for r in rules if r.get('uid') in model.RULE_UID}
    states = graf.rules()
    states = [r for r in states if r.get('name') in titles]
    if len(titles) != 4 or len(states) != 4:
        raise ObservationError('현재 규칙 상태 누락')
    if any(r.get('health') != 'ok' or r.get('state') not in ('inactive', 'pending', 'firing')
           or any(a.get('state') not in ('Normal', 'Pending', 'Alerting') for a in r.get('alerts', [])) for r in states):
        raise ObservationError('현재 규칙 Error 또는 NoData')
    options = panels[8]['options']
    filters = options.get('stateFilter', {})
    visible = all(filters.get({'inactive': 'normal', 'pending': 'pending', 'firing': 'firing'}[r['state']]) for r in states)
    visible = visible and not options.get('dashboardAlerts') and not options.get('alertName') and options.get('maxItems', 0) >= 4
    return {'up': [{'role': k[0], 'value': v, 'current': actual.get(k)} for k, v in sorted(up.items())],
            'rules': [{'state': r['state'], 'health': r['health']} for r in states],
            'verdict': 'pass' if actual == up and visible else 'fail'}


def run(root, sdir, graf):
    result = {'scenario': 'dashboard', 'windows': [], 'exit': model.EXIT_OBSERVE}
    try:
        paths = [p for p in glob.glob(os.path.join(sdir, 'all', 'slack-lab.log*')) if os.path.isfile(p)]
        records, paths = read_logs(paths)
        times, result['coverage'] = select_windows(records)
        dashboard = os.path.join(root, 'infra', 'monitoring', 'grafana', 'dashboards', 'slack-lab.json')
        with open(dashboard, encoding='utf-8') as stream:
            panels = {p['id']: p for p in json.load(stream)['panels']}
        for t in times:
            result['windows'].append(compare_window(records, t, panels, graf, p1_metrics(root, paths, t)))
        result['current'] = current(panels, graf, time.time())
        result['exit'] = model.EXIT_OK if all(w['verdict'] == 'pass' for w in result['windows']) and result['current']['verdict'] == 'pass' else model.EXIT_FAIL
    except (ObservationError, OSError, ValueError, KeyError, TypeError, IndexError):
        result['reason'] = '관측 오류: 로그·질의·p1-metrics·현재 상태 확인 필요'
    with open(os.path.join(sdir, 'run-dashboard.json'), 'w', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    return result['exit']
