#!/usr/bin/env python3
"""
KubeChaos Advisor - tells you which container orchestrator to use
(Kubernetes, K3s, MicroK8s, Docker Swarm or Nomad) for YOUR use case.

It mixes two things:
  1. Numbers MEASURED by KubeChaos (speed, latency, crash recovery, memory, start-up)
  2. Ratings from the official docs (ease, ecosystem, scale, edge, non-container jobs)
Your answers decide how much each one counts. Same engine as the website (site/index.html).

Usage:
  python3 advisor.py                       # asks you 6 questions
  python3 advisor.py --use edge --ram small --nodes few --care memory,recovery --exp none --noncont no
  python3 advisor.py --results results/all.json   # use your own KubeChaos measurements
  python3 advisor.py --list                # show all question and answer ids
  python3 advisor.py --json ...            # machine-readable output

Only the Python standard library is needed.
"""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "advisor_data.json")
KEYS = ["startup_s", "deploy_s", "memory_mb", "normal_rps", "heavy_rps",
        "heavy_p95_ms", "recovery_s", "crash_failed_pct"]


def load_data(results=None):
    with open(DATA_FILE) as f:
        data = json.load(f)
    if results and os.path.exists(results):
        with open(results) as f:
            mine = json.load(f).get("tools", {})
        rename = {"kind": "kubernetes"}
        for k, t in mine.items():
            key = rename.get(k, k)
            if key in data["measured"]:
                for m in KEYS:
                    if t.get(m) is not None:
                        data["measured"][key][m] = t[m]
        data["version"] = f"your own results ({results})"
    return data


def ratio(data, metric, value):
    """0-100. The best tool gets 100, the others get their share of the best."""
    vals = [t[metric] for t in data["measured"].values() if t.get(metric) is not None]
    if value is None or not vals:
        return None
    if data["higher_is_better"].get(metric):
        best = max(vals)
        return 100.0 if best <= 0 else 100.0 * value / best
    # lower is better: softened ratio, so tiny absolute gaps (0.8 s vs 4.4 s) do not look huge
    k = data.get("soften", {}).get(metric, 0)
    best = min(vals)
    if value + k <= 0:
        return 100.0
    return 100.0 * (best + k) / (value + k)


def criterion_score(data, crit, tool):
    c = data["criteria"][crit]
    if c["source"] == "rated":
        return data["rated"][crit][tool]
    total = wsum = 0.0
    for metric, w in c["parts"].items():
        s = ratio(data, metric, data["measured"][tool].get(metric))
        if s is not None:
            total += w * s
            wsum += w
    return total / wsum if wsum else None


def weights_from(data, answers):
    w = {c: 0.0 for c in data["criteria"]}
    for q in data["questions"]:
        picked = answers.get(q["id"])
        picked = picked if isinstance(picked, list) else [picked]
        for opt in q["options"]:
            if opt["id"] in picked:
                for c, v in opt["w"].items():
                    w[c] += v
    return w


def advise(data, answers):
    w = weights_from(data, answers)
    wsum_all = sum(w.values()) or 1.0
    out = []
    for tool in data["tools"]:
        total = wsum = 0.0
        parts, missing = {}, []
        for c, wc in w.items():
            s = criterion_score(data, c, tool)
            parts[c] = None if s is None else round(s, 1)
            if wc == 0:
                continue
            if s is None:          # not measured yet: count it as average, never as a bonus
                missing.append(c)
                s = 50.0
            total += wc * s
            wsum += wc
        score = total / wsum if wsum else 0.0
        notes = []
        for r in data["rules"]:
            if r["tool"] == tool and all(answers.get(k) == v for k, v in r["if"].items()):
                score *= r["factor"]
                notes.append(r["note"])
        # why: the criteria with the biggest weight where this tool is strong
        strong = sorted([c for c in w if w[c] > 0 and (parts[c] or 0) >= 85],
                        key=lambda c: -w[c] * (parts[c] or 0))
        weak = sorted([c for c in w if w[c] > 0 and parts[c] is not None and parts[c] < 50],
                      key=lambda c: -w[c])
        out.append({"tool": tool, "name": data["tools"][tool]["name"], "score": round(score, 1),
                    "parts": parts, "strong": strong[:3], "weak": weak[:2],
                    "missing": missing, "notes": notes})
    out.sort(key=lambda r: -r["score"])
    return {"weights": {c: round(v / wsum_all * 100) for c, v in w.items() if v}, "ranking": out}


