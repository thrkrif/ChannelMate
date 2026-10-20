"""검증 전용 소스셋 계약(6단계 M48): 운영 jar에 검증 코드가 없고, 설정에 verify 키가 없으며, 검증 클래스패스에 테스트 출력이 없다.
`./gradlew bootJar -x test`를 한 번 돌린다(외부 호출 없음). 실행: python3 scripts/test_verify_sourceset.py"""
import glob
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


subprocess.run(["./gradlew", "bootJar", "-x", "test", "-q"], cwd=ROOT, check=True, capture_output=True)
jars = [j for j in glob.glob(os.path.join(ROOT, "build", "libs", "*.jar")) if not j.endswith("-plain.jar")]
check("bootJar가 하나 있다", len(jars) == 1, jars)
with zipfile.ZipFile(jars[0]) as z:
    names = z.namelist()
    check("bootJar 안 com/slack/labverify/ 0건", not [n for n in names if "com/slack/labverify/" in n])
    check("bootJar의 application.yml에 verify. 키 0건",
          not re.search(r"(?m)^\s*verify\s*:", z.read("BOOT-INF/classes/application.yml").decode("utf-8")))

gradle = open(os.path.join(ROOT, "build.gradle"), encoding="utf-8").read()
verify_block = gradle[gradle.index("sourceSets {\n    verify"):]
check("verify 소스셋은 main 출력만 클래스패스에 더한다",
      "compileClasspath += sourceSets.main.output" in verify_block and "sourceSets.test" not in verify_block)
check("테스트 출력이 verify 런타임 클래스패스에 없다", not re.search(r"verify\w*Classpath[^\n]*sourceSets\.test", gradle))
check("ArchitectureTest가 운영→검증 의존 금지 규칙을 가진다",
      "com.slack.labverify" in open(os.path.join(ROOT, "src/test/java/com/slack/lab/ArchitectureTest.java"), encoding="utf-8").read())
print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
