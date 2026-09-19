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
policy resolver, output guard — 134 unit tests, zero model calls); the model only decides *what to say*,
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

Requires Python 3.11+ and Node 18+. **All `make` targets run from the repo root** (where the
`Makefile` is) — each one `cd`s into the right subdirectory itself.

```bash
# one-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"
cp .env.example .env          # then edit in your ANTHROPIC_API_KEY
cd frontend && npm install && cd ..
```

Then, in **two terminals**, both from the repo root:

```bash
make dev-backend      # FastAPI on :8000  (uvicorn --reload)
make dev-frontend     # Vite on :5173     (hot reload, proxies /api -> :8000)
```

Open **http://localhost:5173** — not 8000. During development the UI is served by Vite; the backend
on 8000 only answers `/api`.

Single-port alternative, no Vite: `make frontend-build` once, then `make dev-backend`, and open
**http://localhost:8000**. FastAPI serves the built files from `frontend/dist`. This is exactly how
the Docker image runs, so it is the right way to sanity-check a build before deploying — but you have
to re-run `make frontend-build` after every frontend change.

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

All from the repo root:

```bash
make test           # 281 tests, 0 model calls, ~5s
make independence   # the thesis test: hostile model, 0 API calls, ~10s
make eval           # 15 scenarios against the real API, ~$0.80, ~6 min
make simulate N=6   # improvising simulated callers, ~$0.15/conversation
make eval-one ID=margaret_chen_happy_path
```

`make independence` is the one to run first. It re-runs the whole suite against a model that
**actively tries to break the SOP** and asserts that safety metrics stay at zero while quality
collapses — the claim being that guarantees come from the harness, not from the model. It needs no API
key and costs nothing, which is why it is the check that can run on every commit. See EVAL.md §2.

The eval run writes `backend/evals/reports/latest.json` and is rendered as a readable page at
**/api/evals/report** (also linked from the Operations tab).

See [EVAL.md](EVAL.md) for what each layer checks and the current baseline results
(`backend/evals/reports/baseline.json`).

## Deploying to Fly.io

```bash
flyctl auth login
flyctl launch --no-deploy          # reuses fly.toml; rename the app if "sop-harness-demo" is taken
flyctl secrets set ANTHROPIC_API_KEY=sk-ant-...
flyctl deploy
```

### Redeploying

Nothing needs stopping first. `flyctl deploy` builds the new image and replaces the machine in place;
`flyctl status` will show it come back up. `fly.toml` scales to zero when idle, so a `stopped` machine
is the cost-saving working as intended — the first request wakes it (one cold start of a few seconds).

**Sessions do not survive a deploy.** There is no Fly volume attached, so `backend/runs/` lives on the
machine's ephemeral filesystem: it survives the scale-to-zero stop/start cycle (which is what
`app/session/persistence.py` was added for), but a deploy replaces the filesystem and the Operations
board starts empty. That is fine for a demo and deliberate — a volume pins the app to one machine in
one region, which is a real constraint to take on knowingly rather than by accident. To keep history
across deploys:

```bash
flyctl volumes create sop_data --size 1 --region sjc
```

```toml
# then add to fly.toml
[mounts]
  source = "sop_data"
  destination = "/app/backend/runs"
```

**Hosted at https://sop-harness-demo.fly.dev** (one machine in `sjc`, scale-to-zero — the first
request after an idle period takes a few seconds to wake).

## Project structure

```
backend/app/
  sop/        state machine, spec loader, policy resolver, domain data, disposition codes
  identity/   deterministic ≥3-factor matcher, alias/normalization, representative lookup
  llm/        Anthropic provider wrapper, PERCEIVE (extraction), ACT (generation), prompt assembly
  guards/     the output guard — disclosure / grounding / commitment / contract checks
  tools/      tool schemas + side-effect handlers (transfer, consent, follow-up, email)
  session/    the PERCEIVE→DECIDE→ACT→VERIFY orchestrator, session store
  api/        FastAPI app
  obs/        HTML transcript export, idle-policy metrics
backend/sops/       insurance_claims.yaml — the SOP spec (DESIGN.md §6)
                    bank_kyc.yaml — a second vertical, to keep §6's claim falsifiable
backend/fixtures/   the provided starter data
backend/tests/      234 pytest tests, no model calls
backend/evals/      scenario harness against the real API — scenarios, invariants, runner,
                    JSON + HTML reports, ASR-noise suite, model-routing cost experiment
frontend/           React chat + Inspector + Operations board (Vite)
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
