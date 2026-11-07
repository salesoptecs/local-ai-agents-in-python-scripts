"""Self-healing wrapper: run pipeline.build_report; when it crashes, ask a LOCAL model (Ollama, CPU only, nothing leaves
the machine) for a replacement of the function that failed, swap it in, re-run, and only release the report if it
passes a validation contract built from the raw file (no answer key).

usage: python healer.py <scenario> <model> [guarded|naive]   -> prints one JSON line"""
import ast, csv, importlib, inspect, io, json, os, re, sys, time, traceback
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


PROFILE = os.environ.get("HEAL_PROFILE", "") == "1"   # also show a profile of the whole file (columns, distinct text values)
N_CAND = int(os.environ.get("HEAL_N", "1"))          # best-of-N: ask for N fixes, keep the first one the checks accept
NUM_CTX = int(os.environ.get("HEAL_NUMCTX", "4096"))


def profile(path, mod, fn):
    """Facts about the WHOLE input file (not just its first lines): the columns, and for text columns with few distinct values,
    every value and how often it appears. Next to the constants the failing function uses, so a mismatch is visible."""
    if not os.path.exists(path):
        return "\nFile profile: the input file does not exist.\n"
    text = open(path, "rb").read().decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    lines = [f"rows: {len(rows)}", f"columns: {list(rows[0].keys()) if rows else []}"]
    for c in (rows[0].keys() if rows else []):
        vals = {}
        for r in rows:
            vals[r[c]] = vals.get(r[c], 0) + 1
        if len(vals) <= 12:
            lines.append(f"{c!r} values: " + ", ".join(f"{v!r} x{n}" for v, n in sorted(vals.items(), key=lambda x: -x[1])))
        else:
            sample = list(vals)[:3]
            lines.append(f"{c!r}: {len(vals)} distinct values, e.g. {sample}")
    return "\nProfile of the whole input file:\n```\n" + "\n".join(lines) + "\n```\n" + used_globals(mod, fn)


GUARD = os.environ.get("HEAL_GUARD", "") == "1"     # reject fixes that rewrite a module setting (e.g. STAGE_OUTCOME); re-ask
TRIAGE = os.environ.get("HEAL_TRIAGE", "")         # "1": ask the model "code or outside problem?"; "code": decide from the exception type
THINK = os.environ.get("HEAL_THINK", "")           # qwen3-style models: "retry" = answer fast, think only on a second try at the same function
THINK_CAP = int(os.environ.get("HEAL_THINK_CAP", "3000"))   # max tokens of thinking + answer when thinking
CUR_THINK = False
OUTSIDE = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError, ConnectionError, TimeoutError)


def rewritten_settings(code, mod):
    """Module settings (UPPER_CASE globals) the fix assigns a whole new value to, anywhere in its code. Adding one key
    (STAGE_OUTCOME["x"] = ...) is allowed; replacing the setting is not."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    hit = set()
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
        for t in targets:
            for n in ast.walk(t) if isinstance(t, (ast.Tuple, ast.List)) else [t]:
                if isinstance(n, ast.Name) and n.id.isupper() and hasattr(mod, n.id) and not keeps_old(node, getattr(mod, n.id)):
                    hit.add(n.id)
    return sorted(hit)


def keeps_old(node, old):
    """A redefinition is fine if it is a plain literal that keeps every existing entry with the same value (it only adds)."""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    try:
        new = ast.literal_eval(node.value)
    except Exception:
        return False
    if isinstance(old, dict) and isinstance(new, dict):
        return all(k in new and new[k] == v for k, v in old.items())
    if isinstance(old, (list, tuple, set)) and isinstance(new, (list, tuple, set)):
        return all(v in new for v in old)
    return new == old


def settings_patch(code, ns, mod):
    """HEAL_GUARD: a reply may be just a corrected setting (STAGE_OUTCOME = {...old entries..., new: value}) with no function.
    Top-level reassignments that keep every old entry (the guard already rejected any other kind) are applied to the module."""
    if not GUARD:
        return []
    names = []
    for node in ast.parse(code).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
            if n.isupper() and hasattr(mod, n) and keeps_old(node, getattr(mod, n)):
                names.append(n)
    for n in names:
        setattr(mod, n, ns[n])
        for f in vars(mod).values():          # earlier fixes run in their own copy of the globals: update those too
            g = getattr(f, "__globals__", None)
            if g is not None and g is not vars(mod) and n in g:
                g[n] = ns[n]
    return names


def guarded_ask(model, prompt, mod, log, fn):
    """ask(), but when HEAL_GUARD is on and the fix rewrites a module setting, say so and ask again (up to 2 more times)."""
    reply, secs = ask(model, prompt)
    for _ in range(2):
        code = extract_code(reply)
        bad = rewritten_settings(code, mod) if (GUARD and code) else []
        if not bad:
            break
        log["rounds"].append({"function": fn, "error": "rejected: rewrites " + ", ".join(bad), "seconds": secs, "fix": code})
        cur = "\n".join(f"{n} = {getattr(mod, n)!r}" for n in bad)
        reply, s2 = ask(model, prompt + f"\n\nYour last fix replaced the existing setting(s) {', '.join(bad)}. Do not replace or "
                        f"redefine them; other code depends on their exact values. Current values:\n```python\n{cur}\n```\n"
                        f"If something is missing, add only the missing entry next to the function, for example "
                        f"NAME[key] = value, using the same style of values that are already there.")
        secs += s2
    return reply, secs


def triage(model, err, tb, fn):
    """HEAL_TRIAGE: a separate short question before any fix. Returns the model's reason if it says OUTSIDE, else None."""
    q = (f"A Python data pipeline crashed in `{fn}`.\n\nError:\n{err}\n\nTraceback (end):\n{tb}\n"
         "Question: is this caused by the CODE (a bug or a data format the code doesn't handle), or by something OUTSIDE "
         "the code that changing the code cannot correct (a missing input file, network, permissions, login)?\n"
         "Answer with one word, CODE or OUTSIDE, then one short sentence why.")
    reply, secs = ask(model, q)
    word = re.search(r"\b(CODE|OUTSIDE)\b", reply.upper())
    return (reply.strip()[:300] if word and word.group(1) == "OUTSIDE" else None), secs


