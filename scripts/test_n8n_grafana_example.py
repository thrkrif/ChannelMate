"""n8n Grafana 경유 예제(docs/n8n/grafana-relay.json)의 오프라인 검증(6단계 M48 C8). 외부 호출 0.
노드 구성·원형 본문 전달·응답 상태 전달·Credential/비밀 0건. 실행: python3 scripts/test_n8n_grafana_example.py"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PATH = os.path.join(ROOT, "docs", "n8n", "grafana-relay.json")
results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, "" if cond else detail)


text = open(PATH, encoding="utf-8").read()
wf = json.loads(text)
nodes = {n["name"]: n for n in wf["nodes"]}
kinds = [n["type"].split(".")[-1] for n in wf["nodes"]]
check("노드 3종(webhook·httpRequest·respondToWebhook)", sorted(kinds) == ["httpRequest", "respondToWebhook", "webhook"], str(kinds))

order, cur = [], "Grafana 웹훅"
while cur:
    order.append(cur)
    nxt = wf["connections"].get(cur, {}).get("main", [[]])[0]
    cur = nxt[0]["node"] if nxt else None
check("연결 순서: 웹훅 → 호출 → 응답", [nodes[n]["type"].split(".")[-1] for n in order] == ["webhook", "httpRequest", "respondToWebhook"], str(order))
check("가운데에 값을 바꾸는 노드(Code·Set·IF 등)가 없다", all(k in ("webhook", "httpRequest", "respondToWebhook") for k in kinds))

hook = nodes["Grafana 웹훅"]["parameters"]
check("웹훅: POST, Header Auth, 응답은 Respond 노드가 한다", hook["httpMethod"] == "POST" and hook["authentication"] == "headerAuth"
      and hook["responseMode"] == "responseNode", str(hook))

http = nodes["알람 API 호출"]["parameters"]
check("호출: POST /alerts/grafana, Header Auth, host.docker.internal", http["method"] == "POST" and http["url"].endswith("/alerts/grafana")
      and http["genericAuthType"] == "httpHeaderAuth" and "host.docker.internal" in http["url"], str(http))
check("본문은 웹훅 본문 그대로(JSON.stringify($json.body))", http["jsonBody"] == "={{ JSON.stringify($json.body) }}", http.get("jsonBody"))
resp_opts = http["options"]["response"]["response"]
check("응답 전체를 받고 4xx/5xx에도 중단하지 않는다(상태 전달의 전제)", resp_opts.get("fullResponse") is True and resp_opts.get("neverError") is True, str(resp_opts))

back = nodes["수신 응답 그대로 반환"]["parameters"]
check("반환: 상태 코드는 $json.statusCode, 본문은 $json.body", back["options"]["responseCode"] == "={{ $json.statusCode }}"
      and back["responseBody"] == "={{ $json.body }}", str(back))
check("503을 200으로 바꾸는 고정 상태 코드가 없다", not re.search(r'"responseCode"\s*:\s*"?\d{3}', text))

check("Credential 데이터·ID 참조가 없다", "credentials" not in text and not any("credentials" in n for n in wf["nodes"]))
check("Slack 토큰 패턴 0건", not re.search(r"xox[a-z]-", text))
real = {}
envfile = os.path.join(ROOT, ".env")
if os.path.exists(envfile):
    for line in open(envfile, encoding="utf-8"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            real[k.strip()] = v.strip().strip('"').strip("'")
leaked = [k for k in ("ALERT_SECRET", "MANAGEMENT_METRICS_TOKEN", "GRAFANA_ADMIN_PASSWORD", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET")
          if len(real.get(k, "")) >= 6 and real[k] in text]
check("실제 .env의 비밀값이 JSON에 없다(값은 출력하지 않는다)", not leaked, f"키: {leaked}")
print(f"\n{sum(results)}/{len(results)} 통과")
sys.exit(0 if all(results) else 1)
