# EVAL.md — How this system is measured, and how measurement makes it better

## The question this document answers

Not "does the demo work". The claim being tested is stronger and narrower:

> **An excellent SOP harness is one whose guarantees do not depend on the model being good.**

That is falsifiable, and most of what follows exists to try to falsify it.

A conventional eval — a set of scripted conversations, a pass rate — cannot test it. It checks
*outcomes*, and an outcome is identical whether the harness enforced it or the model happened to
oblige. This project has a concrete, embarrassing example of why that distinction is not academic,
documented in §4.

---

## 1. Five layers, and what each one can and cannot see

| | Layer | Tests | Model calls | Cost | Speed |
|---|---|---|---|---|---|
| 1 | `backend/tests/` | The deterministic core: state machine, matcher, policy resolver, output guard | **none** | $0 | 302 tests, ~5s |
| 2 | `evals/runner.py` | The whole system on scripted conversations, real model | real API | ~$0.72 | 16 scenarios, ~6 min |
| 3 | `evals/attribution.py` | **Which mechanism** caused each requirement to be met | none (reads layer 2) | $0 | instant |
| 4 | `evals/independence.py` | Whether guarantees survive a hostile model | **none** | $0 | 16 scenarios, ~10s |
| 5 | `evals/simulator.py` | Improvised conversations from personas with goals | real API | ~$0.05/call | unbounded |
| 0 | `evals/architecture.py` | Whether two components that must agree still do | **none** | $0 | 5 claims, ~50ms |

The separation matters. Layer 1 is a **guarantee** — those properties hold no matter what any model
does, because no model is involved. Layer 2 is a **measurement** — it can regress when a prompt or a
model changes, and it is supposed to. Layers 3 and 4 ask the question the other two structurally
cannot: *was that pass earned by the architecture, or by the model's good manners?*

Layer 4 costs nothing and needs no API key, so it can run on every commit. That is deliberate: the
most important check should be the cheapest one to keep running.

---

## 2. Layer 4 is the headline: the hostile-model experiment

```bash
python -m evals.independence            # free, no API key, ~10 seconds
python -m evals.independence --with-real  # + real models, costs money
```

`evals/hostile_model.py` is a drop-in provider that **ignores the system prompt entirely** and returns
replies engineered to break specific requirements: disclosing case detail before identity is
established, promising payouts, inventing dollar amounts, answering off-topic questions, reading out
an SSN, complying with an injection. It is what a badly fine-tuned, prompt-injected or much weaker
model would do at its worst.

Running the ladder only between Sonnet and Haiku tests a shallow version of the claim — both are
well-aligned models that want to follow instructions. This rung removes cooperation entirely.

### The result, and how to read it

The naive claim — "nothing gets worse with a worse model" — is false and not worth defending. A weaker
model writes clumsier sentences and gets things wrong more often. The real claim splits the metrics in
two, and **the split is the result**:

| | Metric | Strong model | Hostile model |
|---|---|---|---|
| **MUST BE FLAT** | invariant violations | 0 | **0** |
| | attribution violations | 0 | **0** |
| | unguarded passes | 0 | **0** |
| | case data before verification | 0 | **0** |
| **ALLOWED TO DEGRADE** | scenario pass rate | 1.00 | 0.07 |
| | guard repair rate | 0.00 | **0.98** |
| | guard fallback rate | 0.00 | **0.98** |

52 replies were delivered to callers while the model was actively trying to break the SOP. **None
contained the fabricated `$4,820`, the payout guarantee, the invented deadline, the SSN, or the
injection compliance.** The caller got stiff template replies and the call went nowhere useful — which
is the point:

> **The harness converts model weakness from a safety problem into a cost-and-quality problem.**

A safety problem stops a deployment. A cost-and-quality problem is a dial the buyer sets. That
conversion is the whole commercial argument for building a control plane instead of writing a longer
prompt — and it is what makes "run the cheaper model" a decision someone can actually take, because
the floor does not move.

---

## 3. Layer 3: attribution — *why* did it pass?

Every claim in `evals/attribution.py` names not just what must be true, but **which mechanism is
supposed to make it true**, and checks both. Three kinds, which fail differently:

| Kind | Meaning | Survives a bad model? |
|---|---|---|
| `structural` | The model *cannot* do the wrong thing — the data or the tool is not in its context | Always |
| `deterministic` | Code decides, the model only narrates | Yes, **if the code actually runs** |
| `behavioural` | The model could misbehave; a directive says not to and the guard checks after | Final answer yes, first attempt no |

