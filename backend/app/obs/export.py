"""Human-readable transcript export (DESIGN.md §8.2: "two artifacts per
session — machine-readable JSONL [see session/store.py's append_trace] and
a human-readable HTML transcript")."""
from __future__ import annotations

import html

from app.sop.types import SessionState

_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 760px;
       margin: 40px auto; padding: 0 20px; color: #1a1a2e; background: #fafafa; }
h1 { font-size: 1.4rem; } .meta { color: #666; font-size: 0.85rem; margin-bottom: 24px; }
.turn { margin-bottom: 18px; padding: 12px 16px; border-radius: 10px; }
.caller { background: #eef2ff; margin-right: 15%; }
.agent { background: #ffffff; border: 1px solid #e5e5e5; margin-left: 15%; }
.role { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; color: #888; margin-bottom: 4px; }
.trace { font-size: 0.75rem; color: #999; margin-top: 6px; }
.badge { display: inline-block; background: #e5e5e5; border-radius: 4px; padding: 1px 6px; margin-right: 4px; }
"""


def render_transcript_html(state: SessionState) -> str:
    rows = []
    for t in state.transcript:
        cls = "caller" if t.role == "caller" else "agent"
        trace_html = ""
        if t.trace_event:
            te = t.trace_event
            trace_html = (
                f'<div class="trace">'
                f'<span class="badge">{html.escape(te["phase_before"])} → {html.escape(te["phase_after"])}</span>'
                f'<span class="badge">route: {html.escape(te["plan"]["route"])}</span>'
                f'<span class="badge">${te["cost"]["total_cost_usd"]:.4f}</span>'
                f"</div>"
            )
        rows.append(
            f'<div class="turn {cls}"><div class="role">{t.role}</div>'
            f"<div>{html.escape(t.text)}</div>{trace_html}</div>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Session {html.escape(state.session_id)}</title>
<style>{_STYLE}</style></head>
<body>
<h1>Session {html.escape(state.session_id)}</h1>
<div class="meta">SOP: {html.escape(state.sop_name)} · Phase: {html.escape(state.phase.value)} ·
Created: {html.escape(state.created_at)}</div>
{''.join(rows)}
</body></html>"""
