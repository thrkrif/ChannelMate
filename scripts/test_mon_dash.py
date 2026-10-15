"""C9.11 합성 회전 로그와 가짜 Grafana. 외부 호출은 금지한다."""
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import _mon_dash as dash
import _mon_model as model
from _mon_spike import iso, log_line, snapshot_line, unobserved_line

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, 'infra/monitoring/grafana/dashboards/slack-lab.json'), encoding='utf-8') as stream:
    PANELS = {p['id']: p for p in json.load(stream)['panels']}
BASE = 1792000000


def metric(t, n=10, **extra):
    fields = dict(event_id='PRIVATE_EVENT', gen=0, manual_run='false', result='Delivered', kind='answer',
                  queue_wait_ms=n, llm_ms=n * 10, answer_ms=n * 20, negative_interval='false')
    fields.update(extra)
    return log_line(t, '처리 지표 ' + ' '.join(f'{k}={v}' for k, v in fields.items()), 'processing_metric', fields)


def snap(t, **extra):
    fields = dict.fromkeys(dash.KEYS, 0)
    fields.update(extra)
    return snapshot_line(t, **fields)


def vector(values, labels):
    return 200, {'status': 'success', 'data': {'resultType': 'vector', 'result': [
        {'metric': dict(zip(labels, key)), 'value': [0, str(v)]} for key, v in values.items()]}}