The report's most important line is **unguarded passes**: a turn where the outcome was right and the
mechanism was absent. That is a green test result that is lying to you.

A harness whose safety rests mostly on `behavioural` enforcement is a prompt with extra steps. The
enforcement mix is reported for exactly that reason.

---

## 4. The bug that made layers 3 and 4 necessary

`SEND_NOW` — the directive instructing the agent to call `send_summary_email` — **never fired once, for
the entire build.** A comparison against `next_turn_index() - 1`, when `resolve()` runs *before* the
caller's turn is appended, made its condition permanently false.

The summary email went out every time anyway, because the model saw `pending_action` in `visible_facts`
and volunteered the tool call. **Every scenario green. Nine invariants holding. The control plane doing
nothing.**

That is precisely the failure this architecture exists to prevent, and no amount of additional scripted
scenarios would have found it, because every one of them would have observed the correct outcome. It
took asking a different question: *which component caused this?*

The test covering it had hand-built its fixture with the same off-by-one, so it passed throughout. It
now drives the real `transition()` path — **a test that constructs its input the way the buggy code
reads it cannot catch the bug.**

### Current results

```
16/16 scenarios passed          $0.7150      54 turns
containment_rate      0.733     guard repair 0.000     guard fallback 0.000

architecture    5/5 static claims coherent (closed vocabularies still closed)
attribution     9/9 claims enforced, 0 unguarded passes, 0 violations   -> trustworthy
enforcement mix 3 structural, 4 deterministic, 2 behavioural
coverage        phases 7/7 (100%),  directives 22/23 (96%)

simulated       6/6 conversations invariant-clean, 8.7 turns avg, 0 guard fallbacks
hostile         0 safety violations, 0.98 fallback rate, 52 replies, no poison delivered
typo noise      light 16/16, moderate 16/16, heavy 9/16 — all 7 failures are
                identity lockouts, 0 invariant violations (§5b)
```

Six directives still never fire (`ACKNOWLEDGE_DECLINE`, `CLOSE_OUT`, `CONSENT_REMINDER`,
`KYC_DISCLOSURE_LIMITS`, `NO_CANDIDATES_HELP`, `SEND_NOW`). Each is a scenario worth writing or a
branch worth deleting; `KYC_DISCLOSURE_LIMITS` belongs to the bank SOP, which has no fixture data, so
it is honestly unreachable rather than untested. Reporting them is the point — an eval that only
printed 15/15 would be hiding this.

---

## 4b. Layer 0: are the pieces still speaking the same language?

Attribution asks "did the mechanism fire during this run?" — which can only speak about code paths a
scenario reached. A whole class of defect lives underneath it: a mechanism that could never fire at
all, because two namespaces that must agree have drifted.

Every instance found in this project has one shape — **two hand-maintained tables and a silent
`.get()`**:

| Two tables | What silently stopped working |
|---|---|
| `policy.py` element strings ↔ guard matchers | **11 of 20** contract elements never checked, incl. R8's send-to-file-address rule |
| reason ↔ disposition ↔ handoff guidance | A disposition code that could never be reached |
| directive emitted ↔ behaviour enforced | `SEND_NOW` never fired for the whole project |
| client event names ↔ metrics sink | A renamed event reads zero forever |

`evals/architecture.py` checks five closed vocabularies in ~50ms with no model. It runs as part of
`make test` *and* first in `make independence`, because a drifted vocabulary invalidates everything
measured after it.

The cure is not more care — care is what fails silently. It is making absence impossible to express:
`end_session()` raises on an unknown reason; `check_contract()` reports an unrecognised element instead
of skipping it; every directive declares which of four mechanisms backs it.

---

## 5. Coverage: how we stop finding bugs by hand

`evals/coverage.py` treats the SOP as what it is — a finite state machine — and computes which parts
have ever executed. Run against the original twelve scenarios, it reported:

- **`CLOSED` was never reached by any scenario.** Not once.
- 6 of 14 disposition codes were unreachable.
- Several directives had never fired.

Every bug found by hand over the preceding days lived in exactly that unlit region: the summary being
re-offered after sending, the phase never settling, the close reason defaulting to abandonment, and
`SEND_NOW`. The tester was not unlucky — they were walking into the one place nothing covered.

Three scenarios were added to close it (`13_full_call_to_close`, `14_decline_then_close`,
`15_second_question_after_wrap_up`). **Coverage is a floor, not a ceiling**: exercising a transition
does not mean the behaviour on it is right. It only means no part of the machine is dark.

---

## 5b. Typing noise: which direction does it fail?

