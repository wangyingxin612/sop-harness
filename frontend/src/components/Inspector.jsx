import { Fragment } from "react";

/* The inspector's job is to make one claim visible: the SOP is being
   enforced, right now, by something other than the model's good intentions.
   Every element below is here because it supports that claim or because an
   operator needs it to act. Internal identifiers and developer detail are
   deliberately not in the default view — they served me while building and
   nobody else since. */

const PHASES = [
  { id: "VERIFY_ID", label: "Verify identity", gate: "≥3 identity factors" },
  { id: "RESOLVE_INTENT", label: "Resolve intent", gate: "caller confirms the claim" },
  { id: "PROCESS_CASE", label: "Process case", gate: null },
  { id: "POST_PROCESS", label: "Wrap up", gate: "explicit consent to email" },
  { id: "CLOSED", label: "Closed", gate: null },
];
const TERMINALS = {
  HUMAN_HANDOFF: "Handed to a human",
  ABUSE_TERMINATED: "Session ended",
};

const DIRECTIVE_LABELS = {
  ACKNOWLEDGE_EMOTION: "Acknowledge how they feel first",
  OFFER_ALTERNATIVE_FACTORS: "Offer a different ID factor",
  STATE_FACTORS_REMAINING: "Say how many factors remain",
  DISAMBIGUATION_HELP: "Ask which claim they mean",
  NO_CANDIDATES_HELP: "Nothing matches — ask them to rephrase",
  CONSENT_REMINDER: "Still need a send/skip decision",
  SEND_NOW: "Send the summary now",
  ACKNOWLEDGE_DECLINE: "Accept the decline without pushback",
  CLOSE_OUT: "Close the call warmly",
  REPRESENTATIVE_SCOPE_NOTE: "Representative — reduced disclosure",
  REFUSAL_TEMPLATE: "Use the exact refusal wording",
  VERIFY_RATIONALE: "Explain why verification protects them",
  NO_DISCLOSURE: "Disclose nothing about the case",
  INDEX_ONLY: "Claim type/status/date only",
  CONFIRM_CANDIDATE: "Restate the claim and confirm",
  GROUNDING_ONLY: "Answer only from provided data",
  NO_COMMITMENT: "Never promise an outcome",
  ALTERNATIVE_LADDER_FIRST: "Offer alternatives before a transfer",
  OFFER_SUMMARY: "Offer the summary email",
  CONSENT_REQUIRED: "Don't send without clear agreement",
  SEND_TO_FILE_ADDRESS_ONLY: "Only the address on file",
  HANDOFF_CLOSING: "Explain what's being carried over",
  ABUSE_CLOSING: "Close firmly and politely",
  SESSION_CLOSING: "Close warmly",
};

function PhaseRail({ phase, facts }) {
  const terminal = TERMINALS[phase];
  const idx = PHASES.findIndex((p) => p.id === phase);

  return (
    <div className="rail">
      {PHASES.map((p, i) => {
        const done = !terminal && i < idx;
        const current = !terminal && i === idx;
        const locked = !terminal && i > idx;
        const cls = ["rail-step", done && "is-done", current && "is-current", locked && "is-locked"]
          .filter(Boolean)
          .join(" ");
        return (
          <div key={p.id} className={cls}>
            <span className="rail-dot">{done ? "✓" : locked ? "🔒" : i + 1}</span>
            <span className="rail-label">{p.label}</span>
            {current && <span className="rail-state open">current</span>}
            {locked && p.gate && <span className="rail-state locked">gated</span>}
          </div>
        );
      })}
      {terminal && (
        <div className="rail-step is-current is-stopped">
          <span className="rail-dot">!</span>
          <span className="rail-label">{terminal}</span>
          <span className="rail-state stopped">{facts.escalation_reason?.replace(/_/g, " ")}</span>
        </div>
      )}
    </div>
  );
}

function GateNote({ phase, facts }) {
  if (TERMINALS[phase]) return null;
  if (phase === "VERIFY_ID") {
    const left = Math.max(0, 3 - facts.matched_factor_count);
    return (
      <div className="gate-note">
        No claim data has been fetched. {left > 0 ? `${left} more identity factor${left > 1 ? "s" : ""} needed.` : "Gate clearing…"}
      </div>
    );
  }
  if (phase === "RESOLVE_INTENT") {
    return <div className="gate-note">Claim list visible — type, status and date only. No amounts or reasons.</div>;
  }
  if (facts.caller_role === "representative" && facts.consent_status !== "approved") {
    return <div className="gate-note">Representative without consent — narrative and amounts withheld.</div>;
  }
  return <div className="gate-note open">Full detail for the confirmed claim is available.</div>;
}

