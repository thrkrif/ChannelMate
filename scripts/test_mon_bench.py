"""M49a bench 회귀 시험. PATH의 가짜 실행 파일·가상 시계만 쓰며 외부 호출과 실제 기동은 없다."""
import argparse
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import _mon_bench as bench

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("bench_mv", str(HERE / "monitoring-verify"))
mv = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(mv)

FAKE = r'''
import json, os, pathlib, shutil, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
sdir = pathlib.Path(os.environ['FAKE_SESSION'])
trace = sdir / 'trace.jsonl'
with trace.open('a') as f:
    f.write(json.dumps([name] + args) + '\n')
def opt(k): return args[args.index(k)+1]
def read(p): return json.loads(p.read_text())
def write(p, v): p.write_text(json.dumps(v))
def logs(failure=False):
    role = 'worker' if os.environ['FAKE_MODE'] == 'app' else 'all'
    p = sdir / role / 'slack-lab.log'
    p.parent.mkdir(exist_ok=True)
    with p.open('a') as f:
        f.write(json.dumps({'msg':'워커 소비자 시작'})+'\n')
        if failure:
            for msg in ('모델 확인을 건너뜀', 'LLM 웜업 결과=Failed'):
                f.write(json.dumps({'msg':msg})+'\n')
if name == 's6-session':
    ledger = read(sdir / 'ledger.json')
    if args[0] == 'spawn':
        n = opt('--name')
        ledger['pids'].append({'pid':40000+len(ledger['pids']), 'name':n})
        write(sdir / 'ledger.json', ledger)
        extra = {}
        for i, a in enumerate(args):
            if a == '--env':
                k,v = args[i+1].split('=',1); extra[k]=v
        if n.startswith('load-'):
            subprocess.run(args[args.index('--')+1:], env=dict(os.environ, **extra), check=True)
        elif n.startswith('bench-app'):
            logs(n.endswith('failure'))
        print(ledger['pids'][-1]['pid'])
    elif args[0] == 'compose':
        tail = args[args.index('--')+1:]
        ledger['compose'].append(tail)
        write(sdir / 'ledger.json', ledger)
        if 'receiver' in tail and 'up' in tail:
            logs('--force-recreate' in tail)
        if 'build' in tail and os.environ.get('FAKE_BUILD_FAIL'):
            print('secret-value channel-private EvPRIVATE', file=sys.stderr); sys.exit(1)
elif name == 'hw-measure':
    out = pathlib.Path(opt('--out'))
    if args[0] == 'start':
        out.mkdir()
        (out / 'hw-sampler.pid').write_text('45000')
    elif args[0] == 'report':
        print(json.dumps({'ok': not bool(os.environ.get('FAKE_HW_FAIL')), 'stable_ok':True, 'swap_growth_mib':1, 'vm_ok':True}))
elif name == 'p1-load':
    label = pathlib.Path(os.environ['P1_SUMMARY_OUT']).stem.split('load-')[1]
    n = {'warmup':2, 'seq':20, 'burst':10, 'normal':5, 'failure':5}[label]
    failure = label == 'failure'
    summary = dict(threads=n,ok=0 if failure else n,missing=0,dup=0,failure_notice=n if failure else 0,language_violation=0)
    write(pathlib.Path(os.environ['P1_SUMMARY_OUT']),dict(summary=summary,http_errors=0))
    role = 'worker' if os.environ['FAKE_MODE'] == 'app' else 'all'
    p = sdir / role / 'slack-lab.log'
    def row(f, event=None, msg='', **fields):
        f.write(json.dumps(dict(event=event,msg=msg,fields=fields))+'\n')
    with p.open('a') as f:
        for i in range(n):
            row(f,msg='slack 수신 retry_num=null')
            for j in range(4 if failure else 1):
                row(f,'processing_metric',kind='failure_notice' if failure and j==3 else ('-' if failure else 'answer'),result='Delivered' if not failure or j==3 else 'RetryRequested')
                if failure and j<3:
                    if not (os.environ.get('FAKE_BAD_COUNTS') and i==0 and j==0):
                        row(f,msg='LLM 재시도 가능한 실패 → 재시도 예약')
            if failure: row(f,'llm_final_failure')
    posts = sdir / 'posts.json'
    write(posts, (read(posts) if posts.exists() else 0) + n*2)
elif name == 'docker':
    if args[:2] == ['image','inspect']:
        if os.environ.get('FAKE_IMAGE_MISSING'): sys.exit(1)
        print('sha256:fake-image')
    elif args[0] == 'cp':
        target = pathlib.Path(args[-1])
        shutil.copyfile(sdir / 'app.jar',target)
        if os.environ.get('FAKE_IMAGE_STALE'): target.write_bytes(b'stale')
    elif args[0] == 'ps' and os.environ.get('FAKE_CONTAINERS'): print('old-container')
    elif args[:2] == ['volume','ls'] and os.environ.get('FAKE_VOLUMES'): print('old-volume')
elif name == 'ollama':
    print('NAME ID SIZE')
    if os.environ.get('FAKE_OLLAMA'): print('loaded-model x 1GB')
elif name == 'lsof':
    if '-t' in args:
        print('44000') if sum(1 for ln in trace.read_text().splitlines() if json.loads(ln)[:2]==['lsof','-t']) == 1 or 'bench-app-failure' in trace.read_text() else None
    elif os.environ.get('FAKE_LISTENER'): print('java TCP *:8080 (LISTEN)')
'''


