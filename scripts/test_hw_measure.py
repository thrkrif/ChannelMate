"""scripts/hw-measure 단위 검증(6단계 M48 C8). 명령 출력 픽스처 파싱·판정만 본다. 외부 호출 0. 실행: python3 scripts/test_hw_measure.py"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("hw", os.path.join(HERE, "hw-measure"))
hw = importlib.util.module_from_spec(importlib.util.spec_from_loader("hw", loader))
loader.exec_module(hw)

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


STATS = '\n'.join(json.dumps(r) for r in [
    {"Name": "slack-lab-s6v-x-worker-1", "MemUsage": "412.5MiB / 1GiB", "MemPerc": "40.3%"},
    {"Name": "slack-lab-s6v-x-loki-1", "MemUsage": "1.2GiB / 1GiB"},
    {"Name": "n", "MemUsage": "800kB / 1GiB"}]) + "\nnot json\n"
st = hw.parse_docker_stats(STATS)
check("docker stats: 이름별 바이트(MiB·GiB·kB)", st["slack-lab-s6v-x-worker-1"] == int(412.5 * 1024 ** 2) and st["slack-lab-s6v-x-loki-1"] == int(1.2 * 1024 ** 3)
      and st["n"] == 800_000, str(st))
check("docker stats: JSON이 아닌 줄은 건너뛴다", len(st) == 3)

ps = hw.parse_ps("  1024 /usr/bin/java\n  2048 java\n 4096 /Applications/Ollama.app/ollama\n 99 bash\n  70 ollamactl\n")
check("ps: java·ollama RSS 합(KiB), 비슷한 이름은 제외", ps == {"java": 3072, "ollama": 4096}, str(ps))

VM = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:                               5000.\nPages active:                            20000.\nPages inactive:                          10000.\nPages wired down:                         8000.\nPages occupied by compressor:            30000.\n"
vm = hw.parse_vm_stat(VM)
check("vm_stat: 쪽 크기 16384 곱", vm["free"] == 5000 * 16384 and vm["compressed"] == 30000 * 16384 and set(vm) == {"free", "active", "inactive", "wired", "compressed"}, str(vm))
check("vm_stat: 머리줄이 없으면 16384 가정", hw.parse_vm_stat("Pages free: 10.\n")["free"] == 10 * 16384)

check("swap: M·G 단위", hw.parse_swap("vm.swapusage: total = 2048.00M  used = 1164.25M  free = 883.75M  (encrypted)") == 1164.25
      and hw.parse_swap("vm.swapusage: total = 4.00G  used = 1.00G  free = 3.00G") == 1024.0 and hw.parse_swap("") is None)
check("ollama ps: 적재 모델 이름", hw.parse_ollama_ps("NAME ID SIZE PROCESSOR UNTIL\nqwen2.5:7b abc 5.4 GB 100% GPU 4 minutes\nbge-m3 def 1 GB 100% GPU 2 minutes\n")
      == ["qwen2.5:7b", "bge-m3"] and hw.parse_ollama_ps("NAME ID SIZE PROCESSOR UNTIL\n") == [])
check("inspect: 재시작 합·OOM 수", hw.parse_inspect("/a 0 false\n/b 2 true\n") == (2, 1))


def sample(t, mem, swap, restarts=0, ooms=0, total=4 * 1024 ** 3):
    return {"t": t, "containers": {"a": mem}, "docker_vm_total": total, "swap_used_mib": swap, "restart_count": restarts, "oom_killed": ooms}


good = [sample(0, 1 * 1024 ** 3, 1000), sample(10, 3 * 1024 ** 3, 1100), sample(20, int(3.2 * 1024 ** 3), 1300)]
v = hw.judge(good, warmup_t=10)
check("판정: 스왑 증가 200MiB·VM 80% 이하·안정 → 합격", v["ok"] and v["swap_growth_mib"] == 200 and v["vm_ok"], str(v))
check("판정: Docker VM 최고 81% → 미달", not hw.judge([sample(0, int(3.3 * 1024 ** 3), 1000)], warmup_t=0)["ok"])
check("판정: 워밍업 뒤 스왑 증가 513MiB → 미달", not hw.judge([sample(0, 1, 1000), sample(10, 1, 1513)], warmup_t=0)["swap_ok"])
check("판정: 워밍업 전 스왑 증가는 세지 않는다", hw.judge([sample(0, 1, 1000), sample(5, 1, 2500), sample(10, 1, 2500), sample(15, 1, 2600)], warmup_t=10)["swap_ok"])
check("판정: 재시작 증가나 OOM이 있으면 미달", not hw.judge([sample(0, 1, 1, 0), sample(5, 1, 1, 1)], warmup_t=0)["stable_ok"]
      and not hw.judge([sample(0, 1, 1, 0, 1)], warmup_t=0)["stable_ok"])
check("판정: 워밍업 표시가 없으면 합격시키지 않는다", not hw.judge(good, warmup_t=None)["ok"])
check("판정: 표본이 없으면 미달", not hw.judge([])["ok"])

# take_sample: 명령 실행기를 가짜로 바꿔 한 번 조립
fake = {"docker stats": STATS, "docker ps": "abc\n", "docker inspect": "/x 1 false\n", "docker info": "4294967296\n",
        "ps": "100 java\n", "vm_stat": VM, "sysctl": "vm.swapusage: total = 1M  used = 500.00M  free = 1M", "ollama": "NAME\nm 1 1 GB 100% GPU 1 m\n"}
hw.run = lambda cmd, timeout=20: fake.get(" ".join(cmd[:2]) if cmd[0] == "docker" else cmd[0], "")
s = hw.take_sample(now=5.0)
check("표본 조립: 모든 칸이 채워진다", s["t"] == 5.0 and s["docker_vm_total"] == 4294967296 and s["swap_used_mib"] == 500.0
      and s["ollama_models"] == ["m"] and s["restart_count"] == 1 and s["ps_rss_kib"]["java"] == 100, str(s))

with tempfile.TemporaryDirectory() as d:
    open(hw.paths(d)["samples"], "w", encoding="utf-8").write("\n".join(json.dumps(x) for x in good) + "\nbroken\n")
    open(hw.paths(d)["warmup"], "w", encoding="utf-8").write("10")
    check("report: 깨진 줄을 건너뛰고 합격 판정은 종료 코드 0", hw.main(["report", "--out", d, "--json"]) == 0)
    check("사용법 오류는 2", hw.main([]) == 2 and hw.main(["start"]) == 2 and hw.main(["mark", "x", "--out", d]) == 2)
print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