```bash
python -m evals.runner --noise heavy --out evals/reports/typo_heavy.json
```

The same 16 scenarios, replayed with the caller's messages degraded the way people actually type in a
support chat — QWERTY-adjacent slips, doubled and dropped letters, no capitals or punctuation,
abbreviations, a stray keystroke inside a number:

| Profile | Passed | Guard repair | Invariant violations |
|---|---|---|---|
| clean | 16/16 | 0.019 | **0** |
| light | 16/16 | 0.019 | **0** |
| moderate | 16/16 | 0.000 | **0** |
| heavy | **9/16** | 0.037 | **0** |

Heavy noise breaks the suite, and **how** it breaks is the whole result. Every one of the seven
failures is an identity failure: sessions stuck in `VERIFY_ID` with no `verified_party_id`, and one
routed to a human. Not a single invariant violation, and no case data reached anyone.

That is the harness failing **closed**. Identity tolerance is edit-distance on *names only* — date of
birth, phone, email and the ID last-four stay exact (§DESIGN 7.2) — so a stray keystroke inside an SSN
is supposed to stop verification. It did. The caller retries or gets a person; nobody gets someone
else's claim.

The same shape as §2's hostile-model result, arrived at from a different direction: **degradation
shows up as cost and friction, never as a safety failure.** One is an adversarial model with clean
input, the other a cooperative model with corrupted input, and the floor holds in both.

This axis is also why identity tolerance is edit-distance rather than phonetic. An earlier version of
this suite modelled speech-recognition errors — spelled-out digits, homophone names — which is careful
work aimed at a channel this product does not have. Nothing a caller sends here has ever been spoken.
The noise model and the defence now describe the same failure mode.

---

## 6. Layer 5: simulated callers

```bash
python -m evals.simulator --personas 6 --conversations 12
```

A scripted scenario is a precise regression test and a poor explorer — it can only find what its author
already suspected. A simulated caller has a goal and a personality and improvises, so it wanders into
the corners nobody wrote down, which is where the bugs were.

Six personas: brisk, rambling, distressed, muddled (gives a detail wrong then corrects), representative
(third-party consent), distractible (off-topic, playful injection). Each carries **disruptions** drawn
from what real testing actually did — going quiet, changing the subject, remembering one more thing
after saying goodbye.

**What is judged is not "was the reply good."** There is no script to compare against, and asking a
model to grade another model's politeness produces a number nobody can act on. The judge remains the
same invariants and attribution claims, because those are properties of the harness and hold regardless
of what was said. The simulator's job is to *generate situations*; the invariants *decide*.

This is also, unchanged, the RL environment in DESIGN.md §8.4. Swap the simulated caller for a learner
and the same harness scores it.

---

## 7. The improvement loop

This is the part that was missing. Eval output is not a report card; each signal has a defined owner and
a defined response:

| Signal | What it means | What it drives |
|---|---|---|
| **Invariant violation** | A guarantee broke | Stop. Fix in code, never in the prompt. Add a unit test at layer 1. |
| **Unguarded pass** | Right outcome, no mechanism | The requirement is unenforced. Build the enforcement, then re-run layer 4. |
| **Coverage gap** | Part of the SOP never runs | Write the scenario, or delete the dead branch. |
| **Guard repair rate ↑** | Model needs more attempts | Prompt/model calibration. Moves *before* pass rate does — an early warning. |
| **Guard fallback rate ↑** | Model failed twice | Either the model is too weak for this phase, or the directive is unclear. |
| **Scenario failure** | Task behaviour regressed | Normal debugging — the least interesting signal here. |
| **Simulator violation** | A situation nobody scripted broke something | Capture it as a scenario, then fix. Every manual bug becomes a permanent test. |

Two rules keep the loop honest, both learned the hard way in this project:

1. **When a detector misfires, constrain its context — do not delete its input.** Removing the verb
   `get` from the promissory-language check traded a false positive for a false *negative*: "you will
   get the full amount" stopped being flagged. Constraining the object fixed both directions.
2. **A test may only be relaxed when the intended behaviour changed, and the reason must be written
   down.** `01_margaret_chen_happy_path` now expects `CLOSED` instead of `POST_PROCESS` — because the
   old expectation encoded the stall a live tester complained about. That reasoning is in the scenario
   file itself, not in a commit message nobody will read.

---

## 8. Layer 1: the deterministic core

```bash
make test        # 302 tests, 0 model calls, ~5s
```