function IdentityCard({ facts, phoneticUsed, policy }) {
  const verified = facts.verification_status === "verified";
  const needed = policy?.min_distinct_factors ?? 3;
  const maxMismatches = policy?.max_mismatches ?? 2;
  const remaining = Math.max(0, maxMismatches - facts.mismatch_count);
  return (
    <section className="card">
      <h3 className="card-title">
        Identity gate
        <span className={`rail-state ${facts.matched_factor_count >= needed ? "open" : "locked"}`}>
          {facts.matched_factor_count}/{needed}
        </span>
      </h3>

      <div className="segbar">
        {Array.from({ length: needed }, (_, i) => (
          <span key={i} className={`seg ${i < facts.matched_factor_count ? "filled" : ""}`} />
        ))}
      </div>

      <div className="factor-chips">
        {(policy?.factor_types ?? ["full_name", "dob", "phone", "email", "id_last4"]).map((f) => {
          const matched = facts.matched_factor_types.includes(f);
          const isPhonetic = matched && f === "full_name" && phoneticUsed;
          return (
            <span key={f} className={`chip ${matched ? (isPhonetic ? "phonetic" : "matched") : ""}`}>
              {f.replace("_", " ")}
              {isPhonetic ? " ~" : ""}
            </span>
          );
        })}
      </div>

      {phoneticUsed && (
        <div className="gate-note" style={{ marginTop: 10 }}>
          Name matched by sound, not exactly — recorded in the audit trail.
        </div>
      )}
      {/* "Two locks the session" is a warning about something that can still
          happen. Once the caller is through the gate it cannot: the identity
          matcher only runs in VERIFY_ID (see machine.py — the phase branch
          is the only caller of _identity_phase_transition), so mismatch_count
          can never grow again and the lock can never fire on this path.
          Leaving the red threat up after a successful verification was the
          UI telling the caller they were still in danger of being locked out
          of a session they had already been admitted to. Verified, it becomes
          what it actually is: an audit fact. */}
      {facts.mismatch_count > 0 && (
        verified ? (
          <div className="gate-note" style={{ marginTop: 10 }}>
            {facts.mismatch_count} detail{facts.mismatch_count > 1 ? "s" : ""} didn't match before
            verification succeeded — recorded in the audit trail.
          </div>
        ) : (
          <div className="gate-note stopped" style={{ marginTop: 10 }}>
            {facts.mismatch_count} detail{facts.mismatch_count > 1 ? "s" : ""} didn't match.
            {" "}
            {remaining === 1
              ? "One more locks the session."
              : `${remaining} more lock the session.`}
          </div>
        )
      )}

      {(facts.caller_role !== "unknown" || facts.verified_party_id) && (
        <dl className="kv" style={{ marginTop: 12 }}>
          <dt>Caller</dt>
          <dd>{facts.caller_role === "representative" ? "Authorised representative" : "Policyholder"}</dd>
          {facts.caller_role === "representative" && (
            <>
              <dt>Consent</dt>
              <dd className={facts.consent_status === "approved" ? "" : "mono"}>
                {facts.consent_status.replace(/_/g, " ")}
                {facts.consent_poll_count > 0 && ` · checked ${facts.consent_poll_count}×`}
              </dd>
            </>
          )}
        </dl>
      )}
    </section>
  );
}

function groupBySource(slots) {
  const byQuote = new Map();
  for (const [key, slot] of slots) {
    const q = slot.verbatim_quote || "";
    if (!byQuote.has(q)) byQuote.set(q, []);
    byQuote.get(q).push([key, slot]);
  }
  return [...byQuote.entries()];
}

