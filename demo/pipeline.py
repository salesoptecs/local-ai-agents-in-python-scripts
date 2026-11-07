"""A typical sales ops cleanup script: read the weekly CRM opportunity export, clean it, build the pipeline report.
Written for data/clean.csv. Every step is a small function so the healer can swap one out."""
import pandas as pd

STAGE_OUTCOME = {"Prospecting": "open", "Qualification": "open", "Proposal": "open", "Negotiation": "open",
                 "Closed Won": "won", "Closed Lost": "lost"}


def load_export(path):
    return pd.read_csv(path, encoding="utf-8", dtype=str, keep_default_na=False)


def parse_dates(df):
    df["Created Date"] = pd.to_datetime(df["Created Date"], format="%Y-%m-%d")
    df["Close Date"] = pd.to_datetime(df["Close Date"], format="%Y-%m-%d")
    return df


def parse_amounts(df):
    df["Amount"] = [float(a) if a != "" else None for a in df["Amount"]]
    df["Amount"] = df["Amount"].astype(float)
    return df


def map_stages(df):
    df["Outcome"] = [STAGE_OUTCOME[s] for s in df["Stage"]]
    return df


def win_rates(df):
    rates = {}
    for rep in sorted(df["Owner"].unique()):
        mine = df[df["Owner"] == rep]
        won = int((mine["Outcome"] == "won").sum())
        lost = int((mine["Outcome"] == "lost").sum())
        rates[rep] = round(won / (won + lost), 4)
    return rates


def summarize(df):
    won = df[df["Outcome"] == "won"]
    q = won["Close Date"].dt.year.astype(str) + "-Q" + won["Close Date"].dt.quarter.astype(str)
    by_q = {k: round(float(v), 2) for k, v in won.groupby(q)["Amount"].sum().sort_index().items()}
    return {"rows": int(len(df)), "total_amount": round(float(df["Amount"].sum(skipna=True)), 2),
            "avg_amount": round(float(df["Amount"].mean(skipna=True)), 2), "missing_amount_rows": int(df["Amount"].isna().sum()),
            "won_count": int((df["Outcome"] == "won").sum()), "lost_count": int((df["Outcome"] == "lost").sum()),
            "open_count": int((df["Outcome"] == "open").sum()), "won_amount_by_quarter": by_q, "win_rate_by_rep": win_rates(df)}


def build_report(path):
    df = load_export(path)
    df = parse_dates(df)
    df = parse_amounts(df)
    df = map_stages(df)
    return summarize(df)
