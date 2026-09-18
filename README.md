# SOP Harness — Insurance Claims Support Agent

An SOP harness for an insurance claims support agent: a fixed
business workflow (`VERIFY_ID → RESOLVE_INTENT → PROCESS_CASE → POST_PROCESS`) that still converses
naturally, strict where the SOP demands it and flexible where reasoning helps.

**Read [DESIGN.md](DESIGN.md) for the full design rationale** — the problem framing, alternatives
considered, the architecture, and every non-obvious decision with its reasoning. This file is the
practical "how to run it" companion. **[EVAL.md](EVAL.md)** covers testing methodology and results.
**[PROGRESS.md](PROGRESS.md)** is a running build log, including bugs found via live testing and how
they were fixed — useful if you want to see the actual engineering process, not just the end state.

## What this is, in one paragraph

The engine (`backend/app/sop/`) is a generic SOP runtime: phases, gates, tool permissions, and disclosure
scopes are read from a YAML spec (`backend/sops/insurance_claims.yaml`), not hard-coded. Authority over
*what may happen* lives entirely in deterministic, LLM-free Python (state machine, identity matcher,
policy resolver, output guard — 118 unit tests, zero model calls); the model only decides *what to say*,
inside whatever envelope that code hands it each turn. A FastAPI backend and a React chat+Inspector
frontend sit on top, so you can watch the SOP enforce itself turn by turn.

## Quickstart (Docker)

```bash
docker build -t sop-harness .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... sop-harness
```

Then open **http://localhost:8000**. Or with docker-compose:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up --build
```

**Two ways to provide the API token (R10)**: the `ANTHROPIC_API_KEY` environment variable above, or —
with no server-side key configured at all — click **"Use my own API key"** in the running UI and enter
one per-session. Either path works standalone.

## Quickstart (no Docker)

Requires Python 3.11+ and Node 18+.

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example ../.env   # then edit in your ANTHROPIC_API_KEY

cd ../frontend
npm install && npm run build   # builds into frontend/dist, served by the backend

cd ../backend
uvicorn app.api.main:app --reload
```

Open **http://localhost:8000**. For frontend hot-reload during development, run `npm run dev` in
`frontend/` instead (proxies `/api` to `localhost:8000` — see `frontend/vite.config.js`) and open
**http://localhost:5173**.

## Trying it

The UI offers three one-click example openers, or type your own. To reproduce the brief's worked example
exactly:

> I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied healthcare
> claim from January. DOB is 1985-03-15, SSN last four is 4472.

Watch the Inspector panel (right side): identity verifies in one turn (3/3 factors, with the verbatim
quote each came from), the case hint is remembered and used the moment you're verified, and every
subsequent turn shows the active gate, the directives in force, and the output guard's verdict.

To see the third-party + async-consent path, switch **Consent scenario** to `timeout` before starting a
session, then open with "I'm David Chen, calling about my mom Margaret Chen's claim..." — disclosure
stays reduced (status/documents/deadline only) since consent never arrives, which is the intended
graceful-degradation behavior (DESIGN.md §7.10).

## Running the tests

```bash
cd backend && source .venv/bin/activate   # or ../.venv if using the repo-root venv layout below
pytest -q                                  # 118 tests, 0 model calls, ~1.5s
```

```bash
python -m evals.runner                    # 12 scenarios against the real API, ~$0.44, ~4 min
python -m evals.runner --id margaret_chen_happy_path   # a single scenario
python -m evals.runner --tag adversarial               # by tag
```

See [EVAL.md](EVAL.md) for what each layer checks and the current baseline results
(`backend/evals/reports/baseline.json`).

## Deploying to Fly.io

```bash
flyctl auth login
flyctl launch --no-deploy          # reuses fly.toml; rename the app if "sop-harness-demo" is taken
flyctl secrets set ANTHROPIC_API_KEY=sk-ant-...
flyctl deploy
```

`fly.toml` scales to zero machines when idle, so an unused deployment costs nothing between demos.

> **Note on this submission's hosted URL**: this environment has no Fly.io account credentials available
> to create/authenticate one on your behalf — that's a decision only you can make (which account, which
> region, what the app name should be). The Dockerfile, `fly.toml`, and the three commands above are
> ready; deploying is the one remaining step. The Docker image itself has been built and run end-to-end
> in this environment as verification (see PROGRESS.md).

## Project structure

```
backend/app/
  sop/        state machine, identity matcher's caller, spec loader, policy resolver, domain data
  identity/   deterministic ≥3-factor matcher, alias/normalization, representative lookup
  llm/        Anthropic provider wrapper, PERCEIVE (extraction), ACT (generation), prompt assembly
  guards/     the output guard — disclosure / grounding / commitment / contract checks
  tools/      tool schemas + side-effect handlers (transfer, consent, follow-up, email)
  session/    the PERCEIVE→DECIDE→ACT→VERIFY orchestrator, session store
  api/        FastAPI app
  obs/        HTML transcript export
backend/sops/       insurance_claims.yaml — the SOP spec (DESIGN.md §6)
backend/fixtures/   the provided starter data
backend/tests/      118 pytest tests, no model calls
backend/evals/      scenario harness against the real API — scenarios, invariants, runner, report
frontend/           React chat + Inspector UI (Vite)
DESIGN.md   EVAL.md   PROGRESS.md
```

## Known limitations

Stated plainly in [DESIGN.md §11](DESIGN.md#11-known-limitations); most relevant here:

- Streaming to the browser is the fully-guarded final reply delivered progressively, not raw incremental
  token streaming with mid-generation abort — see DESIGN.md §7.11 for the design and why that gap is
  documented rather than hidden.
- The eval suite is scripted, not adversarially LLM-simulated (EVAL.md §5) — the natural next step,
  not built in this timeframe.
- `bank_kyc.yaml` (selectable in the UI) proves the *control layer* — phases, gates, freedom levels, tool
  permissions, escalation, directives — is vertical-agnostic, checked by `tests/test_bank_kyc_spec.py`.
  It does not yet prove the full runtime is pluggable: case resolution past identity verification still
  reads insurance fixture data, because no bank domain-adapter exists (DESIGN.md §9.2's honestly-scoped
  note). The UI says this plainly when the SOP is selected, rather than letting it look like more than
  it is.