function EvidenceCard({ memory }) {
  const slots = Object.entries(memory.identity_slots);
  const nothing =
    slots.length === 0 && memory.case_hints.length === 0 && !memory.resolved_intent && !memory.confirmed_case_id;

  return (
    <section className="card">
      <h3 className="card-title">What it remembers · with sources</h3>
      {nothing && <p className="muted">Nothing recorded yet.</p>}

      {/* Slots extracted from the SAME utterance share one citation. Printing
          the caller's sentence once per field turned a three-field turn into
          the same long quote three times — noise that buries the thing the
          panel exists to show. */}
      {groupBySource(slots).map(([quote, group]) => (
        <div className="evidence" key={quote}>
          {group.map(([key, slot]) => (
            <div className="evidence-head" key={key} style={{ marginBottom: 2 }}>
              <span className="evidence-key">
                {key.replace("_", " ")}
                {slot.superseded && " · corrected"}
              </span>
              <span className="evidence-val">{String(slot.value)}</span>
            </div>
          ))}
          <div className="evidence-quote">“{quote}”</div>
        </div>
      ))}

      {memory.case_hints.length > 0 && (
        <div className="evidence">
          <div className="evidence-head">
            <span className="evidence-key">claim hints</span>
          </div>
          {/* Shows where the hint CAME FROM, not an internal transcript
              index. "turns 0, 2, 8, 10" was meaningless to anyone: those are
              positions in a list that counts both sides of the conversation,
              and they were only there because the same hint was being
              recorded repeatedly (now deduped in the state machine). */}
          {memory.case_hints.map((h, i) => (
            <div key={i}>
              <div className="evidence-val" style={{ fontSize: 12.5, fontWeight: 500 }}>
                {[h.case_type, h.status, h.time_ref].filter(Boolean).join(" · ") || "—"}
              </div>
              {h.verbatim_quote && <div className="evidence-quote">“{h.verbatim_quote}”</div>}
            </div>
          ))}
        </div>
      )}

      {memory.resolved_intent && (
        <div className="evidence">
          <div className="evidence-head">
            <span className="evidence-key">what they want</span>
            <span className="evidence-val">{memory.resolved_intent.replace(/_/g, " ")}</span>
          </div>
          {memory.intent_evidence_quote && <div className="evidence-quote">“{memory.intent_evidence_quote}”</div>}
        </div>
      )}

      {(memory.candidate_case_id || memory.confirmed_case_id) && (
        <div className="evidence">
          <div className="evidence-head">
            <span className="evidence-key">{memory.confirmed_case_id ? "confirmed claim" : "proposed, awaiting confirmation"}</span>
            <span className="evidence-val">{memory.confirmed_case_id || memory.candidate_case_id}</span>
          </div>
        </div>
      )}

      {memory.consent_events.length > 0 && (
        <div className="evidence">
          <div className="evidence-head">
            <span className="evidence-key">consent on record</span>
            <span className={`tag ${memory.consent_events.at(-1).decision === "approved" ? "open" : ""}`}>
              {memory.consent_events.at(-1).decision}
            </span>
          </div>
          <div className="evidence-quote">“{memory.consent_events.at(-1).quote}”</div>
        </div>
      )}

      {memory.followup_notes.length > 0 && (
        <div className="evidence">
          <div className="evidence-head"><span className="evidence-key">notes on file</span></div>
          {memory.followup_notes.map((n, i) => (
            <div key={i} style={{ fontSize: 12.5 }}>• {n}</div>
          ))}
        </div>
      )}
    </section>
  );
}

/** Where this turn's wall-clock time actually went.
 *
 *  Cost was visible here and latency was not, so "why did that feel slow?"
 *  had no answer in the product — the only way to find out was to guess. The
 *  stages are shown separately because they have different fixes: PERCEIVE
 *  is prompt size, ACT is model tier and reply length, and VERIFY being
 *  non-zero means the guard sent a draft back and paid for a second call. */
function LatencyBar({ latency }) {
  if (!latency) return null;
  const parts = [
    { key: "perceive", label: "read", cls: "lat-perceive" },
    { key: "act", label: "write", cls: "lat-act" },
    { key: "verify", label: "check", cls: "lat-verify" },
  ].filter((p) => (latency[p.key] ?? 0) > 0.01);
  const total = latency.total || 0.001;

  return (
    <div className="latency">
      <div className="latency-head">
        <span className="evidence-key">Time on this turn</span>
        <span className="latency-total">{total.toFixed(1)}s</span>
      </div>
      <div className="latency-bar">
        {parts.map((p) => (
          <span
            key={p.key}
            className={p.cls}
            style={{ width: `${((latency[p.key] / total) * 100).toFixed(1)}%` }}
            title={`${p.label} — ${latency[p.key].toFixed(2)}s`}
          />
        ))}
      </div>
      <div className="latency-legend">
        {parts.map((p) => (
          <span key={p.key}>
            <i className={p.cls} /> {p.label} {latency[p.key].toFixed(1)}s
          </span>
        ))}
        {latency.reply_words != null && (
          <span className="latency-words">{latency.reply_words} words</span>
        )}
      </div>
    </div>
  );
}

