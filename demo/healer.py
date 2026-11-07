"""Self-healing wrapper: run pipeline.build_report; when it crashes, ask a LOCAL model (Ollama, CPU only, nothing leaves
the machine) for a replacement of the function that failed, swap it in, re-run, and only release the report if it
passes a validation contract built from the raw file (no answer key).

usage: python healer.py <scenario> <model> [guarded|naive]   -> prints one JSON line"""
import csv, importlib, inspect, io, json, os, re, sys, time, traceback
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/generate"
MAX_ROUNDS = 3
SHOW = lambda event, **kw: None                   # heal_demo.py sets this to print each step on screen

SYSTEM = "You fix Python functions in a pandas data pipeline. Reply with code only."
RULES = {
    "guarded": ("Rules: return ONLY the corrected function in one ```python block, with the same name and signature. "
                "Keep what the function is for. Never drop rows. Never hide errors with try/except. If a value can't be "
                "read, keep the row and leave that value missing. If the error is not caused by the code (missing file, "
                "network, permissions, login), reply exactly: CANNOT_FIX: <reason>"),
    "naive": "Return the corrected function in one ```python block.",
}


def raw_facts(path):
    """What the raw file says, for the contract: row count, how many amount cells hold a number, any 'won' stage."""
    text = open(path, "rb").read().decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    cols = rows[0].keys() if rows else []
    amt_col = next((c for c in cols if "amount" in c.lower()), None)
    stage_col = next((c for c in cols if "stage" in c.lower()), None)
    return {"rows": len(rows),
            "amount_cells_with_digits": sum(1 for r in rows if amt_col and re.search(r"\d", r[amt_col] or "")),
            "has_won_stage": any("won" in (r[stage_col] or "").lower() for r in rows) if stage_col else False}


def contract(report, facts):
    fails = []
    if report["rows"] != facts["rows"]:
        fails.append(f"row count {report['rows']} != {facts['rows']} rows in the file")
    if report["rows"] - report["missing_amount_rows"] != facts["amount_cells_with_digits"]:
        fails.append(f"{report['rows'] - report['missing_amount_rows']} amounts parsed, but {facts['amount_cells_with_digits']} amount cells contain a number")
    if report["won_count"] + report["lost_count"] + report["open_count"] != report["rows"]:
        fails.append("won + lost + open != rows")
    if facts["has_won_stage"] and report["won_count"] == 0:
        fails.append("file has won deals but the report counts 0 won")
    if any(not re.fullmatch(r"20\d\d-Q[1-4]", k) for k in report["won_amount_by_quarter"]):
        fails.append("won amount has quarters that aren't real dates: " + ", ".join(k for k in report["won_amount_by_quarter"] if not re.fullmatch(r"20\d\d-Q[1-4]", k)))
    if any(v is not None and not (0 <= v <= 1) for v in report["win_rate_by_rep"].values() if v == v):
        fails.append("a win rate is outside 0..1")
    return fails


def failing_function(exc, mod):
    names = [f.name for f in traceback.extract_tb(exc.__traceback__) if f.filename.endswith("pipeline.py") or f.filename == "<string>"]   # <string> = a swapped-in fix
    funcs = [n for n in names if inspect.isfunction(getattr(mod, n, None)) and n != "build_report"]
    return funcs[-1] if funcs else None


TEMP = float(os.environ.get("HEAL_TEMP", "0"))     # repeat runs: HEAL_TEMP=0.7 HEAL_SEED=1..3
SEED = int(os.environ.get("HEAL_SEED", "1"))
EMPTY = os.path.join(HERE, "_claude_cwd")
CONTEXT = os.environ.get("HEAL_CONTEXT", "")       # "globals": also show the module constants the function uses


def used_globals(mod, fn):
    """Module-level constants (dicts, lists, strings...) the failing function reads, e.g. STAGE_OUTCOME."""
    f = getattr(mod, fn)
    out, codes, names = [], [f.__code__], []
    while codes:                                   # list comprehensions are nested code objects on 3.10
        c = codes.pop()
        names += c.co_names
        codes += [k for k in c.co_consts if inspect.iscode(k)]
    for n in dict.fromkeys(names):
        v = f.__globals__.get(n)
        if v is not None and not callable(v) and not inspect.ismodule(v):
            out.append(f"{n} = {v!r}")
    return ("\nModule constants this function uses:\n```python\n" + "\n".join(out) + "\n```\n") if out else ""


def ask(model, prompt):
    t = time.time()
    if model.startswith("claude-"):
        # reference run only (synthetic data): Claude Code CLI on the subscription, no tools, empty working folder
        import shutil, subprocess
        os.makedirs(EMPTY, exist_ok=True)
        cmd = [shutil.which("claude") or "claude", "-p", "--model", model, "--disallowedTools",
               "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Task"]
        p = subprocess.run(cmd, input=SYSTEM + chr(10) * 2 + prompt + chr(10), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=EMPTY, timeout=600)
        if p.returncode != 0:
            raise RuntimeError("claude failed: " + (p.stderr or p.stdout)[:200])
        return p.stdout.strip(), round(time.time() - t, 1)
    r = requests.post(OLLAMA, json={"model": model, "system": SYSTEM, "prompt": prompt, "stream": False,
                                    "options": {"temperature": TEMP, "seed": SEED, "num_ctx": 4096}}, timeout=900)
    r.raise_for_status()
    return r.json()["response"], round(time.time() - t, 1)