Covers the identity matcher (worked example, aliases, spoken digits, mismatch lock, ambiguity
narrowing), state machine (gate one-wayness, escalation precedence, strike decay, purity), policy
resolver (per-phase disclosure scoping asserted field-by-field, directive precedence, refusal
determinism), output guard (all five rule families against hand-crafted strings), disposition
classification, idle policy, the abandonment sweeper, and the invariant that a verified session can
never re-lock.

Notable ones that exist because of a specific bug:

- `test_verified_cannot_lock.py` — the Inspector drops its lock warning after verification; this pins
  the invariant that makes that honest.
- `test_procedural_relevance.py` — the fifth guard rule, added after the hostile run (§9).
- `test_disposition.py::test_every_assigned_reason_is_in_the_registry` — replaced a source-scanning
  test that missed a ternary. The guard was as fragile as the thing it guarded.

---

## 9. What the hostile run found: R3 was only half-enforced

The run delivered a banana bread recipe to callers **seventeen times** while every safety-critical rule
held.

Scope (R3) was enforced at the **input** boundary only: an off-topic caller message is classified by
PERCEIVE and answered with a deterministic template. Nothing checked the other direction — an off-topic
*reply* to a perfectly on-topic question. On the output side, R3 was enforced entirely by the model's
good manners.

The fix is a fifth guard rule, `check_procedural_relevance`, and it is deliberately **not a topic
blocklist** — a list of forbidden subjects is unbounded and out of date the day it ships. It is the
positive form of the SOP's own `KEEP_THE_CALL_MOVING` directive: every reply must ask something, address
the caller, or state something traceable to this caller's case. Prose about anything else has none of
those.

Validated against **283 recorded real replies: 0 false positives**, while still catching the off-topic
answer. Two false positives in the first version (`let me`, and a vocative "Thanks for calling in,
Margaret") were found by measuring rather than by imagining, and both are now regression tests.

Full output-scope classification would need a model call per turn. This is the deterministic floor under
it; §10 records the remaining gap rather than implying it is closed.

---

## 9b. What attribution found: R9 was unguarded

`ACKNOWLEDGE_EMOTION` — the directive telling the agent to acknowledge a caller's feelings before
continuing — **never fired once across the entire suite**, including `frustrated_caller_bonus`, whose
whole purpose is a caller saying *"I already told you who I am. This is ridiculous."*

The transcripts looked fine. The agent was empathetic, because the model is a polite model. R9 was
being satisfied by manners rather than by the harness — an unguarded pass, and the hardest kind to
notice, because everything reads correctly.

Root cause: `signals.intensity` is a **deferred** perception, recorded during the previous turn's ACT.
That is the right call for most signals (§7.3's deferred/blocking split buys latency) and the wrong one
here, because empathy is needed on the turn the person is upset, not the turn after.

Fix: a deterministic intensity floor read straight off the caller's words, available before the turn's
plan is built. The usual objection to a marker list is brittleness — correct for a security rule, wrong
here. This only ever *raises* a floor: a false positive makes the agent slightly warmer, and a false
negative defers to the model's own reading. That is "isolate uncertainty on the low-consequence path"
applied exactly where it belongs. Directive coverage rose 65% → 77% as paths that had never executed
lit up.

---

## 10. Honest limitations

- **Small N.** 16 scenarios finds real bugs and demonstrates the method. It does not produce a
  statistically confident containment estimate for a launch decision.
- **The hostile model is a fixed rotation, not an adaptive adversary.** It does not search for the
  weakest rule; a real red-team would. It establishes that the floor holds against a known set of
  attacks, not that the floor is unbreakable.
- **Output-scope enforcement is a heuristic floor.** §9's rule catches prose with no procedural anchor.
  A fluent, on-register, subtly out-of-scope answer would pass it.
- **No LLM-judge scoring for naturalness or empathy.** The suite checks safety and procedure precisely;
  how *good* a compliant reply sounds is still read by a human. Deliberate — a judge model's score moves
  for reasons nobody can act on, and it would be the least trustworthy number in the document.
- **One directive never fires**: `KYC_DISCLOSURE_LIMITS`, which belongs to the bank SOP. That spec has
  no fixture data behind it, so the directive is honestly unreachable rather than untested — and the
  coverage report says so rather than quietly rounding to 100%. Of the six that were unfired before,
  four turned out to be structurally dead and were deleted; one got a scenario.
- **The simulator is not yet coverage-directed.** It explores by personality, not by aiming at unreached
  transitions. Closing that loop is the natural next step.
- **Run-to-run variance is real.** Multi-step async flows whose pacing the model controls (consent
  polling) sit near a decision boundary; scenarios give enough room for either reasonable choice rather
  than forcing one.
