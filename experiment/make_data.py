"""Builds the test exports for the self-healing pipeline prototype.

data/clean.csv        the export the pipeline was written for (it runs fine)
data/<scenario>.csv   the same opportunities after a realistic change ("drift") that crashes the pipeline
answer_key.json       the correct report for every scenario, computed from the canonical data by an independent
                      implementation (report_from_canonical), never by pipeline.py
"""
import json, os, random
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
random.seed(42)

ACCOUNTS = ["Acme Corp", "Brightline Logistics", "Société Générale Services", "Müller GmbH", "Northwind Traders",
            "Globex", "Initech", "Halvorsen Freight", "Café Olé Holdings", "Pinecrest Health", "Summit Dental Group",
            "Blue Harbor Foods", "Kestrel Analytics", "Orion Fitness", "Redwood Supply Co"]
REPS = ["Gary Novak", "Priya Shah", "Derek Lowe", "Megan Ortiz", "Kevin Brandt"]
REGIONS = ["East", "West", "Central", "South"]
STAGES = ["Prospecting", "Qualification", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]
WEIGHTS = [10, 12, 12, 8, 30, 28]


def base(n=1200):
    rows = []
    for i in range(n):
        created = pd.Timestamp("2025-07-01") + pd.Timedelta(days=random.randint(0, 300))
        close = created + pd.Timedelta(days=random.randint(10, 120))
        rows.append({"Opportunity ID": f"OPP-{10000 + i}", "Account Name": random.choice(ACCOUNTS),
                     "Owner": random.choice(REPS), "Region": random.choice(REGIONS),
                     "Stage": random.choices(STAGES, WEIGHTS)[0],
                     "Amount": float(round(random.lognormvariate(10, 0.7), -2)),
                     "Created Date": created, "Close Date": close})
    return pd.DataFrame(rows)


def report_from_canonical(df):
    """Independent reference implementation of the report (the answer key)."""
    amt = df["Amount"]
    outcome = df["Stage"].map({"Closed Won": "won", "Closed Lost": "lost"}).fillna("open")
    won = df[outcome == "won"]
    q = won["Close Date"].dt.year.astype(str) + "-Q" + won["Close Date"].dt.quarter.astype(str)
    by_q = {k: round(float(v), 2) for k, v in won.groupby(q)["Amount"].sum().sort_index().items()}
    rates = {}
    for rep, g in df.groupby("Owner"):
        o = outcome[g.index]
        w, l = int((o == "won").sum()), int((o == "lost").sum())
        rates[rep] = round(w / (w + l), 4) if w + l else None
    return {"rows": int(len(df)), "total_amount": round(float(amt.sum(skipna=True)), 2),
            "avg_amount": round(float(amt.mean(skipna=True)), 2), "missing_amount_rows": int(amt.isna().sum()),
            "won_count": int((outcome == "won").sum()), "lost_count": int((outcome == "lost").sum()),
            "open_count": int((outcome == "open").sum()), "won_amount_by_quarter": by_q, "win_rate_by_rep": dict(sorted(rates.items()))}


def write(df, name, encoding="utf-8", date_fmt="%Y-%m-%d", amount_fmt=None, rename=None, stage_rename=None, na_token=""):
    out = df.copy()
    for c in ("Created Date", "Close Date"):
        out[c] = out[c].dt.strftime(date_fmt)
    if amount_fmt:
        out["Amount"] = [amount_fmt(a) if pd.notna(a) else na_token for a in out["Amount"]]
    else:
        out["Amount"] = [(f"{a:.2f}" if pd.notna(a) else na_token) for a in out["Amount"]]
    if stage_rename:
        out["Stage"] = out["Stage"].replace(stage_rename)
    if rename:
        out = out.rename(columns=rename)
    out.to_csv(os.path.join(DATA, name + ".csv"), index=False, encoding=encoding)


def main():
    os.makedirs(DATA, exist_ok=True)
    df = base()
    key = {}
    write(df, "clean"); key["clean"] = report_from_canonical(df)
    # 1. the export renamed a column
    write(df, "s1_renamed_column", rename={"Amount": "Opportunity Amount"}); key["s1_renamed_column"] = report_from_canonical(df)
    # 2. someone changed the export's date format to US style
    write(df, "s2_date_format", date_fmt="%m/%d/%Y"); key["s2_date_format"] = report_from_canonical(df)
    # 3. blank amounts now come out as the text N/A (rows must be KEPT, amount counted as missing)
    d3 = df.copy(); d3.loc[d3.sample(frac=0.08, random_state=3).index, "Amount"] = float("nan")
    write(d3, "s3_na_amounts", na_token="N/A"); key["s3_na_amounts"] = report_from_canonical(d3)
    # 4. amounts formatted as currency text
    write(df, "s4_currency_text", amount_fmt=lambda a: f"${a:,.2f}"); key["s4_currency_text"] = report_from_canonical(df)
    # 5. a new rep with only open deals (win rate has no closed deals to divide by)
    new = pd.DataFrame([{"Opportunity ID": f"OPP-2{i:04d}", "Account Name": random.choice(ACCOUNTS), "Owner": "Tasha Reed",
                         "Region": "West", "Stage": random.choice(STAGES[:4]), "Amount": 25000.0,
                         "Created Date": pd.Timestamp("2026-04-01"), "Close Date": pd.Timestamp("2026-06-30")} for i in range(6)])
    d5 = pd.concat([df, new], ignore_index=True)
    write(d5, "s5_new_rep"); key["s5_new_rep"] = report_from_canonical(d5)
    # 6. admin renamed the stage picklist value "Closed Won" -> "Closed - Won"
    write(df, "s6_stage_renamed", stage_rename={"Closed Won": "Closed - Won"}); key["s6_stage_renamed"] = report_from_canonical(df)
    # 7. file re-saved from Excel in Windows-1252 (accented account names)
    write(df, "s7_encoding", encoding="cp1252"); key["s7_encoding"] = report_from_canonical(df)
    # 8. NOT a code problem: the export never arrived (correct behaviour = give up, don't "fix")
    key["s8_missing_file"] = None
    json.dump(key, open(os.path.join(HERE, "answer_key.json"), "w"), indent=1, ensure_ascii=False)
    print("wrote", len(key), "scenarios")


if __name__ == "__main__":
    main()