def ask(model, prompt, temp=None, seed=None):
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
    body = {"model": model, "system": SYSTEM, "prompt": prompt, "stream": False,
            "options": {"temperature": temp if temp is not None else TEMP, "seed": seed if seed is not None else SEED, "num_ctx": NUM_CTX}}
    if THINK:
        body["think"] = bool(CUR_THINK)
        if CUR_THINK:
            body["options"]["num_predict"] = THINK_CAP
    r = requests.post(OLLAMA, json=body, timeout=1800)
    r.raise_for_status()
    j = r.json()
    TOKENS.append(j.get("eval_count", 0))
    if THINK and CUR_THINK and not j.get("response", "").strip():
        # thought until the cap without answering: take a fast answer instead
        THINKS.append("capped")
        body["think"] = False; body["options"].pop("num_predict", None)
        r = requests.post(OLLAMA, json=body, timeout=1800); r.raise_for_status(); j = r.json()
        TOKENS.append(j.get("eval_count", 0))
    elif THINK and CUR_THINK:
        THINKS.append("thought")
    # reasoning models (qwen3, deepseek-r1) think first: drop the thinking so only the final answer is parsed
    return re.sub(r"<think>.*?</think>", "", j["response"], flags=re.S).strip(), round(time.time() - t, 1)


TOKENS = []
THINKS = []


def extract_code(reply):
    m = re.findall(r"```(?:python)?\s*\n(.*?)```", reply, re.S)
    return m[-1] if m else None


