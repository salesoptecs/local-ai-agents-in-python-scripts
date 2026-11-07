"""Run every scenario x model through healer.py (each in its own process) and score against answer_key.json.
usage: python run_experiment.py <model> [<model> ...] [--mode guarded|naive] [--only s1,s2]"""
import json, math, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = json.load(open(os.path.join(HERE, "answer_key.json"), encoding="utf-8"))
args = sys.argv[1:]
mode = args[args.index("--mode") + 1] if "--mode" in args else "guarded"
only = args[args.index("--only") + 1].split(",") if "--only" in args else None
models = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] not in ("--mode", "--only"))]


def same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        if (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b)):
            return False
        return abs(a - b) <= 0.01 + 1e-6 * abs(b)
    return a == b


def diff(rep, key):
    out = []
    for k in key:
        if not same(rep.get(k), key[k]):
            out.append(k if isinstance(key[k], dict) else f"{k}: {rep.get(k)} vs {key[k]}")
    return out


def score(log, key):
    st = log.get("status")
    if key is None:
        return "correct (gave up)" if st == "gave_up" else f"WRONG ({st})"
    if st == "healed":
        return "correct" if same(log["report"], key) else "SILENT DAMAGE (passed checks)"
    if st == "blocked_by_contract":
        return "blocked by checks"
    return st


os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
for model in models:
    rows = []
    for sc, key in KEY.items():
        if sc == "clean" or (only and sc not in only):
            continue
        t = time.time()
        try:
            p = subprocess.run([sys.executable, os.path.join(HERE, "healer.py"), sc, model, mode], capture_output=True, text=True,
                               timeout=int(os.environ.get("HEAL_TIMEOUT", "1800")))
            log = json.loads(p.stdout.strip().splitlines()[-1])
        except subprocess.TimeoutExpired:
            log = {"status": "timeout"}
        except Exception:
            log = {"status": "harness_error", "stderr": p.stderr[-500:]}
        log["score"] = score(log, key)
        fr = log.get("first_report")
        log["unguarded"] = ("n/a" if key is None else "correct" if fr and same(fr, key) else
                            ("damaged: " + "; ".join(diff(fr, key))[:160]) if fr else "no report")
        log["wall_seconds"] = round(time.time() - t, 1)
        rows.append(log)
        print(f"{model:20} {sc:20} {log['score']:30} llm {log.get('llm_seconds', 0):6}s  unguarded: {log['unguarded']}", flush=True)
    tag = model.replace(":", "_").replace("/", "_")
    rep = f"_t{os.environ['HEAL_TEMP']}_s{os.environ.get('HEAL_SEED', '1')}" if os.environ.get("HEAL_TEMP") else ""
    rep += ("_ctx" if os.environ.get("HEAL_CONTEXT") else "")
    rep += ("_guard" if os.environ.get("HEAL_GUARD") == "1" else "") + {"1": "_tri", "code": "_tricode"}.get(os.environ.get("HEAL_TRIAGE", ""), "") + (f"_think{os.environ['HEAL_THINK']}" if os.environ.get("HEAL_THINK") else "")
    rep += ("_prof" if os.environ.get("HEAL_PROFILE") == "1" else "") + (f"_n{os.environ['HEAL_N']}" if os.environ.get("HEAL_N", "1") != "1" else "")
    json.dump(rows, open(os.path.join(HERE, "results", f"{tag}_{mode}{rep}.json"), "w"), indent=1, default=str)
