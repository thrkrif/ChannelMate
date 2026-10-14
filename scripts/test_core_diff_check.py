"""scripts/core-diff-check 단위 시험(임시 git 저장소 픽스처, 외부 호출 없음). 실행: python3 scripts/test_core_diff_check.py"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "core-diff-check")
CORE = "src/main/java/com/slack/lab/core"
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


def sh(repo, *cmd):
    subprocess.run(cmd, cwd=repo, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def put(repo, rel, text):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)


def run(repo, *args):
    p = subprocess.run([sys.executable, SCRIPT, "--repo", repo, *args], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def make_repo():
    repo = tempfile.mkdtemp()
    sh(repo, "git", "init", "-q", "-b", "develop")
    put(repo, CORE + "/A.java", "class A {}\n")
    put(repo, "src/main/java/com/slack/lab/adapter/B.java", "class B {}\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "commit", "-q", "-m", "base")
    sh(repo, "git", "checkout", "-q", "-b", "feature/x")
    return repo


repo = make_repo()
rc, out = run(repo)
check("변경 없음 → 통과", rc == 0 and "코어 diff 0" in out, out)

put(repo, "src/main/java/com/slack/lab/adapter/B.java", "class B { int x; }\n")
sh(repo, "git", "commit", "-q", "-am", "adapter")
rc, out = run(repo)
check("코어 밖 변경 → 통과", rc == 0, out)

put(repo, CORE + "/A.java", "class A { int y; }\n")
rc, out = run(repo)
check("작업 트리의 코어 변경 → 실패", rc == 1 and "A.java" in out, out)
sh(repo, "git", "commit", "-q", "-am", "core")
rc, out = run(repo)
check("커밋된 코어 변경 → 실패", rc == 1 and "A.java" in out, out)

rc, out = run(repo, "--range", "HEAD~2..HEAD~1")
check("범위 지정: 코어를 안 건드린 구간 → 통과", rc == 0, out)
rc, out = run(repo, "--range", "HEAD~1..HEAD")
check("범위 지정: 코어를 건드린 구간 → 실패", rc == 1, out)

put(repo, CORE + "/New.java", "class New {}\n")
sh(repo, "git", "reset", "-q", "--hard", "develop")
rc, out = run(repo)
check("추적 안 되는 새 코어 파일 → 실패", rc == 1 and "New.java" in out, out)

rc, out = run(repo, "--bogus")
check("알 수 없는 옵션 → 종료 코드 2", rc == 2, out)
rc, out = run(repo, "--range", "nope..nope2")
check("잘못된 범위 → 종료 코드 2", rc == 2, out)

print("전체", "PASS" if all(results) else "FAIL", sum(results), "/", len(results))
sys.exit(0 if all(results) else 1)