def best_of(mod, fn, prompt, path, src, log, err):
    """HEAL_N > 1: ask for N fixes (temperature 0.7, different seeds) and try each one for real. Keep the first that passes the
    checks; otherwise the best partial one (crash moved on > ran but failed checks); otherwise report that nothing worked."""
    old_f = getattr(mod, fn)
    gave, best = 0, None
    for k in range(N_CAND):
        reply, secs = ask(log["model"], prompt, temp=max(TEMP, 0.7), seed=SEED * 100 + k)
        log["llm_seconds"] += secs
        code = extract_code(reply)
        rec = {"function": fn, "error": err[:200], "seconds": secs, "cand": k, "fix": code}
        log["rounds"].append(rec)
        if reply.strip().upper().startswith("CANNOT_FIX") or (code is None and "CANNOT_FIX" in reply.upper()):
            rec["reply"] = reply.strip()[:300]; gave += 1; continue
        if not code:
            rec["verdict"] = "no code"; continue
        ns = dict(vars(mod))
        try:
            exec(code, ns)
            assert callable(ns.get(fn)), f"reply did not define {fn}"
        except Exception as ce:
            rec["verdict"] = f"did not run: {type(ce).__name__}: {ce}"[:200]; continue
        added = [n for n in ns if not hasattr(mod, n)]
        setattr(mod, fn, ns[fn])
        for n in added:
            setattr(mod, n, ns[n])
        try:
            rep = mod.build_report(path)
            fails = contract(rep, raw_facts(path)) if os.path.exists(path) else ["file missing"]
            rec["verdict"] = fails or "passed"
            score = 3 if not fails else 1
        except Exception as e2:
            same = f"{type(e2).__name__}: {e2}" == err or (type(e2).__name__ == err.split(":")[0] and failing_function(e2, mod) in (fn, None))
            rec["verdict"] = "same crash" if same else f"new crash: {type(e2).__name__}: {e2}"[:200]
            score = 0 if same else 2
        setattr(mod, fn, old_f)
        for n in added:
            delattr(mod, n)
        if best is None or score > best[0]:
            best = (score, code)
        if score == 3:
            break
    if best and best[0] > 0:
        ns = dict(vars(mod)); exec(best[1], ns); setattr(mod, fn, ns[fn]); src[fn] = best[1]
        for n, v in ns.items():
            if not hasattr(mod, n):
                setattr(mod, n, v)
        log["rounds"].append({"function": fn, "error": err[:200], "seconds": 0, "fix": best[1], "applied": True})
        return "applied"
    return "gave_up" if gave else "no_fix"


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
                      f"First lines of the input file:\n```\n{head}\n```\n{profile(path, mod, fn) if PROFILE else ''}{feedback}\n{RULES[mode]}")
            global CUR_THINK
            CUR_THINK = THINK == "retry" and any(r.get("function") == fn and r.get("fix") for r in log["rounds"])
            if TRIAGE == "code":
                if isinstance(e, OUTSIDE):
                    why = f"{type(e).__name__} is outside the code; escalate to a person"
                    log["rounds"].append({"function": fn, "error": err[:200], "seconds": 0, "triage": why})
                    log.update(status="gave_up", error=err[:300]); SHOW("gave_up", reply=why, secs=0); break
            elif TRIAGE:
                why, tsecs = triage(model, err, tb, fn)
                log["llm_seconds"] += tsecs
                log["rounds"].append({"function": fn, "error": err[:200], "seconds": tsecs, "triage": why or "CODE"})
                if why:
                    log.update(status="gave_up", error=err[:300]); SHOW("gave_up", reply=why, secs=tsecs); break
            SHOW("ask", model=model, fn=fn)
            if N_CAND > 1:
                res = best_of(mod, fn, prompt, path, src, log, err)
                if res == "gave_up":
                    log.update(status="gave_up", error=err[:300]); break
                feedback = "" if res == "applied" else "\nNone of your previous fixes worked.\n"
                continue
            reply, secs = guarded_ask(model, prompt, mod, log, fn)
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
                patched = settings_patch(code, ns, mod)
                assert callable(ns.get(fn)) or patched, f"reply did not define {fn}"
                if callable(ns.get(fn)):
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
        CUR_THINK = THINK == "retry"
        SHOW("ask", model=model, fn=fn, retry=True)
        rprompt = (f"Your fix for `{fn}` ran, but the report fails these checks:\n- " + "\n- ".join(fails) +
                   f"\n\nYour fix:\n```python\n{log['rounds'][-1]['fix']}```\n\nFirst lines of the input file:\n```\n"
                   f"{open(path, 'rb').read()[:600].decode('utf-8', errors='replace')}\n```\n{used_globals(mod, fn) if CONTEXT == 'globals' else ''}"
                   f"{profile(path, mod, fn) if PROFILE else ''}{RULES[mode]}")
        if N_CAND > 1:
            best_of(mod, fn, rprompt, path, src, log, "contract: " + "; ".join(fails))
            continue
        reply, secs = guarded_ask(model, rprompt, mod, log, fn)
        log["llm_seconds"] += secs
        code = extract_code(reply)
        SHOW("fix", fn=fn, code=code, secs=secs)
        log["rounds"].append({"function": fn, "error": "contract: " + "; ".join(fails)[:200], "seconds": secs, "fix": code})
        if code:
            ns = dict(vars(mod))
            try:
                exec(code, ns)
                patched = settings_patch(code, ns, mod)
                if callable(ns.get(fn)):
                    setattr(mod, fn, ns[fn]); src[fn] = code
                elif not patched:
                    raise AssertionError(f"reply did not define {fn}")
                for k, v in ns.items():
                    if not hasattr(mod, k):
                        setattr(mod, k, v)
            except Exception:
                pass
    log["first_report"] = first_report          # what an unguarded self-healer would have shipped
    log["llm_seconds"] = round(log["llm_seconds"], 1)
    log["tokens_out"] = sum(TOKENS)
    log["thinks"] = THINKS
    return log


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "guarded"), default=str))
