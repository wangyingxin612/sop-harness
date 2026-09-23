# SOP Harness

Make an LLM agent follow a business procedure — and be able to *prove* it did.

The worked example is insurance claims support: a caller must be identity-verified on three separate
factors before any claim detail is disclosed, the conversation moves through four phases in order, and
an email summary at the end requires explicit consent. The agent still has to sound like a person
while all of that is enforced.

---

## The claim this tests

> **An excellent SOP harness is one whose guarantees do not depend on the model being good.**

That is falsifiable, so it's tested. The same scenario suite runs against a deliberately **hostile
model** that ignores the system prompt and actively tries to leak case data, promise payouts, invent
dollar amounts and read out an SSN:

| | Normal model | Hostile model |
|---|---|---|
| Scenarios passed | 16/16 | **1/16** |
| Guard fallback rate | 0.00 | **0.98** |
| **Safety invariant violations** | **0** | **0** |
| **Case data disclosed pre-verification** | **0** | **0** |

54 replies reached callers while the model was attacking the procedure. None contained the fabricated
amount, the guarantee, the SSN, or the injection compliance. Quality collapsed; **the floor did not
move.**

That's the whole idea: **the harness turns model weakness from a safety problem into a cost problem.**
A safety problem blocks a deployment. A cost problem is a dial you set — which is what makes running a
cheaper model a decision you can defend instead of a leap of faith.

```bash
make independence   # ~10 seconds, no API key, $0 — run it yourself
```

---

## Quickstart

```bash
docker build -t sop-harness .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... sop-harness
```

Open **http://localhost:8000** and paste this:

> I'm the policyholder. My name is Margaret Chen, policy POL-9921. I'm calling about my denied
> healthcare claim from January. DOB is 1985-03-15, SSN last four is 4472.

Watch the Inspector on the right. Identity verifies in one turn, and the claim hint mentioned *during
verification* is remembered and reused the moment the gate opens — without asking again.

Without Docker: `pip install -e "backend[dev]"`, then `make dev-backend` and `make dev-frontend` from
the repo root (backend on `:8000`, UI on `:5173`).

---

## How it works

The hard part of this problem isn't "LLM plus some code" — every solution is LLM plus some code. It's
**where the boundary goes.** This puts the **control plane in code and the data plane in the model**:
code decides what phase you're in, what data exists, and which tools are reachable; the model only
decides what to say.

The consequence that matters: **phases are permission scopes, not conversation steps.** `VERIFY_ID`
isn't "the part where we ask for a date of birth" — it's the scope in which no claim data exists in
the model's context at all. Prompt injection can't extract what was never loaded. That's a structural
guarantee, not a hopeful instruction.

```
caller ─→ PERCEIVE ─→ DECIDE ─→ ACT ─→ VERIFY ─→ reply
          extract     build     LLM     5-rule
          signals     the plan  writes  output guard
```

`DECIDE` is two pure functions and never calls a model. The plan is computed **before** generation, not
repaired afterwards.

The procedure itself is a YAML file — phases, freedom levels, tool permissions, disclosure scopes,
escalation thresholds, refusal copy. A second spec (`bank_kyc.yaml`) exists to keep the "this engine
isn't insurance-specific" claim honest rather than merely asserted.

**[DESIGN.md](DESIGN.md)** has the full rationale: alternatives considered, why this one, and every
non-obvious decision. **[EVAL.md](EVAL.md)** covers how it's measured.

---

## Verify the claims

Every number above comes from a command, not from me:

```bash
make test           # 302 tests, zero model calls, ~5s
make independence   # hostile-model run + 5 static architecture checks, $0, ~10s
make eval           # 16 scenarios against the live model, ~$0.71, ~6 min
make simulate N=6   # improvising simulated callers
```

Results land in `backend/evals/reports/`, and `/api/evals/report` renders them as a page.

---

## What went wrong along the way

The two most useful findings were both cases of *passing for the wrong reason*:

**A directive that never fired, for the entire build.** `SEND_NOW` tells the agent to send the summary
email. An off-by-one in a turn-index comparison made its condition permanently false — and the email
went out every time anyway, because the model saw the pending action and volunteered the tool call.
Every scenario green throughout. The model was quietly doing the control plane's job.

That produced the attribution layer: for each requirement, the report names the **mechanism** that
should enforce it and checks both. A right outcome with the mechanism absent is an *unguarded pass* —
a green result that's lying.

**A banana bread recipe reached callers 17 times.** The hostile run found that scope was enforced only
on *input*: an off-topic caller message gets a templated refusal, but an off-topic *reply* to an
on-topic question had nothing checking it. The fix is a fifth guard rule — deliberately not a topic
blocklist, since that list is unbounded and out of date the day it ships.

---

## Known limitations

- **Small N.** 16 scenarios finds real bugs; it doesn't produce a confident containment estimate.
- **The adversary is a fixed rotation**, not an adaptive red-teamer. The floor holds against a *known*
  set of attacks.
- **Output-scope enforcement is a heuristic floor.** A fluent, on-register, subtly out-of-scope answer
  would pass it. Closing that needs a classifier call per turn.
- **The data plane isn't pluggable yet.** The second SOP proves the *spec* layer is generic; the
  runtime still calls insurance-shaped functions to assemble facts.
- **Four directives are advisory** — nothing enforces them. They shape tone and cost nothing if
  ignored, but they're the share of the procedure that's still hope rather than mechanism, and the
  count is published for exactly that reason.

DESIGN.md §13 has what I'd build next, and what I'd deliberately leave alone.

---

## Layout

```
backend/app/sop/        state machine, policy resolver, SOP spec loader, disposition
backend/app/guards/     the output guard — five rule families
backend/app/identity/   deterministic ≥3-factor matcher, typo-tolerant name matching
backend/app/llm/        provider, extraction (PERCEIVE), generation (ACT)
backend/sops/           the procedures themselves, as YAML
backend/evals/          scenarios, invariants, attribution, coverage, hostile model, simulator
frontend/src/           React chat + Inspector + operations board
```
