"""Build the weekly pipeline report from a CRM export.
usage: python run_report.py <export.csv>                     plain run: a bad export crashes it
       python run_report.py <export.csv> --heal [--model M]  on a crash, a local model tries to fix the code"""
import itertools, os, sys, threading, time

args = sys.argv[1:]
path = args[0]
MODEL = args[args.index("--model") + 1] if "--model" in args else os.environ.get("HEAL_MODEL", "qwen2.5-coder:7b")
CLOUD = MODEL.startswith("claude-")
WHERE = "Anthropic API, over the internet" if CLOUD else "Ollama, CPU only"
KIND = "cloud" if CLOUD else "local"
RED, GRN, YEL, CYN, DIM, BLD, RST = "\033[91m", "\033[92m", "\033[93m", "\033[96m", "\033[90m", "\033[1m", "\033[0m"


def money(v):
    return f"${v:,.0f}"


def show_report(r, title="PIPELINE REPORT"):
    print(f"\n{BLD}{title}{RST}")
    print(f"  Opportunities     {r['rows']:,}")
    print(f"  Total pipeline    {money(r['total_amount'])}")
    print(f"  Average deal      {money(r['avg_amount'])}")
    print(f"  Won / Lost / Open {r['won_count']} / {r['lost_count']} / {r['open_count']}")
    q = r["won_amount_by_quarter"]
    print("  Won by quarter    " + ("  ".join(f"{k} {money(v)}" for k, v in q.items()) if q else "(none)"))
    print("  Win rate by rep   " + "  ".join(f"{k.split()[0]} {'n/a' if v is None or v != v else f'{v:.0%}'}"
                                          for k, v in r["win_rate_by_rep"].items()))


if "--heal" not in args:
    import pipeline
    show_report(pipeline.build_report(path))
    sys.exit(0)

import healer


class Waiting:
    """Live 'thinking 12s' line while the local model works."""
    def __init__(self, label):
        self.label, self.stop = label, threading.Event()
        self.t = threading.Thread(target=self.spin, daemon=True); self.t.start()

    def spin(self):
        t0 = time.time()
        for ch in itertools.cycle("|/-\\"):
            if self.stop.wait(0.25):
                break
            print(f"\r  {YEL}{ch} {self.label} {time.time() - t0:4.0f}s{RST}", end="", flush=True)
        print("\r" + " " * 70 + "\r", end="", flush=True)

    def done(self):
        self.stop.set(); self.t.join()


wait = None


def show(event, **kw):
    global wait
    if event == "crash":
        tag = "Pipeline crashed" if kw["rnd"] == 0 else "Still crashing"
        print(f"\n{RED}x {tag} in {kw['fn'] or '?'}(){RST}  {DIM}{kw['err'][:110]}{RST}")
    elif event == "ask":
        what = "Sending the failed checks back to" if kw.get("retry") else "Asking"
        print(f"{YEL}> {what} the {KIND} model ({kw['model']}) for a fix to {kw['fn']}(){RST}")
        wait = Waiting("thinking")
    elif event in ("fix", "gave_up"):
        wait.done()
        if event == "gave_up":
            print(f"{GRN}  Model answered in {kw['secs']:.0f}s: {kw['reply'][:120]}{RST}")
            return
        print(f"{DIM}  Model answered in {kw['secs']:.0f}s. Swapping in its version of {kw['fn']}():{RST}")
        for line in (kw["code"] or "(no code in the reply)").rstrip().splitlines()[:14]:
            print(f"    {CYN}{line}{RST}")
        print(f"{DIM}  Re-running the report...{RST}")
    elif event == "checks":
        if not kw["fails"]:
            print(f"{GRN}  Checks against the raw file: all passed{RST}")
        else:
            print(f"{RED}  Report ran, but it fails checks against the raw file:{RST}")
            for f in kw["fails"]:
                print(f"    {RED}- {f}{RST}")
    elif event == "done":
        log = kw["log"]
        st = log["status"]
        if st == "healed":
            show_report(log["report"], "PIPELINE REPORT (healed)")
            print(f"\n{GRN}Fixed by a {KIND} model in {log['llm_seconds']:.0f}s of model time." + ("" if CLOUD else " Nothing left this machine.") + RST)
        elif st == "blocked_by_contract":
            show_report(log["report"], "REPORT THE FIX WOULD HAVE SENT")
            print(f"\n{RED}{BLD}NOT RELEASED.{RST}{RED} The fix ran, but the numbers don't match the file. A human needs to look.{RST}")
        else:
            print(f"\n{RED}{BLD}GAVE UP AFTER {len(log['rounds'])} TRIES.{RST}{RED} No report sent. A human needs to look.{RST}")


healer.SHOW = show
print(f"{DIM}Self-healing on. {KIND.title()} model: {MODEL} ({WHERE}){RST}")
log = healer.run(path, MODEL)
if log["status"] == "no_error":
    show_report(log["report"])
elif log["status"] == "gave_up":
    print(f"\n{GRN}{BLD}NOT A CODE PROBLEM.{RST}{GRN} The model refused to patch it. No report sent.{RST}")
