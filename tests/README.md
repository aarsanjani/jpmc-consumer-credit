# Tests

Run the test suite offline without configuring real Google Cloud parameters by going to the `jpmc-cc-main` root directory and typing:

```bash
PYTHONPATH=. ./.venv/bin/python3 -m pytest tests/ -q
```

A `conftest.py` is loaded that populates the environment with safe dummy variables on session start. 
The test suite ensures load-bearing application domain rules are preserved (math and metric edge cases, formatting logic, missing values behaving predictably, basic request shape constraints). 

The test suite deliberately **does NOT** cover actual HTTP connection failures to live APIs (GCP BigQuery, Dataplex, or Vertex AI Agent Engine), because forcing realistic connection drops or IAM assertion responses requires real configured credentials, and we prefer an offline testing baseline over artificially faking HTTP pass shapes at deep infrastructure levels.
