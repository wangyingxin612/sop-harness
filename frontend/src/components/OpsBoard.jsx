import { useEffect, useState } from "react";
import { listSessions, exportUrl } from "../api.js";

/**
 * Cross-session operations board.
 *
 * WHY A FUNNEL AND NOT A STATUS LIST. The obvious board groups sessions by
 * "state" — some in progress, some closed, some transferred. That answers
 * "what is happening", which nobody is asking. The question an operations
 * lead actually has is "where do calls stop?", because that is the only
 * version of the question that implies an action: a pile-up in VERIFY_ID is
 * an identity-data problem, a pile-up in PROCESS_CASE is a knowledge-base
 * problem, and a wide TRANSFERRED column is a staffing problem.
 *
 * So the columns are the SOP's own phases in order, followed by the terminal
 * outcomes. Live and finished sessions share one axis, and the shape of the
 * board is the shape of the funnel.
 *
 * The second decision is that CONTAINED and TRANSFERRED sit next to each
 * other but ABANDONED is kept separate rather than folded into failure. A
 * caller who walked away did not defeat the automation, and a board that
 * mixes the two produces a number that moves for two unrelated reasons.
 */

const PHASE_COLUMNS = [
  { key: "VERIFY_ID", title: "Verifying", hint: "identity not yet established" },
  { key: "RESOLVE_INTENT", title: "Finding the case", hint: "which claim, and what about it" },
  { key: "PROCESS_CASE", title: "Working the case", hint: "answering from the record" },
  { key: "POST_PROCESS", title: "Wrapping up", hint: "summary decision outstanding" },
];

const OUTCOME_COLUMNS = [
  { key: "contained", title: "Contained", hint: "finished without a person" },
  { key: "transferred", title: "Transferred", hint: "handed to a human queue" },
  { key: "abandoned", title: "Abandoned", hint: "caller went silent" },
  { key: "terminated", title: "Terminated", hint: "ended on policy grounds" },
];

const TERMINAL = new Set(["CLOSED", "HUMAN_HANDOFF", "ABUSE_TERMINATED"]);

function relative(iso) {
  if (!iso) return "";
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function Card({ row, current }) {
  const d = row.disposition;
  return (
    <div className={`ops-card ${d.review_flag ? "flagged" : ""} ${current ? "current" : ""}`}>
      <div className="ops-card-head">
        <span className="ops-id">{row.session_id}</span>
        <span className="ops-when">{relative(row.last_activity_at)}</span>
      </div>

      <div className="ops-label">{d.label}</div>

      {row.last_caller_message && (
        <div className="ops-quote">“{row.last_caller_message}”</div>
      )}

      <div className="ops-tags">
        <span className={`ops-tag ${row.verified ? "ok" : "warn"}`}>
          {row.verified ? "verified" : "unverified"}
        </span>
        {row.caller_role === "representative" && (
          <span className="ops-tag">rep · consent {row.consent_status}</span>
        )}
        {row.case_id && <span className="ops-tag">{row.case_id}</span>}
        {row.peak_intensity >= 2 && <span className="ops-tag warn">upset {row.peak_intensity}/3</span>}
        {d.routing_hint !== "none" && <span className="ops-tag route">→ {d.routing_hint}</span>}
      </div>

      <div className="ops-card-foot">
        <span>{row.turns_used} turns · ${row.cost_usd.toFixed(4)}</span>
        <a href={exportUrl(row.session_id)} target="_blank" rel="noreferrer">transcript</a>
      </div>
    </div>
  );
}

export default function OpsBoard({ currentSessionId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [reviewOnly, setReviewOnly] = useState(false);

  async function load() {
    try {
      setData(await listSessions());
      setError(null);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
    // Slow refresh on purpose. The board is for reading a shape, not for
    // watching individual cards move; a fast poll would cost more than the
    // conversations it is describing.
    const id = setInterval(load, 15000);
    return () => clearInterval(id);
  }, []);

  if (error) return <div className="ops-empty">Couldn’t load sessions: {error}</div>;
  if (!data) return <div className="ops-empty">Loading…</div>;

  const rows = reviewOnly
    ? data.sessions.filter((r) => r.disposition.review_flag)
    : data.sessions;

  if (data.sessions.length === 0) {
    return (
      <div className="ops-empty">
        No sessions yet. Start a conversation and it will appear here — along with every other
        session this deployment has handled.
      </div>
    );
  }

  const live = rows.filter((r) => !TERMINAL.has(r.phase));
  const done = rows.filter((r) => TERMINAL.has(r.phase));
  const r = data.rollup;

  return (
    <div className="ops-pane">
      <div className="ops-rollup">
        <div className="ops-stat">
          <div className="ops-stat-num">{r.total}</div>
          <div className="ops-stat-label">sessions</div>
        </div>
        <div className="ops-stat">
          <div className="ops-stat-num">
            {r.containment_rate === null ? "—" : `${Math.round(r.containment_rate * 100)}%`}
          </div>
          {/* "—" not "0%": no finished calls yet is a different claim from
              "none of them were contained". */}
          <div className="ops-stat-label">contained</div>
        </div>
        <div className="ops-stat">
          <div className="ops-stat-num">{r.needs_review}</div>
          <div className="ops-stat-label">flagged for review</div>
        </div>
        <div className="ops-stat">
          <div className="ops-stat-num">${r.total_cost_usd.toFixed(3)}</div>
          <div className="ops-stat-label">total spend</div>
        </div>

        <div className="spacer" />
        <label className="ops-filter">
          <input
            type="checkbox"
            checked={reviewOnly}
            onChange={(e) => setReviewOnly(e.target.checked)}
          />
          flagged only
        </label>
        <a className="btn btn-sm" href="/api/evals/report" target="_blank" rel="noreferrer">
          Eval report
        </a>
      </div>

      <div className="ops-section-label">In flight — where the call currently is</div>
      <div className="ops-columns">
        {PHASE_COLUMNS.map((col) => {
          const cards = live.filter((x) => x.phase === col.key);
          return (
            <div className="ops-col" key={col.key}>
              <div className="ops-col-head">
                <span className="ops-col-title">{col.title}</span>
                <span className="ops-col-count">{cards.length}</span>
              </div>
              <div className="ops-col-hint">{col.hint}</div>
              <div className="ops-col-body">
                {cards.map((x) => (
                  <Card key={x.session_id} row={x} current={x.session_id === currentSessionId} />
                ))}
              </div>
            </div>
          );
        })}
      </div>

      <div className="ops-section-label">Finished — how the call ended</div>
      <div className="ops-columns">
        {OUTCOME_COLUMNS.map((col) => {
          const cards = done.filter((x) => x.disposition.outcome_class === col.key);
          return (
            <div className={`ops-col outcome-${col.key}`} key={col.key}>
              <div className="ops-col-head">
                <span className="ops-col-title">{col.title}</span>
                <span className="ops-col-count">{cards.length}</span>
              </div>
              <div className="ops-col-hint">{col.hint}</div>
              <div className="ops-col-body">
                {cards.map((x) => (
                  <Card key={x.session_id} row={x} current={x.session_id === currentSessionId} />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