class Clock:
    def __init__(self):
        self.t = 1000
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'repo'
        self.sdir = self.base / 'session'
        self.bin = self.base / 'bin'
        for p in (self.root / 'scripts', self.sdir, self.bin):
            p.mkdir(parents=True)
        fake = self.bin / 'fake'
        fake.write_text('#!' + sys.executable + '\n' + FAKE)
        fake.chmod(0o755)
        for name in ('docker', 'lsof', 'ollama', 'ps'):
            (self.bin / name).symlink_to(fake)
        for name in ('s6-session', 'hw-measure', 'p1-load'):
            (self.root / 'scripts' / name).symlink_to(fake)
        (self.root / '.env').write_text('SLACK_BOT_TOKEN=secret-value\nSLACK_SIGNING_SECRET=secret-value\nSLACK_TEST_CHANNEL=channel-private\nALERT_CHANNEL=channel-private\nMANAGEMENT_METRICS_TOKEN=secret-value\n')
        (self.sdir / 'app.jar').write_bytes(b'jar')
        bench.write_json(str(self.sdir / 'jar.json'), {'jar': str(self.sdir / 'app.jar'), 'sha256': hashlib.sha256(b'jar').hexdigest()})
        bench.write_json(str(self.sdir / 'ledger.json'), {'project':'slack-lab-s6v-test','pids':[],'compose':[]})
        self.clock = Clock()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(bench, 'ROOT', str(self.root)))
        self.stack.enter_context(patch.object(bench._mon_env, 'ROOT', str(self.root)))
        self.stack.enter_context(patch.dict(os.environ, {'PATH':str(self.bin)+os.pathsep+os.environ['PATH'], 'FAKE_SESSION':str(self.sdir), 'FAKE_MODE':'host'}))
        self.stack.enter_context(patch.object(bench.time, 'time', self.clock.now))
        self.stack.enter_context(patch.object(bench.time, 'monotonic', self.clock.now))
        self.stack.enter_context(patch.object(bench.time, 'sleep', self.clock.sleep))
        self.stack.enter_context(patch.object(bench.os, 'kill', lambda *a: os.environ.update(FAKE_STOPPED='1')))
        self.stack.enter_context(patch.object(bench.os, 'killpg', lambda *a: None))
        self.stack.enter_context(patch.object(bench.Bench, 'health', lambda s: True))
        self.stack.enter_context(patch.object(bench.Bench, 'stub_posts', lambda s: bench.read_json(str(self.sdir/'posts.json')) if (self.sdir/'posts.json').exists() else 0))

    def run_bench(self, mode='host', extra=(), row='echo'):
        os.environ['FAKE_MODE'] = mode
        self.stdout = io.StringIO()
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stdout):
            code = bench.main(['--mode',mode,'--row',row,'--session',str(self.sdir), *extra], mv)
        self.result = bench.read_json(str(self.sdir / ('bench-'+mode+'.json')))
        self.trace = [json.loads(x) for x in (self.sdir/'trace.jsonl').read_text().splitlines()] if (self.sdir/'trace.jsonl').exists() else []
        return code

    def fake_alarm(self, code=0, ready=True, alarm_posts=0, extra_posts=0):
        session = Mock()
        session.env = {'ALERT_CHANNEL': 'ALARM'}
        session.data = {}
        def trace(action):
            with (self.sdir/'trace.jsonl').open('a') as stream:
                stream.write(json.dumps(['alarm', action])+'\n')
        def register(name):
            path = str(self.sdir/'ledger.json')
            ledger = bench.read_json(path)
            ledger['pids'].append({'pid': 46000+len(ledger['pids']), 'name': name})
            bench.write_json(path, ledger)
        def restart(mode):
            trace(mode)
            register('alarm-app')
            register('alarm-app-jvm')
            path = self.sdir/'all'/'slack-lab.log'
            path.parent.mkdir(exist_ok=True)
            with path.open('a') as stream:
                stream.write(json.dumps({'msg':'워커 소비자 시작'})+'\n')
            session.data['mode'] = mode
            bench.write_json(str(self.sdir/'alarm-state.json'), session.data)
        def up():
            trace('up')
            register('alarm-slack')
            register('alarm-llm')
            restart('echo')
            session.data['ready'] = ready and code == 0
            bench.write_json(str(self.sdir/'alarm-state.json'), session.data)
            return code
        def posts(*args):
            return {'chat.postMessage': {'CSTUBVERIFY': bench.read_json(str(self.sdir/'posts.json')),
                                         'ALARM': alarm_posts, 'OTHER': extra_posts}}
        session.up.side_effect = up
        session.restart_app.side_effect = restart
        session.slack.request.side_effect = posts
        self.stack.enter_context(patch.object(bench._mon_alarm, 'AlarmSession', return_value=session))
        return session

    def test_b3_same_boot_order_counts_and_preserved_environment(self):
        session = self.fake_alarm()
        self.assertEqual(self.run_bench(row='echo-mon'), 0, self.stdout.getvalue())
        actions = [(r[0], r[1]) for r in self.trace]
        expected = [('hw-measure','start'), ('alarm','up'), ('alarm','echo'),
                    ('p1-load','warmup'), ('hw-measure','mark'), ('p1-load','seq'),
                    ('p1-load','burst'), ('p1-load','seq'), ('alarm','F-echo'),
                    ('p1-load','seq'), ('alarm','echo'), ('hw-measure','stop'), ('hw-measure','report')]
        filtered = [a for a in actions if a in expected]
        self.assertEqual(filtered, expected)
        session.up.assert_called_once_with()
        loads = [r for r in self.trace if r[:2]==['s6-session','spawn'] and any(x.startswith('load-') for x in r)]
        self.assertEqual(len(loads), 5)
        self.assertTrue(all('SLACK_TEST_CHANNEL=CSTUBVERIFY' in r for r in loads))
        self.assertEqual([c.args[0] for c in session.restart_app.call_args_list], ['F-echo','echo'])
        self.assertFalse(any('teardown' in r or 'compose' in r or 'bench-slack' in r for r in self.trace))
        self.assertEqual(self.result['round'], 'B3')
        self.assertEqual(self.result['row'], 'echo-mon')
        self.assertEqual(self.result['log_counts']['received'], 42)
        self.assertEqual(self.result['stub_posts_total'], 84)
        self.assertEqual(self.result['alarm_posts'], 0)
        self.assertEqual(self.result['hardware']['swap_growth_mib'], 1)
        state = bench.read_json(str(self.sdir/'alarm-state.json'))
        self.assertEqual(state, {'ready':True, 'mode':'echo'})
        names = [r['name'] for r in self.result['measurement_processes']]
        self.assertEqual(names.count('alarm-app-jvm'), 3)
        for name in ('hw-sampler','alarm-slack','alarm-llm','load-failure'):
            self.assertIn(name, names)
        self.assertTrue(self.result['teardown_pending'])
        entries = [json.loads(line) for line in (self.sdir/'bench-ledger.jsonl').read_text().splitlines()]
        self.assertEqual([e['round'] for e in entries], ['B3','B3'])

    def test_b3_rules_mismatch_stops_measurement_and_preserves_exit(self):
        self.fake_alarm(code=3)
        self.assertEqual(self.run_bench(row='echo-mon'), 3)
        self.assertEqual(self.result['alarm_up_exit'], 3)
        self.assertFalse(self.result['ok'])
        self.assertFalse(any(r[0]=='p1-load' for r in self.trace))
        self.assertEqual(sum(r[:2]==['hw-measure','stop'] for r in self.trace), 1)
        self.assertTrue(self.result['teardown_pending'])

    def test_b3_not_ready_is_not_a_successful_boot(self):
        self.fake_alarm(ready=False)
        self.assertEqual(self.run_bench(row='echo-mon'), 1)
        self.assertIn('ready', self.result['reason'])
        self.assertFalse(any(r[0]=='p1-load' for r in self.trace))

    def test_b3_up_exception_stops_only_sampler(self):
        session = self.fake_alarm()
        session.up.side_effect = RuntimeError('secret-value')
        self.assertEqual(self.run_bench(row='echo-mon'), 1)
        self.assertFalse(any(r[0]=='p1-load' or 'teardown' in r for r in self.trace))
        self.assertEqual(sum(r[:2]==['hw-measure','stop'] for r in self.trace), 1)
        self.assertNotIn('secret-value', json.dumps(self.result))

    def test_b3_dispatch_preserves_bench_arguments_and_exit(self):
        args = ['--mode','host','--row','echo-mon','--session',str(self.sdir)]
        with patch.dict(sys.modules, {mv.__name__: mv}), patch.object(bench, 'main', return_value=3) as dispatch:
            self.assertEqual(mv.main(['bench', *args]), 3)
        self.assertEqual(dispatch.call_args.args[0], args)

    def test_b3_alarm_posts_fail_with_reason_and_count(self):
        self.fake_alarm(alarm_posts=1)
        self.assertEqual(self.run_bench(row='echo-mon'), 1)
        self.assertEqual(self.result['alarm_posts'], 1)
        self.assertEqual(self.result['stub_posts_total'], 85)
        self.assertIn('알람 채널 게시', self.result['reason'])

    def test_b3_other_posts_fail_total_reconciliation(self):
        self.fake_alarm(extra_posts=1)
        self.assertEqual(self.run_bench(row='echo-mon'), 1)
        self.assertIn('테스트 채널 외', self.result['reason'])

    def test_b3_failure_evidence_still_required(self):
        self.fake_alarm()
        with patch.dict(os.environ, {'FAKE_BAD_COUNTS':'1'}):
            self.assertEqual(self.run_bench(row='echo-mon'), 1)
        self.assertEqual(self.result['failure_evidence']['retries'], 14)
        self.assertIn('유도 문제', self.result['reason'])

    def test_b3_hardware_failure_keeps_environment(self):
        self.fake_alarm()
        with patch.dict(os.environ, {'FAKE_HW_FAIL':'1'}):
            self.assertEqual(self.run_bench(row='echo-mon'), 1)
        self.assertTrue(bench.read_json(str(self.sdir/'alarm-state.json'))['ready'])
        self.assertEqual(self.result['hardware']['swap_growth_mib'], 1)

    def test_b3_invalid_mode_and_launcher_before_boot(self):
        for args in (['--mode','app'], ['--launcher','jar'], ['--build-first']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bench.main(['--mode','host','--row','echo-mon','--session',str(self.sdir), *args],mv),2)
        self.assertFalse((self.sdir/'trace.jsonl').exists())

    def test_host_order_and_wait(self):
        self.assertEqual(self.run_bench(), 0, self.stdout.getvalue())
        actions = [(r[0],r[1]) for r in self.trace]
        self.assertLess(actions.index(('hw-measure','start')), actions.index(('s6-session','compose')))
        self.assertLess(actions.index(('p1-load','warmup')),actions.index(('hw-measure','mark')))
        self.assertLess(actions.index(('hw-measure','mark')),actions.index(('p1-load','seq')))
        self.assertLess(max(i for i,r in enumerate(self.trace) if r[0]=='p1-load'),actions.index(('hw-measure','stop')))
        self.assertLess(actions.index(('hw-measure','stop')),actions.index(('hw-measure','report')))
        self.assertEqual(sum(self.clock.sleeps),1800)
        self.assertTrue(all(s<=5 for s in self.clock.sleeps))
        self.assertEqual(self.result['failure_evidence']['attempts'],20)
        self.assertTrue(any('bootRun' in r for r in self.trace))
        self.assertFalse(any('teardown' in r or 'build' in r for r in self.trace))
        pids = bench.read_json(str(self.sdir/'ledger.json'))['pids']
        self.assertTrue(any(r['name']=='hw-sampler' for r in pids))
        self.assertTrue(any(r.get('signal_pid') for r in pids))

    def test_app_recreate_no_automatic_build(self):
        self.assertEqual(self.run_bench('app'),0,self.stdout.getvalue())
        compose = [r for r in self.trace if r[:2]==['s6-session','compose']]
        self.assertFalse(any('build' in r for r in compose))
        recreate = next(r for r in compose if '--force-recreate' in r)
        self.assertIn('--no-deps',recreate)
        self.assertIn('--no-build',recreate)
        self.assertNotIn('rabbitmq',recreate)
        text = (self.sdir/'bench-override.yaml').read_text()
        self.assertEqual(text.count('LLM_CLIENT:'),3)
        self.assertIn('host.docker.internal:18962',text)
        self.assertNotIn('secret-value',text)

    def test_build_only_before_measurement(self):
        self.assertEqual(self.run_bench('app',['--build-first']),0)
        build = next(i for i,r in enumerate(self.trace) if 'build' in r)
        start = next(i for i,r in enumerate(self.trace) if r[:2]==['hw-measure','start'])
        self.assertLess(build,start)
        self.assertFalse(any('gradlew' in r for r in self.trace))
        self.assertEqual((self.sdir/'bench-app.jar').read_bytes(),b'jar')

    def test_image_missing_and_stale_fail_before_measurement(self):
        for key in ('FAKE_IMAGE_MISSING','FAKE_IMAGE_STALE'):
            with self.subTest(key=key), patch.dict(os.environ,{key:'1'}):
                self.assertEqual(self.run_bench('app'),1)
                self.assertFalse(any(r[:2]==['hw-measure','start'] for r in self.trace))
                self.assertFalse(any('build' in r for r in self.trace))

    def test_failure_count_mismatch(self):
        with patch.dict(os.environ,{'FAKE_BAD_COUNTS':'1'}):
            self.assertEqual(self.run_bench(),1)
        self.assertIn('유도 문제',self.result['reason'])
        self.assertEqual(self.result['failure_evidence']['retries'],14)
        self.assertTrue(any(r[:2]==['hw-measure','stop'] for r in self.trace))

    def test_preconditions(self):
        for key in ('FAKE_LISTENER','FAKE_OLLAMA','FAKE_CONTAINERS','FAKE_VOLUMES'):
            with self.subTest(key=key), patch.dict(os.environ,{key:'1'}):
                self.assertEqual(self.run_bench(),1)
                self.assertFalse(any(r[:2]==['hw-measure','start'] for r in self.trace))

    def test_missing_session_jar(self):
        (self.sdir/'jar.json').unlink()
        self.assertEqual(self.run_bench(),1)
        self.assertEqual(self.trace,[])

    def test_secret_stderr_hidden(self):
        with patch.dict(os.environ,{'FAKE_BUILD_FAIL':'1'}):
            self.assertEqual(self.run_bench('app',['--build-first']),1)
        output = self.stdout.getvalue()+json.dumps(self.result)
        for secret in ('secret-value','channel-private','EvPRIVATE'):
            self.assertNotIn(secret,output)
        self.assertFalse(any(r[:2]==['hw-measure','start'] for r in self.trace))

    def test_hardware_failure(self):
        with patch.dict(os.environ,{'FAKE_HW_FAIL':'1'}):
            self.assertEqual(self.run_bench(),1)
        self.assertFalse(self.result['ok'])

    def test_host_launchers(self):
        for launcher, command in (('jar', 'java'), ('verifyBootRun', 'verifyBootRun')):
            with self.subTest(launcher=launcher):
                opts = argparse.Namespace(session=str(self.sdir), out=None, mode='host', launcher=launcher, build_first=False)
                with patch.object(bench._mon_env, 'jar_env', return_value={}):
                    b = bench.Bench(opts, mv)
                b.meta = bench.read_json(str(self.sdir/'jar.json'))
                b.stub_app = 'http://127.0.0.1:18962'
                captured = []
                with patch.object(b, 'spawn', lambda name, cmd, extra: captured.append((cmd,extra)) or 41000), \
                        patch.object(b, 'wait'), patch.object(b, 'register'), \
                        patch.object(b, 'call', return_value=argparse.Namespace(stdout='44000\n')):
                    b.start_app()
                self.assertIn(command,captured[0][0])
                self.assertEqual(captured[0][1]['APP_ROLE'],'all')
                self.assertEqual(captured[0][1]['SLACK_LAB_VERIFY_PAUSE_FLAG'],str(self.sdir.resolve()/'pause.flag'))

    def test_exception_keeps_process_ledger_for_teardown(self):
        with patch.object(bench.Bench, 'start_app', side_effect=RuntimeError('secret-value')):
            self.assertEqual(self.run_bench(),1)
        names = [e['name'] for e in bench.read_json(str(self.sdir/'ledger.json'))['pids']]
        self.assertIn('hw-sampler',names)
        self.assertIn('bench-slack',names)
        self.assertTrue(self.result['teardown_pending'])
        self.assertTrue(any(r[:2]==['hw-measure','stop'] for r in self.trace))
        self.assertNotIn('secret-value',self.stdout.getvalue())
        entries = [json.loads(line) for line in (self.sdir/'bench-ledger.jsonl').read_text().splitlines()]
        self.assertEqual([e['event'] for e in entries],['start','measurement_finished'])
        self.assertFalse(entries[-1]['ok'])

    def test_teardown_signals_owned_jvm_directly(self):
        loader = importlib.machinery.SourceFileLoader('bench_s6',str(HERE/'s6-session'))
        s6 = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name,loader))
        loader.exec_module(s6)
        with patch.object(s6.os,'kill') as kill, patch.object(s6.os,'killpg') as killpg:
            ledger = {'pids':[{'pid':100,'signal_pid':True},{'pid':200}]}
            s6.signal_owned(ledger,100,15)
            s6.signal_owned(ledger,200,15)
        kill.assert_called_once_with(100,15)
        killpg.assert_called_once_with(200,15)

    def test_usage(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bench.main([],mv),2)
            self.assertEqual(bench.main(['--mode','host','--row','echo','--session',str(self.sdir),'--minutes','nan'],mv),2)

    def test_failure_counter_real_fields(self):
        rows=[]
        for _ in range(5):
            rows.append({'msg':'slack 수신 retry_num=null'})
            for j in range(4):
                rows.append({'event':'processing_metric','fields':{'kind':'failure_notice' if j==3 else '-', 'result':'Delivered' if j==3 else 'RetryRequested'}})
                if j<3: rows.append({'msg':'LLM 재시도 가능한 실패 → 재시도 예약'})
            rows.append({'event':'llm_final_failure'})
        got,ok=bench.failure_evidence(rows,{'failure_notice':5,'ok':0})
        self.assertTrue(ok)
        self.assertEqual(got,bench.EXPECTED_FAILURE)
        rows.pop()
        self.assertFalse(bench.failure_evidence(rows,{'failure_notice':5,'ok':0})[1])


if __name__=='__main__':
    unittest.main()