class FakeGrafana:
    def __init__(self, records):
        self.records, self.bump, self.error, self.up, self.health = records, 0, False, 1, 'ok'
        self.calls = []

    def query(self, ds, expr, t=None):
        self.calls.append((ds, expr, t))
        if self.error:
            return 500, {}
        if ds == 'prom':
            return vector({('all',): self.up}, ('role',))
        # 기대 함수를 공유하면 필터의 회귀를 함께 놓치므로 별도로 계산한다.
        window = [(ts, e, f) for ts, e, f in self.records if t - 300 < ts <= t]
        if 'quantile_over_time' in expr:
            key = 'queue_wait_ms' if 'unwrap queue_wait_ms' in expr else 'answer_ms' if 'unwrap answer_ms' in expr else 'llm_ms'
            values = []
            for _, e, f in window:
                if e != 'processing_metric':
                    continue
                if key != 'llm_ms' and not (f['result'] == 'Delivered' and f['kind'] == 'answer' and f['gen'] == '0' and f['manual_run'] != 'true'):
                    continue
                v = int(f[key])
                if v >= 0:
                    values.append(v)
            s = sorted(values)
            p = .95 * (len(s) - 1)
            import math
            value = s[math.floor(p)] * (1 - p % 1) + s[math.ceil(p)] * (p % 1) if s else None
            return vector({('all',): value} if s else {}, ('role',))
        if 'last_over_time' in expr:
            key = expr.split('unwrap ', 1)[1].split()[0]
            recent = [(ts, e, f) for ts, e, f in window if ts > t - 30]
            snapshots = [f for _, e, f in recent if e == 'backlog_snapshot']
            hidden = any(e == 'backlog_unobserved' or (e == 'backlog_snapshot' and key not in f) for _, e, f in recent)
            return vector({('all',): int(snapshots[-1][key])} if snapshots and not hidden else {}, ('role',))
        from collections import Counter
        counts = Counter()
        for _, e, f in window:
            if 'processing_metric' in expr and e == 'processing_metric':
                counts[(f['result'], f['kind'])] += 1
            elif 'rag_result' in expr and e == 'rag_result':
                counts[(f['result'],)] += 1
        if self.bump and 'processing_metric' in expr:
            counts[('Delivered', 'answer')] += self.bump
        return vector(counts, ('result', 'kind') if 'processing_metric' in expr else ('result',))

    def request(self, method, path):
        if path.endswith('/targets'):
            return 200, {'status': 'success', 'data': {'activeTargets': [
                {'labels': {'job': 'slack-lab', 'role': 'all'}, 'health': 'up'}]}}
        return 200, [{'uid': uid, 'title': uid} for uid in model.RULE_UID]

    def rules(self):
        return [{'name': uid, 'state': 'inactive', 'health': self.health} for uid in model.RULE_UID]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sdir = self.temp.name
        os.mkdir(os.path.join(self.sdir, 'all'))
        self.lines = [snap(BASE), metric(BASE + 5), metric(BASE + 15, 20),
                      unobserved_line(BASE + 20), snapshot_line(BASE + 25, queue_ready=3),
                      log_line(BASE + 26, '알람 리포트', 'alert_report'),
                      log_line(BASE + 27, 'metric=rag_result result=found', 'rag_result', {'result': 'found'}),
                      metric(BASE + 35, 30, result='RetryRequested', kind='-', negative_interval='true'),
                      snap(BASE + 70), metric(BASE + 75, 40), snap(BASE + 80)]
        self.write(self.lines)

    def write(self, lines):
        # 파일 이름의 정렬 순서와 시간 순서가 달라도 p1의 마지막 스냅샷을 지켜야 한다.
        for name, rows in [('slack-lab.log.2', lines[:5]), ('slack-lab.log', lines[5:])]:
            with open(os.path.join(self.sdir, 'all', name), 'w', encoding='utf-8') as stream:
                stream.write('\n'.join(rows) + '\n')
        import glob
        self.records, self.paths = dash.read_logs(glob.glob(os.path.join(self.sdir, 'all', '*')))
        self.graf = FakeGrafana(self.records)

    def compare(self, offset):
        t = BASE + offset
        return dash.compare_window(self.records, t, PANELS, self.graf, dash.p1_metrics(ROOT, self.paths, t))

    def test_common_and_interpolated_percentile(self):
        row = self.compare(19)
        self.assertTrue(row['common_valid'])
        self.assertEqual(row['verdict'], 'pass', row)
        self.assertEqual(dash.percentile([10, 20]), 20)
        self.assertEqual(dash.percentile([10, 20], True), 19.5)

    def test_state_table(self):
        for offset, state in [(24, '관측 불가'), (51, '키 누락'), (56, 'stale')]:
            with self.subTest(offset=offset):
                row = self.compare(offset)
                self.assertFalse(row['common_valid'])
                self.assertEqual(row['states']['defer'], state)
                self.assertEqual(row['verdict'], 'pass', row)
        self.assertEqual(self.compare(56)['measurement'], '측정 무효')
        self.assertEqual(self.compare(79)['verdict'], 'pass')
        no_snap = [(BASE, 'other', {})]
        self.assertEqual(dash.expected(no_snap, BASE + 1)['states']['defer'], '스냅샷 없음')

    def test_one_count_mismatch(self):
        self.graf.bump = 1
        self.assertEqual(self.compare(19)['verdict'], 'fail')

    def test_backlog_and_percentile_mismatch(self):
        original = self.graf.query
        for expr in (PANELS[1]['targets'][0]['expr'], PANELS[2]['targets'][0]['expr']):
            def changed(ds, query, t=None):
                return vector({('all',): 999}, ('role',)) if query == expr else original(ds, query, t)
            with patch.object(self.graf, 'query', side_effect=changed):
                self.assertEqual(self.compare(19)['verdict'], 'fail')

    def test_missing_invalid_marker(self):
        t = BASE + 56
        metrics = dash.p1_metrics(ROOT, self.paths, t)
        metrics['invalid'] = False
        self.assertEqual(dash.compare_window(self.records, t, PANELS, self.graf, metrics)['verdict'], 'fail')

    def test_empty_and_malformed_observation(self):
        with self.assertRaises(dash.ObservationError):
            dash.select_windows([])
        with patch.object(self.graf, 'query', return_value=vector({('all',): float('nan')}, ('role',))):
            with self.assertRaises(dash.ObservationError):
                self.compare(19)
        with patch.object(self.graf, 'query', return_value=(200, {'status': 'error'})):
            with self.assertRaises(dash.ObservationError):
                self.compare(19)

    def test_window_selection(self):
        times, coverage = dash.select_windows(self.records)
        self.assertGreaterEqual(sum(any(t - 300 <= ts <= t and e == 'processing_metric' for ts, e, _ in self.records) for t in times), 2)
        self.assertTrue(all(v == '포함' for v in coverage.values()))
        _, absent = dash.select_windows(self.records[:3])
        self.assertEqual(absent['30초 초과 공백'], '해당 창 없음')
        self.assertEqual(absent['알람 리포트'], '해당 창 없음')

    def test_current_and_errors(self):
        self.assertEqual(dash.current(PANELS, self.graf, BASE)['verdict'], 'pass')
        self.graf.up = 0
        self.assertEqual(dash.current(PANELS, self.graf, BASE)['verdict'], 'fail')
        self.graf.health = 'error'
        with self.assertRaises(dash.ObservationError):
            dash.current(PANELS, self.graf, BASE)
        with self.assertRaises(dash.ObservationError):
            dash.parse_metrics('잘린 출력')

    def test_run_exit_and_privacy(self):
        self.assertEqual(dash.run(ROOT, self.sdir, self.graf), 0)
        self.graf.bump = 1
        self.assertEqual(dash.run(ROOT, self.sdir, self.graf), 1)
        self.graf.error = True
        self.assertEqual(dash.run(ROOT, self.sdir, self.graf), 4)
        with open(os.path.join(self.sdir, 'run-dashboard.json'), encoding='utf-8') as stream:
            output = stream.read()
        self.assertNotIn('PRIVATE_EVENT', output)
        self.assertNotIn(self.sdir, output)

    def test_boundary(self):
        row = self.compare(30)
        self.assertFalse(row['common_valid'])
        self.assertEqual(self.compare(55)['states']['defer'], 'stale')


if __name__ == '__main__':
    with patch('urllib.request.urlopen', side_effect=AssertionError('외부 호출 금지')):
        unittest.main()