def extract_code(reply):
    m = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.S)
    return m.group(1) if m else None


def run(scenario, model, mode="guarded"):
    path = scenario if scenario.endswith(".csv") else os.path.join(HERE, "data", scenario + ".csv")
    mod = importlib.reload(importlib.import_module("pipeline"))
    log = {"scenario": scenario, "model": model, "mode": mode, "rounds": [], "llm_seconds": 0.0}
    first_report = None
    src = {n: inspect.getsource(f) for n, f in vars(mod).items() if inspect.isfunction(f) and getattr(f, "__module__", "") == "pipeline"}
    feedback = ""
    for rnd in range(MAX_ROUNDS + 1):
        try:
            report = mod.build_report(path)
        except Exception as e:
            fn = failing_function(e, mod)
            err = f"{type(e).__name__}: {e}"
            SHOW("crash", fn=fn, err=err, rnd=rnd)
            if rnd == MAX_ROUNDS or fn is None:
                log.update(status="failed", error=err[:300]); SHOW("done", log=log); break
            head = open(path, "rb").read()[:600].decode("utf-8", errors="replace") if os.path.exists(path) else "(file not found)"
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__)[-3:])
            prompt = (f"The function `{fn}` crashed.\n\nError:\n{err}\n\nTraceback (end):\n{tb}\n"
                      f"Function source:\n```python\n{src[fn]}```\n{used_globals(mod, fn) if CONTEXT == 'globals' else ''}\n"
                      f"First lines of the input file:\n```\n{head}\n```\n{feedback}\n{RULES[mode]}")
            SHOW("ask", model=model, fn=fn)
            reply, secs = ask(model, prompt)
            log["llm_seconds"] += secs
            rec = {"function": fn, "error": err[:200], "seconds": secs}
            if reply.strip().upper().startswith("CANNOT_FIX") or (extract_code(reply) is None and "CANNOT_FIX" in reply.upper()):
                rec["reply"] = reply.strip()[:300]; log["rounds"].append(rec)
                log.update(status="gave_up", error=err[:300]); SHOW("gave_up", reply=reply.strip(), secs=secs); break
            code = extract_code(reply)
            SHOW("fix", fn=fn, code=code, secs=secs)
            rec["fix"] = code
            log["rounds"].append(rec)
            if not code:
                log.update(status="no_code", error=err[:300]); break
            ns = dict(vars(mod))
            try:
                exec(code, ns)
                assert callable(ns.get(fn)), f"reply did not define {fn}"
                setattr(mod, fn, ns[fn]); src[fn] = code
                for k, v in ns.items():            # helpers / imports the fix needs
                    if not hasattr(mod, k):
                        setattr(mod, k, v)
            except Exception as ce:
                feedback = f"\nYour previous fix did not run: {type(ce).__name__}: {ce}\n"
            continue
        if first_report is None:
            first_report = report
        if rnd == 0:
            log.update(status="no_error", report=report); break
        if not os.path.exists(path):
            log.update(status="failed", error="file missing"); break
        fails = contract(report, raw_facts(path))
        log["rounds"][-1]["contract"] = fails or "passed"
        SHOW("checks", fails=fails, report=report)
        if not fails:
            log.update(status="healed", report=report); SHOW("done", log=log); break
        if rnd == MAX_ROUNDS:
            log.update(status="blocked_by_contract", report=report); SHOW("done", log=log); break
        # a fix that "works" but breaks the numbers: send the contract failures back for another try
        fn = log["rounds"][-1]["function"]
        SHOW("ask", model=model, fn=fn, retry=True)
        reply, secs = ask(model, f"Your fix for `{fn}` ran, but the report fails these checks:\n- " + "\n- ".join(fails) +
                          f"\n\nYour fix:\n```python\n{log['rounds'][-1]['fix']}```\n\nFirst lines of the input file:\n```\n"
                          f"{open(path, 'rb').read()[:600].decode('utf-8', errors='replace')}\n```\n{used_globals(mod, fn) if CONTEXT == 'globals' else ''}{RULES[mode]}")
        log["llm_seconds"] += secs
        code = extract_code(reply)
        SHOW("fix", fn=fn, code=code, secs=secs)
        log["rounds"].append({"function": fn, "error": "contract: " + "; ".join(fails)[:200], "seconds": secs, "fix": code})
        if code:
            ns = dict(vars(mod))
            try:
                exec(code, ns); setattr(mod, fn, ns[fn]); src[fn] = code
                for k, v in ns.items():
                    if not hasattr(mod, k):
                        setattr(mod, k, v)
            except Exception:
                pass
    log["first_report"] = first_report          # what an unguarded self-healer would have shipped
    log["llm_seconds"] = round(log["llm_seconds"], 1)
    return log


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "guarded"), default=str))
