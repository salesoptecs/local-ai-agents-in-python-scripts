# Local AI Agents in Your Python Scripts

Code from the SalesOptecs video **Embedding Local AI Agents in Your Python Scripts**.

A Python report script crashes every time the CRM export changes. Here, the `except` block asks a small AI model running on the
same machine for a fix: no internet, no API keys, a regular desktop CPU and no GPU. Before any report goes out, the fixed run is
checked against facts the raw file can prove (row count, amounts read, won + lost + open = total, won deals present when the
file has them). If the checks fail, the failures go back to the model. If it still can't pass them, the report is not released
and a human gets the problem.

All data is synthetic (1,200 made up opportunities).

## What's here

| Folder | What it is |
|---|---|
| `demo/` | The version filmed in the video. `pipeline.py` is a normal weekly pipeline report, `healer.py` catches the crash, asks the model for a new version of the failing function, swaps it in and runs the checks. `run_report.py` is the on screen runner. `exports/` has the clean export and the changed ones. |
| `experiment/` | The test kit behind the scoreboard. `make_data.py` builds the exports and an answer key from an independent implementation, `run_experiment.py` runs every scenario against a model and scores it, `results/` has the raw runs. |

## Run it

1. Install [Ollama](https://ollama.com) and pull the model: `ollama pull qwen2.5-coder:7b`
2. `pip install pandas requests`
3. From `demo/`:

```
python run_report.py exports/clean.csv                    # runs fine
python run_report.py exports/s4_currency_text.csv         # crashes
python run_report.py exports/s4_currency_text.csv --heal  # the local model tries to fix it
```

Use `--model` to try another Ollama model. `OLLAMA_URL` points the healer at a different Ollama host.

To rerun the test battery from `experiment/`:

```
python make_data.py
python run_experiment.py qwen2.5-coder:7b --mode guarded
```

## The scenarios

| File | What changed in the export |
|---|---|
| s1_renamed_column | a column was renamed |
| s2_date_format | US date format |
| s3_na_amounts | blank amounts now say N/A |
| s4_currency_text | amounts exported as text with $ and commas |
| s5_new_rep | a new rep with no closed deals |
| s6_stage_renamed | the "Closed Won" stage was renamed "Closed - Won" |
| s7_encoding | the file was re saved with a different encoding |
| missing file | the export isn't there at all (the right answer is to give up) |

## A word of caution

This is a prototype for a video, not a product. The model rewrites code while your script runs. The checks are what make it
safe, and checks only catch what you thought to check. Use it on reports you can verify, never on anything that writes to a
system of record.

From [SalesOptecs](https://salesoptecs.com).