def fact(data, crit, tool):
    m = data["measured"][tool]
    f = {
        "speed": lambda: f"{m['heavy_rps']:,.0f} requests/s under heavy load" if m.get("heavy_rps") else None,
        "latency": lambda: f"95% of requests answered within {m['heavy_p95_ms']} ms" if m.get("heavy_p95_ms") else None,
        "recovery": lambda: f"replaced a crashed copy in {m['recovery_s']} s" if m.get("recovery_s") else None,
        "memory": lambda: f"used {m['memory_mb']:.0f} MB when idle" if m.get("memory_mb") else None,
        "setup": lambda: f"ready in {m['startup_s']} s" if m.get("startup_s") is not None else None,
    }.get(crit, lambda: None)()
    return f or data["rated_why"].get(crit, {}).get(tool, "")


def ask(data):
    answers = {}
    print("\nKubeChaos Advisor - answer 6 quick questions (press Enter for the default)\n")
    total = len(data["questions"])
    for qn, q in enumerate(data["questions"], 1):
        pick = "pick one or more of the options" if q["type"] == "many" else "pick one of the options"
        print(f"Q{qn}/{total}. {q['text'].replace(' Pick any.', '')}  ({pick})")
        for i, o in enumerate(q["options"], 1):
            print(f"  {i}. {o['label']}")
        d = data["defaults"][q["id"]]
        dflt = ",".join(str(i) for i, o in enumerate(q["options"], 1)
                        if o["id"] in (d if isinstance(d, list) else [d]))
        n = len(q["options"])
        hint = f"1-{n}, you can pick more than one, like 1,3" if q["type"] == "many" else f"1-{n}"
        while True:
            raw = input(f"  Your choice ({hint}) [{dflt}]: ").strip() or dflt
            try:
                idx = [int(x) for x in raw.replace(" ", "").split(",") if x]
                ok = idx and all(1 <= i <= n for i in idx) and (q["type"] == "many" or len(idx) == 1)
                ids = [q["options"][i - 1]["id"] for i in dict.fromkeys(idx)] if ok else []
                if ids:
                    break
            except ValueError:
                pass
            if q["type"] == "many":
                print(f"  Please select one or more of the given options: numbers from 1 to {n}, separated by commas (for example 1,3).")
            else:
                print(f"  Please select one of the given options: type one number from 1 to {n}.")
        answers[q["id"]] = ids if q["type"] == "many" else ids[0]
        print()
    return answers


def show(data, result):
    crit = data["criteria"]
    print("What counts for you:  " + ", ".join(f"{crit[c]['label']} {p}%" for c, p in
                                            sorted(result["weights"].items(), key=lambda kv: -kv[1])))
    print()
    for i, r in enumerate(result["ranking"], 1):
        bar = "#" * int(r["score"] / 4)
        print(f"  {i}. {r['name']:<13} {r['score']:5.1f}  {bar}")
    best = result["ranking"][0]
    t = data["tools"][best["tool"]]
    print(f"\nRecommendation: use {best['name']}.  {t['summary']}")
    if best["strong"]:
        print("Why:")
        for c in best["strong"]:
            print(f"  + {crit[c]['label']}: {fact(data, c, best['tool'])}")
    for c in best["weak"]:
        print(f"  - Watch out, {crit[c]['label'].lower()}: {fact(data, c, best['tool'])}")
    for n in best["notes"]:
        print(f"  ! {n}")
    if best["missing"]:
        print("  (not measured yet: " + ", ".join(crit[c]["label"] for c in best["missing"]) + ")")
    print(f"\nGet started:  {t['install']}\nDocs:         {t['docs']}")
    print(f"\nData: {data['version']}\n")


def main(argv=None):
    data0 = load_data()
    p = argparse.ArgumentParser(description="Recommend a container orchestrator for your use case")
    for q in data0["questions"]:
        p.add_argument("--" + q["id"], help=q["text"] + " (" + "|".join(o["id"] for o in q["options"]) + ")")
    p.add_argument("--results", help="use your own KubeChaos results/all.json")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--list", action="store_true", help="list question and answer ids")
    a = p.parse_args(argv)
    data = load_data(a.results)
    if a.list:
        for q in data["questions"]:
            print(f"--{q['id']:<8} {q['text']}")
            for o in q["options"]:
                print(f"     {o['id']:<10} {o['label']}")
        return
    given = {q["id"]: getattr(a, q["id"]) for q in data["questions"] if getattr(a, q["id"])}
    if given:
        answers = dict(data["defaults"])
        for k, v in given.items():
            answers[k] = v.split(",") if k == "care" else v
    elif sys.stdin.isatty():
        answers = ask(data)
    else:
        answers = dict(data["defaults"])
    result = advise(data, answers)
    if a.json:
        print(json.dumps({"answers": answers, **result}, indent=2))
    else:
        show(data, result)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
