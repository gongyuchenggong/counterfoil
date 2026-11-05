# Counterfoil

- Reconciles broker trades against confirmations, one record at a time, in arrival order.
- Every record gets a status within 2 hours.
- Operations is told what is wrong, why, and what to do next.

## Run


**1. Install [uv](https://docs.astral.sh/uv/), once**

- macOS, Linux (or `brew install uv`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

- Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Install what it needs, from this folder**

- Python 3.14 and the exact package versions in `uv.lock`:

```bash
uv sync --locked --all-extras
```

**3. Run**

- Reconcile `data/*.csv` and write reports to `output/`:

```bash
uv run python -m recon run
```

- Run the tests:

```bash
uv run python -m pytest -q
```


## Code map

| Step | File | Read first? |
| --- | --- | --- |
| Is this input valid? | `recon/loader.py`, `models.py`, `config.py` | |
| Do these two records agree? | `recon/matcher.py` | yes |
| What is the status, and when does it change? | `recon/engine.py` | yes |
| What do we tell Operations? | `recon/diagnostics.py` | yes |
| Who sees it, and how? | `recon/reports.py`, `dashboard.html.j2`, `alerts.py` | |
| How does it run and survive restarts? | `recon/runner.py`, `store.py`, `cli.py` | |

