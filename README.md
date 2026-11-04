# Counterfoil

- Reconciles broker trades against confirmations, one record at a time, as if live.
- Every record gets a status within 2 hours.
- Operations is told what is wrong, why, and what to do next.
- Logic, assumptions, production and extension: [`DESIGN.md`](DESIGN.md).
- Charts in `DESIGN.md` show on GitHub and in the dashboard; VS Code needs a Mermaid extension.

## Run

**No install:** results are already in `output/`. Open `recon_output.txt` and `dashboard.html`.

**1. Install [uv](https://docs.astral.sh/uv/), once**

- macOS, Linux (or `brew install uv`):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

- Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Recreate the environment, from this folder**

- Python 3.14 and the exact versions in `uv.lock`:

```bash
uv sync --locked --all-extras
```

**3. Run**

- Reconcile `data/*.csv` and write reports to `output/`:

```bash
uv run python -m recon run
```

- Run the tests. Database tests need `RECON_TEST_ADMIN_DSN`, otherwise they skip:

```bash
uv run python -m pytest -q
```

**Then**

- Open `output/dashboard.html` in a browser.
- No activation needed: `uv run` uses the project environment.
- PostgreSQL is optional.
- Production, without test tools: `uv run --locked --no-dev python -m recon run`.

| Output in `output/` | Read it for |
| --- | --- |
| `recon_output.txt` | Summary first, then every status in time order |
| `final_status.csv`, `status_events.jsonl` | Machine-readable results and full history |
| `dashboard.html` | Open exceptions and matches with slicers, broker timeliness, light and dark mode, these instructions, the design and the text report |
| `alerts.txt` | What Operations would receive |

## Code map

| Step | Module | Read first? |
| --- | --- | --- |
| Is this input valid? | `recon/loader.py`, `models.py`, `config.py` | |
| Do these two records agree? | `recon/matcher.py` | yes |
| What is the status, and when does it change? | `recon/engine.py` | yes |
| What do we tell Operations? | `recon/diagnostics.py` | yes |
| Who sees it, and how? | `recon/reports.py`, `dashboard.html.j2`, `alerts.py` | |
| How does it run and survive restarts? | `recon/runner.py`, `store.py`, `cli.py` | |

## Try it

- Steps 4 to 6 are written for macOS and Linux. On Windows, use WSL.

**1. Restart: crash on purpose, resume, get the same result**

```bash
uv run python -m recon run --fresh --crash-after 40
uv run python -m recon run
```

**2. Operations decisions: who, when, why, replayed on every rebuild**

```bash
uv run python -m recon accept --side internal --id 2028 --reason "Broker misspelt the trader" --actor yg
uv run python -m recon force-match --trade 2596 --confirmation 72141 --reason "Fee agreed by phone" --actor yg
uv run python -m recon note --side internal --id 6054 --text "Broker promised it by 15:00" --actor yg
```

**3. Undo the decisions from step 2 and restore the original results**

- macOS, Linux:

```bash
rm -rf output/state && uv run python -m recon run
```

- Windows:

```powershell
Remove-Item -Recurse -Force output\state; uv run python -m recon run
```

**4. Run on a real database instead of files (needs Docker and `psql`)**

- Install them once on macOS. This also starts Docker Desktop, which must be running:

```bash
brew install --cask docker-desktop
brew install libpq && brew link --force libpq
open -a Docker
```

- Or on Linux (Debian, Ubuntu). Log out and back in afterwards:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
sudo apt-get install -y postgresql-client
```

- Shortcut: runs all of these except the last command, and proves a second run adds nothing:

```bash
./postgres_demo.sh
```

- Or run them yourself. The last command keeps running until Ctrl+C, and its `--fresh` forgets decisions:

```bash
docker compose up -d --wait
export RECON_DSN="host=localhost port=5432 user=recon password=recon dbname=recon"
uv run python -m recon load-db
uv run python -m recon run --store postgres --source postgres
psql "$RECON_DSN" -f sql/ops_queries.sql
uv run python -m recon live --store postgres --fresh
```

**5. Alerts: nothing is sent unless configured**

- Send urgent cases and resolutions to Slack:

```bash
export RECON_SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
```

- Send the daily digest by email:

```bash
export RECON_SMTP_HOST="mail.example.com:25" RECON_ALERT_TO="ops@example.com"
```

- These take effect on the next run.

**6. Include the database tests**

- Tell the tests which PostgreSQL to use. The value must contain `dbname=postgres`:

```bash
export RECON_TEST_ADMIN_DSN="host=localhost user=recon password=recon dbname=postgres"
```

- Run the tests:

```bash
uv run python -m pytest -q
```
