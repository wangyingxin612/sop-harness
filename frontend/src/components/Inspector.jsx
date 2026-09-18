import { Fragment } from "react";

const PHASE_ORDER = ["VERIFY_ID", "RESOLVE_INTENT", "PROCESS_CASE", "POST_PROCESS", "CLOSED"];
const TERMINAL = ["HUMAN_HANDOFF", "ABUSE_TERMINATED"];

function PhaseStepper({ phase }) {
  const isTerminal = TERMINAL.includes(phase);
  const currentIdx = PHASE_ORDER.indexOf(phase);
  return (
    <div className="phase-stepper">
      {PHASE_ORDER.map((p, i) => {
        let cls = "phase-pill";
        if (!isTerminal) {
          if (p === phase) cls += " active";
          else if (i < currentIdx) cls += " done";
        }
        return (
          <span key={p} className={cls}>
            {p.replace("_", " ")}
          </span>
        );
      })}
      {isTerminal && <span className="phase-pill terminal active">{phase.replace("_", " ")}</span>}
    </div>
  );
}

function IdentityMeter({ facts }) {
  const pct = Math.min(100, (facts.matched_factor_count / 3) * 100);
  const allFactors = ["full_name", "dob", "phone", "email", "id_last4"];
  return (
    <div>
      <div className="meter-row">
        <span>Identity factors</span>
        <div className="meter-track">
          <div className={`meter-fill ${facts.matched_factor_count >= 3 ? "good" : ""}`} style={{ width: `${pct}%` }} />
        </div>
        <span>{facts.matched_factor_count}/3</span>
      </div>
      <div className="factor-chip-row">
        {allFactors.map((f) => (
          <span key={f} className={`factor-chip ${facts.matched_factor_types.includes(f) ? "matched" : ""}`}>
            {f}
          </span>
        ))}
      </div>
      {facts.mismatch_count > 0 && (
        <div style={{ marginTop: 6 }}>
          <span className="badge badge-bad">{facts.mismatch_count} mismatch{facts.mismatch_count > 1 ? "es" : ""}</span>
        </div>
      )}
    </div>
  );
}

function SlotItem({ label, slot }) {
  if (!slot) return null;
  return (
    <div className="slot-item">
      <div className="slot-label">{label}{slot.superseded ? " (corrected)" : ""}</div>
      <div className="slot-value">{String(slot.value)}</div>
      <div className="slot-quote">“{slot.verbatim_quote}”</div>
    </div>
  );
}

function LastTurnDetail({ trace }) {
  if (!trace) return <div className="empty-hint">No turns yet.</div>;
  const lastGuard = trace.guard_attempts?.[trace.guard_attempts.length - 1];
  return (
    <div>
      <dl className="kv" style={{ marginBottom: 10 }}>
        <dt>Route</dt>
        <dd>{trace.plan.route}</dd>
        <dt>Model tier</dt>
        <dd>{trace.plan.model_tier}</dd>
        <dt>Phase</dt>
        <dd>{trace.phase_before} → {trace.phase_after}</dd>
      </dl>

      {trace.plan.directives?.length > 0 && (
        <div style={{ marginBottom: 8 }}>
          {trace.plan.directives.map((d) => (
            <span key={d} className="badge badge-neutral">{d}</span>
          ))}
        </div>
      )}

      {trace.memory_updates?.case_hint && (
        <div style={{ marginBottom: 8, fontSize: 12 }}>
          <strong>Hint recorded:</strong> {trace.memory_updates.case_hint.case_type || "?"} /{" "}
          {trace.memory_updates.case_hint.status || "?"} / {trace.memory_updates.case_hint.time_ref || "?"}
        </div>
      )}
      {trace.memory_updates?.intent && (
        <div style={{ marginBottom: 8, fontSize: 12 }}>
          <strong>Intent:</strong> {trace.memory_updates.intent}
        </div>
      )}

      <div style={{ marginBottom: 4, fontSize: 11, color: "var(--text-dim)" }}>Output guard</div>
      {trace.guard_attempts?.map((g, i) => (
        <div key={i} className={`guard-attempt ${g.violations?.length ? "fail" : "ok"}`}>
          {g.violations?.length ? `attempt ${i + 1}: ${g.violations.length} violation(s)` : `attempt ${i + 1}: passed`}
          {g.empty && <span className="badge badge-bad">empty reply</span>}
          {g.truncated && <span className="badge badge-bad">truncated</span>}
          {g.fallback && <span className="badge badge-neutral">safe template used</span>}
          {g.violations?.map((v, j) => (
            <div key={j} className="guard-viol">· {v}</div>
          ))}
        </div>
      ))}

      {trace.tool_effects?.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>Tool effects</div>
          {trace.tool_effects.map((t, i) => (
            <div key={i} className="tool-effect">🔧 {t.summary}</div>
          ))}
        </div>
      )}

      {trace.plan.visible_facts?.handoff_packet && (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontSize: 11, color: "var(--text-dim)", marginBottom: 4 }}>
            Handoff packet — nothing here needs to be repeated to the human
          </div>
          <dl className="kv">
            {Object.entries(trace.plan.visible_facts.handoff_packet).map(([k, v]) => (
              <Fragment key={k}>
                <dt>{k.replace(/_/g, " ")}</dt>
                <dd>{Array.isArray(v) ? (v.length ? v.join("; ") : "—") : String(v ?? "—")}</dd>
              </Fragment>
            ))}
          </dl>
        </div>
      )}

      <details className="raw-json" style={{ marginTop: 10 }}>
        <summary>Raw trace JSON</summary>
        <pre>{JSON.stringify(trace, null, 2)}</pre>
      </details>
    </div>
  );
}

