"""JPMC evaluation harness: Memory Bank against simpler baselines, on the eval engine.

Modules:
  env.py       settings from the environment, plus the guards that keep writes off
               the live demo engine and the governed demo dataset
  fixtures.py  the demo kit's memory fixtures (sessions, expected facts, cases)
  loader.py    fixtures -> Vertex sessions -> verified write path -> lineage,
               and the rows for the BigQuery `facts` and `raw_notes` tables
  bq.py        the eval dataset: tables, baseline queries, vector search, rerank
  scoring.py   Recall@k / MRR / nDCG@k, the Gemini judge, and its JSONL log
  evaluate.py  the tiers, contradiction, volume, isolation, control, scenarios
  results.py   one JSON per run, and the scorecard view read by backend/eval_api.py

Run it with scripts/run_jpmc_eval.py.
"""