function TurnCard({ trace }) {
  if (!trace) return null;
  const attempts = trace.guard_attempts || [];
  const last = attempts[attempts.length - 1] || {};
  const intervened = attempts.length > 1;
  const packet = trace.plan.visible_facts?.handoff_packet;

  return (
    <section className="card">
      <h3 className="card-title">
        This turn
        <span className="tag">{trace.phase_before} → {trace.phase_after}</span>
      </h3>

      <div className={`verdict ${last.violations?.length ? "fail" : ""}`}>
        <span className="verdict-dot" />
        {last.violations?.length
          ? `Reply blocked — ${last.violations.length} violation(s)`
          : intervened
            ? `Reply approved after ${attempts.length} attempts`
            : "Reply approved by the output guard"}
      </div>
      {attempts
        .filter((a) => a.violations?.length)
        .flatMap((a, i) => a.violations.map((v, j) => (
          <div className="verdict-detail" key={`${i}-${j}`}>· {v}</div>
        )))}

      <LatencyBar latency={trace.latency_s} />

      {trace.plan.directives?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="evidence-key" style={{ marginBottom: 6 }}>Instructions in force</div>
          {trace.plan.directives.map((d) => (
            <span key={d} className="tag">{DIRECTIVE_LABELS[d] || d}</span>
          ))}
        </div>
      )}

      {trace.tool_effects?.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="evidence-key" style={{ marginBottom: 4 }}>Actions taken</div>
          {trace.tool_effects.map((t, i) => (
            <div key={i} className="tool-line">{t.summary}</div>
          ))}
        </div>
      )}

      {packet && (
        <div style={{ marginTop: 12 }}>
          <div className="evidence-key" style={{ marginBottom: 6 }}>
            Handoff packet — the caller repeats none of this
          </div>
          <dl className="kv">
            {Object.entries(packet)
              .filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length))
              .map(([k, v]) => (
                <Fragment key={k}>
                  <dt>{k.replace(/_/g, " ")}</dt>
                  <dd>{Array.isArray(v) ? v.join("; ") : String(v)}</dd>
                </Fragment>
              ))}
          </dl>
        </div>
      )}

      <details className="dev" style={{ marginTop: 12 }}>
        <summary>Developer trace</summary>
        <pre>{JSON.stringify(trace, null, 2)}</pre>
      </details>
    </section>
  );
}

const EFFORT_COPY = {
  quick: "a fact they already know",
  considered: "a choice to make",
  offline_task: "something to go and find",
};

/** Why the bot is waiting as long as it is — the number plus its derivation,
 *  because a silence budget nobody can see is a silence budget nobody can
 *  argue with. The parts are shown separately so it is obvious which one to
 *  change when the timing is wrong. */
function IdleNote({ policy, budgetSeconds }) {
  if (!policy) return null;
  const readingAndComplexity = Math.max(0, (budgetSeconds ?? 0) - policy.base_seconds);
  return (
    <div className="gate-note idle-note">
      Waiting up to <strong>{budgetSeconds ?? policy.base_seconds}s</strong> before checking in
      — {policy.base_seconds}s because this phase asks for {EFFORT_COPY[policy.response_effort] || "an answer"}
      {readingAndComplexity > 0 ? `, +${readingAndComplexity}s to read and act on the last reply` : ""}.
      Hard cap {Math.round(policy.max_session_idle_seconds / 60)} min.
    </div>
  );
}

export default function Inspector({ state, idleBudgetSeconds }) {
  if (!state) {
    return (
      <aside className="inspector-pane">
        <section className="card">
          <h3 className="card-title">Inspector</h3>
          <p className="muted">
            Start a session to watch the procedure enforce itself — which gates are shut, what the agent
            is allowed to know, and what the guard did with every reply.
          </p>
        </section>
      </aside>
    );
  }

  const { facts, memory } = state;
  const lastAgentTurn = [...state.transcript].reverse().find((t) => t.role === "agent" && t.trace_event);
  const trace = lastAgentTurn?.trace_event;
  const phoneticUsed = Object.values(memory.identity_slots).some((s) => s.tier === "phonetic");

  return (
    <aside className="inspector-pane">
      <section className="card">
        <h3 className="card-title">Where the call is</h3>
        <PhaseRail phase={state.phase} facts={facts} />
        <GateNote phase={state.phase} facts={facts} />
        {!TERMINALS[state.phase] && (
          <IdleNote policy={state.idle_policy} budgetSeconds={idleBudgetSeconds} />
        )}
      </section>

      <IdentityCard facts={facts} phoneticUsed={phoneticUsed} policy={state.identity_policy} />
      <EvidenceCard memory={memory} />
      <TurnCard trace={trace} />

      <section className="card">
        <h3 className="card-title">This session</h3>
        <div className="metrics">
          <div className="metric">
            <div className="metric-num">${facts.cost_usd.toFixed(4)}</div>
            <div className="metric-label">cost so far</div>
          </div>
          <div className="metric">
            <div className="metric-num">{facts.turns_used}</div>
            <div className="metric-label">turns</div>
          </div>
          <div className="metric">
            <div className="metric-num">{(facts.tokens_used / 1000).toFixed(1)}k</div>
            <div className="metric-label">tokens</div>
          </div>
          <div className="metric">
            <div className="metric-num">
              ${facts.turns_used ? (facts.cost_usd / facts.turns_used).toFixed(4) : "0.0000"}
            </div>
            <div className="metric-label">per turn</div>
          </div>
        </div>
        {facts.off_topic_strikes > 0 && (
          <div className="gate-note stopped" style={{ marginTop: 10 }}>
            {facts.off_topic_strikes} off-topic strike{facts.off_topic_strikes > 1 ? "s" : ""} — 3 ends the session.
          </div>
        )}
      </section>
    </aside>
  );
}