export default function Inspector({ state }) {
  if (!state) {
    return (
      <div className="inspector-pane">
        <div className="insp-section">
          <div className="empty-hint">Create a session to see the SOP engine's internal state here.</div>
        </div>
      </div>
    );
  }

  const { facts, memory } = state;
  const lastAgentTurn = [...state.transcript].reverse().find((t) => t.role === "agent" && t.trace_event);
  const lastTrace = lastAgentTurn?.trace_event;

  return (
    <div className="inspector-pane">
      <div className="insp-section">
        <h3>Phase</h3>
        <PhaseStepper phase={state.phase} />
        {facts.escalation_reason && (
          <div style={{ marginTop: 8 }}>
            <span className="badge badge-bad">reason: {facts.escalation_reason}</span>
          </div>
        )}
      </div>

      <div className="insp-section">
        <h3>Identity verification</h3>
        <IdentityMeter facts={facts} />
        <dl className="kv" style={{ marginTop: 10 }}>
          <dt>Caller role</dt>
          <dd>{facts.caller_role}</dd>
          {facts.representative_of_party_id && (
            <>
              <dt>Representing</dt>
              <dd>{facts.representative_of_party_id}</dd>
            </>
          )}
          {facts.verified_party_id && (
            <>
              <dt>Verified party</dt>
              <dd>{facts.verified_party_id}</dd>
            </>
          )}
          {facts.caller_role === "representative" && (
            <>
              <dt>Consent status</dt>
              <dd>{facts.consent_status} {facts.consent_poll_count > 0 && `(${facts.consent_poll_count} poll${facts.consent_poll_count > 1 ? "s" : ""})`}</dd>
            </>
          )}
        </dl>
      </div>

      <div className="insp-section">
        <h3>Memory (cross-phase, with provenance)</h3>
        {Object.keys(memory.identity_slots).length === 0 &&
          memory.case_hints.length === 0 &&
          !memory.resolved_intent && <div className="empty-hint">Nothing recorded yet.</div>}
        {Object.entries(memory.identity_slots).map(([k, slot]) => (
          <SlotItem key={k} label={k.replace("_", " ")} slot={slot} />
        ))}
        {memory.case_hints.map((h, i) => (
          <div key={i} className="slot-item">
            <div className="slot-label">case hint (turn {h.turn_index})</div>
            <div className="slot-value">{[h.case_type, h.status, h.time_ref].filter(Boolean).join(" · ") || "—"}</div>
          </div>
        ))}
        {memory.resolved_intent && (
          <div className="slot-item">
            <div className="slot-label">resolved intent</div>
            <div className="slot-value">{memory.resolved_intent}</div>
            <div className="slot-quote">“{memory.intent_evidence_quote}”</div>
          </div>
        )}
        {memory.candidate_case_id && (
          <div className="slot-item">
            <div className="slot-label">candidate case (awaiting confirmation)</div>
            <div className="slot-value">{memory.candidate_case_id}</div>
          </div>
        )}
        {memory.confirmed_case_id && (
          <div className="slot-item">
            <div className="slot-label">confirmed case</div>
            <div className="slot-value">{memory.confirmed_case_id}</div>
          </div>
        )}
        {memory.followup_notes.length > 0 && (
          <div className="slot-item">
            <div className="slot-label">follow-up notes</div>
            {memory.followup_notes.map((n, i) => (
              <div key={i} className="slot-value" style={{ fontWeight: 400, fontSize: 12 }}>
                • {n}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="insp-section">
        <h3>Last turn</h3>
        <LastTurnDetail trace={lastTrace} />
      </div>

      <div className="insp-section">
        <h3>Usage this session</h3>
        <div className="cost-grid">
          <div className="cost-tile">
            <div className="num">${facts.cost_usd.toFixed(4)}</div>
            <div className="label">total cost</div>
          </div>
          <div className="cost-tile">
            <div className="num">{facts.tokens_used.toLocaleString()}</div>
            <div className="label">tokens</div>
          </div>
          <div className="cost-tile">
            <div className="num">{facts.turns_used}</div>
            <div className="label">turns</div>
          </div>
          <div className="cost-tile">
            <div className="num">{facts.off_topic_strikes}</div>
            <div className="label">off-topic strikes</div>
          </div>
        </div>
      </div>
    </div>
  );
}
