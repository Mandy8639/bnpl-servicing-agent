"use strict";

// Every number on this page comes from the API, which reads the ledger and the
// committed eval reports. Nothing here is hard-coded.

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1).replace(/\.0$/, "")}%`);
const money = (v) => `$${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const when = (iso) => (iso ? iso.replace("T", " ").slice(0, 16) + " UTC" : "—");
const label = (s) => String(s).replaceAll("_", " ");

const state = { filter: "all", cases: [], actions: [], minSample: 0 };

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `${response.status} ${response.statusText}`);
  return body;
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("show"), 3200);
}

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
};

// --- Header: agent state and the decision tape --------------------------------------

function renderStatus(digest) {
  const agent = digest.paused
    ? `<span class="pill"><span class="dot paused"></span><b>Paused</b> ${esc(label(digest.pause_reason))}</span>`
    : `<span class="pill"><span class="dot"></span><b>Agent running</b></span>`;
  const fresh = `<span class="pill"><span class="dot ${digest.data_fresh ? "" : "stale"}"></span>Ledger <b>${digest.data_age_hours}h</b> old</span>`;
  $("status").innerHTML = agent + fresh;
}

function renderTape(decisions) {
  const tape = $("tape");
  if (!decisions.length) {
    tape.innerHTML = `<span class="empty">No decisions yet. Run <code>bnpl run</code> to work cases.</span>`;
    $("tape-legend").innerHTML = "";
    return;
  }
  tape.innerHTML = decisions
    .map((d) => {
      const kind = d.actor === "agent" ? d.guardrail_verdict : "human";
      const who = d.actor === "agent" ? `agent ${d.guardrail_verdict}` : `${d.actor}`;
      return `<span class="${kind}" title="${esc(`${d.case_id} · ${d.proposed_action} · ${who}`)}"></span>`;
    })
    .join("");
  const counts = decisions.reduce((acc, d) => {
    const kind = d.actor === "agent" ? d.guardrail_verdict : "human";
    acc[kind] = (acc[kind] || 0) + 1;
    return acc;
  }, {});
  const keys = [["ALLOW", "ran unattended", "var(--allow)"], ["HOLD", "held for a human", "var(--hold)"],
    ["BLOCK", "blocked", "var(--block)"], ["human", "human decisions", "var(--accent)"]];
  $("tape-legend").innerHTML =
    `<span>${decisions.length} decisions in the audit log</span>` +
    keys.map(([k, text, color]) => `<span><i style="background:${color}"></i>${counts[k] || 0} ${text}</span>`).join("");
}

// --- Review queue -------------------------------------------------------------------

function renderQueue(queue) {
  $("queue-count").textContent = `${queue.length} held`;
  if (!queue.length) {
    $("queue").innerHTML = `<p class="empty-state">Nothing is waiting on a human. Held cases appear here with the agent's proposal and the reason it was stopped.</p>`;
    return;
  }
  const choices = state.actions.filter((a) => a !== "hold_for_review");
  $("queue").innerHTML = queue
    .map((h) => {
      const abstained = h.proposed_action === "hold_for_review";
      const select = abstained
        ? `<select name="chosen_action" aria-label="Action to take">
             <option value="">Choose the action to take…</option>
             ${choices.map((a) => `<option value="${a}">${label(a)}</option>`).join("")}
           </select>`
        : "";
      return `
      <article class="held" data-case="${esc(h.case_id)}">
        <div class="held-top">
          <button type="button" class="held-id" data-open="${esc(h.case_id)}">${esc(h.case_id)}</button>
          <span class="held-due">decide by ${esc(when(h.decide_by))}</span>
        </div>
        <div class="held-action">${esc(abstained ? "Agent abstained" : label(h.proposed_action))}</div>
        <div class="held-meta">${esc(h.merchant_name)} · ${esc(h.sector)} · ${money(h.amount_overdue)} overdue · ${h.days_overdue} days
          · <span class="badge HOLD">${esc(h.reason_code)}</span></div>
        <p class="held-why">${esc(h.rationale)}</p>
        <form>
          <textarea name="reason" placeholder="Your reason (saved to the audit log)" aria-label="Reason"></textarea>
          <div class="row">
            ${select}
            <button type="submit" class="btn primary" value="approve">${abstained ? "Take this action" : "Approve"}</button>
            <button type="submit" class="btn" value="reject">Reject</button>
          </div>
        </form>
      </article>`;
    })
    .join("");
}

async function submitReview(event) {
  event.preventDefault();
  const form = event.target;
  const card = form.closest(".held");
  const decision = event.submitter?.value || "approve";
  const actor = $("actor").value.trim();
  const reason = form.reason.value.trim();
  if (!actor) { $("actor").focus(); toast("Add your name under “Reviewing as” first."); return; }
  if (!reason) { form.reason.focus(); toast("Add a reason. It goes in the audit log."); return; }
  // Read the select by name: `form.action` is the form's own action attribute.
  const choice = form.elements.namedItem("chosen_action");
  const action = choice ? choice.value || null : null;
  if (decision === "approve" && choice && !action) { choice.focus(); toast("Choose the action to take."); return; }
  form.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await api(`/cases/${encodeURIComponent(card.dataset.case)}/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ decision, actor, reason, action }),
    });
    toast(`${card.dataset.case} ${decision === "approve" ? "approved" : "rejected"} by ${actor}.`);
    await refreshLedger();
  } catch (error) {
    toast(`Not saved: ${error.message}`);
    form.querySelectorAll("button").forEach((b) => (b.disabled = false));
  }
}

// --- Controls ----------------------------------------------------------------------

const RAILS = {
  auto_action_rate: ["Auto-action rate", true],
  offer_plan_rate: ["Payment-plan rate", true],
  consecutive_blocks: ["Blocks in a row", false],
  data_age_hours: ["Ledger age (hours)", false],
};

function renderControls(digest) {
  $("window-note").textContent = `window: ${digest.window_size} decisions`;
  $("rails").innerHTML = digest.thresholds
    .map((t) => {
      const [name, isRate] = RAILS[t.name] || [label(t.name), false];
      const scale = isRate ? 1 : Math.max(t.limit * 1.5, t.value || 0);
      const fill = Math.min(100, ((t.value || 0) / scale) * 100);
      const gate = Math.min(100, (t.limit / scale) * 100);
      const unsampled = isRate && digest.window_size < state.minSample;
      const show = (v) => (v === null ? "—" : isRate ? pct(v) : Number(v).toLocaleString("en-US"));
      return `
      <div class="rail">
        <div class="rail-label"><span>${name}</span><span>${show(t.value)} / ${show(t.limit)}</span></div>
        <div class="rail-track" role="meter" aria-label="${name}" aria-valuenow="${t.value ?? 0}" aria-valuemax="${t.limit}">
          <div class="rail-fill ${t.breached ? "breached" : unsampled ? "unsampled" : ""}" style="width:${fill}%"></div>
          <div class="rail-gate" style="left:${gate}%"></div>
        </div>
        ${t.breached ? `<div class="rail-note" style="color:var(--block)">Over the limit: the agent pauses.</div>` : ""}
        ${unsampled ? `<div class="rail-note">Counts once ${state.minSample} decisions exist since the last clear.</div>` : ""}
      </div>`;
    })
    .join("");
  const kinds = Object.entries(digest.exceptions_by_kind);
  $("inventory").innerHTML = `
    <div class="kv">
      <span>Total entries</span><span>${digest.exceptions_total}</span>
      ${kinds.map(([k, n]) => `<span>${esc(k)}</span><span>${n}</span>`).join("")}
      <span>Open reviews</span><span>${digest.open_reviews}</span>
      <span>Hold rate</span><span>${pct(digest.hold_rate)}</span>
    </div>`;
}

// --- Eval --------------------------------------------------------------------------

function renderEval({ report, history }) {
  const m = report.metrics;
  $("eval-source").textContent = `${report.file} · ${report.model} · prompt ${report.prompt_version || "v1"}`;
  const tiles = [
    ["Action accuracy", pct(m.action_accuracy), `${m.action_correct}/${m.model_decided_cases} cases`],
    ["False-auto", String(m.false_auto_count), `must be 0 · rate ${pct(m.false_auto_rate)}`, m.false_auto_count === 0 ? "gate" : "gate fail"],
    ["Hold accuracy", pct(m.hold_accuracy), `${m.hold_cases} ambiguous cases`],
    ["Pause detection", pct(m.pause_detection_rate), `${m.control_cases} control cases`],
    ["Guardrail blocks", pct(m.guardrail_block_rate), `${m.out_of_mandate_proposals} out-of-mandate`],
    ["LLM cost", `$${m.llm_cost_usd.toFixed(2)}`, `${report.mode} mode · ${m.runtime_seconds}s`],
  ];
  $("eval-tiles").innerHTML = tiles
    .map(([k, v, n, cls]) => `<div class="tile ${cls || ""}"><div class="k">${k}</div><div class="v">${esc(v)}</div><div class="n">${esc(n)}</div></div>`)
    .join("");
  const names = { nudge: "Nudge", offer_plan: "Payment plan", escalate: "Collections", ambiguous: "Ambiguous → hold", no_action: "No action", control: "Pause / control" };
  $("eval-categories").innerHTML =
    `<thead><tr><th>Category</th><th>Correct</th><th>Accuracy</th></tr></thead><tbody>` +
    Object.entries(m.by_category)
      .map(([k, v]) => `<tr><td>${names[k] || esc(k)}</td><td class="num">${v.correct}/${v.cases}</td><td class="num">${pct(v.accuracy)}</td></tr>`)
      .join("") + `</tbody>`;
  $("eval-history").innerHTML =
    `<thead><tr><th>Report</th><th>Prompt</th><th>Accuracy</th><th>False-auto</th><th>Gate</th></tr></thead><tbody>` +
    history
      .map((h) => `<tr><td class="mono">${esc(h.file)}</td><td>${esc(h.prompt_version)}</td><td class="num">${pct(h.action_accuracy)}</td>
        <td class="num">${h.false_auto_count}</td><td><span class="badge ${h.gate_passed ? "ALLOW" : "BLOCK"}">${h.gate_passed ? "pass" : "fail"}</span></td></tr>`)
      .join("") + `</tbody>`;
}

// --- Cases -------------------------------------------------------------------------

const STATUSES = ["all", "held", "executed", "blocked", "approved", "rejected", "open"];

function renderFilters() {
  const counts = state.cases.reduce((acc, c) => ((acc[c.status] = (acc[c.status] || 0) + 1), acc), {});
  $("filters").innerHTML = STATUSES.filter((s) => s === "all" || counts[s])
    .map((s) => `<button type="button" class="chip" data-filter="${s}" aria-pressed="${state.filter === s}">${s} ${s === "all" ? state.cases.length : counts[s]}</button>`)
    .join("");
}

function renderCases() {
  const rows = state.cases.filter((c) => state.filter === "all" || c.status === state.filter);
  $("cases").innerHTML =
    `<thead><tr><th>Case</th><th>Status</th><th>Merchant</th><th>Overdue</th><th>Days</th><th>Signals</th><th>Agent proposal</th></tr></thead><tbody>` +
    rows
      .map((c) => {
        const flags = c.risk_flags.map((f) => `<span class="badge ${f === "fraud_flag" ? "risk" : ""}">${esc(label(f))}</span>`);
        if (c.merchant_under_review) flags.push(`<span class="badge HOLD">merchant under review</span>`);
        return `<tr data-open="${esc(c.case_id)}" tabindex="0">
          <td class="mono">${esc(c.case_id)}</td>
          <td><span class="badge ${esc(c.status)}">${esc(c.status)}</span></td>
          <td>${esc(c.merchant_name)}<div class="held-meta">${esc(c.sector)} · ${esc(label(c.case_type))}</div></td>
          <td class="num">${money(c.amount_overdue)}</td>
          <td class="num">${c.days_overdue}</td>
          <td><div class="flags">${flags.join("") || "—"}</div></td>
          <td>${c.proposed_action ? `${esc(label(c.proposed_action))} <span class="badge ${esc(c.verdict)}">${esc(c.verdict)}</span>` : "—"}</td>
        </tr>`;
      })
      .join("") + `</tbody>`;
}

async function openCase(caseId) {
  const body = await api(`/cases/${encodeURIComponent(caseId)}`);
  const c = body.case;
  $("detail-title").textContent = `${c.case_id} · ${c.merchant_name}`;
  $("detail-body").innerHTML = `
    <p class="held-meta">${esc(label(c.case_type))} · ${money(c.amount_overdue)} overdue · ${c.days_overdue} days ·
      loan ${money(c.principal)} over ${c.term_months} months at ${(c.apr * 100).toFixed(2)}% · <span class="badge ${esc(body.status)}">${esc(body.status)}</span></p>
    <h3>Communication history</h3>
    <ul class="thread">${c.comms_history.map((m) => `<li class="${esc(m.direction)}"><div class="when">${esc(m.date)} · ${esc(m.channel)} · ${esc(m.direction)}</div>${esc(m.text)}</li>`).join("")}</ul>
    <h3>Payments</h3>
    <div class="table-scroll"><table class="grid"><thead><tr><th>Due</th><th>Paid</th><th>Amount</th><th>Status</th></tr></thead><tbody>
      ${body.payments.map((p) => `<tr><td class="mono">${esc(p.due_date)}</td><td class="mono">${esc(p.paid_date || "—")}</td><td class="num">${money(p.amount)}</td><td><span class="badge">${esc(label(p.status))}</span></td></tr>`).join("")}
    </tbody></table></div>
    <h3>Audit trail</h3>
    ${body.decisions.length ? `<div class="table-scroll"><table class="grid"><thead><tr><th>When</th><th>Who</th><th>Proposed</th><th>Verdict</th><th>Ran</th><th>Reason</th></tr></thead><tbody>
      ${body.decisions.map((d) => `<tr><td class="mono">${esc(when(d.timestamp))}</td><td>${esc(d.actor)}</td><td>${esc(label(d.proposed_action))}</td>
        <td><span class="badge ${esc(d.guardrail_verdict)}">${esc(d.guardrail_verdict)}</span></td><td>${esc(d.final_action ? label(d.final_action) : "nothing")}</td><td>${esc(d.reason)}</td></tr>`).join("")}
    </tbody></table></div>` : `<p class="empty-state">The agent has not worked this case yet.</p>`}`;
  $("detail").showModal();
}

// --- Mandate -----------------------------------------------------------------------

function renderMandate(mandate) {
  state.actions = mandate.actions.map((a) => a.action);
  state.minSample = mandate.thresholds.control_min_sample;
  $("owner").textContent = `escalation owner: ${mandate.escalation_owner} · ${mandate.review_sla_hours}h to decide`;
  $("mandate").innerHTML =
    `<thead><tr><th>Action</th><th>Meaning</th><th>Risk</th><th>Handling</th></tr></thead><tbody>` +
    mandate.actions.map((a) => `<tr><td class="mono">${esc(a.action)}</td><td>${esc(a.meaning)}</td><td>${esc(a.risk)}</td><td>${esc(a.handling)}</td></tr>`).join("") +
    `</tbody>`;
}

// --- Loading -----------------------------------------------------------------------

async function refreshLedger() {
  const [digest, queue, cases, audit] = await Promise.all([
    api("/controls/digest"), api("/reviews"), api("/cases"), api("/audit"),
  ]);
  state.cases = cases;
  renderStatus(digest);
  renderTape(audit);
  renderQueue(queue);
  renderControls(digest);
  renderFilters();
  renderCases();
}

async function init() {
  $("actor").value = store.get("bnpl.actor") || "";
  $("actor").addEventListener("change", (e) => store.set("bnpl.actor", e.target.value.trim()));
  $("queue").addEventListener("submit", submitReview);
  $("filters").addEventListener("click", (e) => {
    const chip = e.target.closest("[data-filter]");
    if (!chip) return;
    state.filter = chip.dataset.filter;
    renderFilters();
    renderCases();
  });
  document.addEventListener("click", (e) => {
    const target = e.target.closest("[data-open]");
    if (target && !e.target.closest("form")) openCase(target.dataset.open).catch((err) => toast(err.message));
  });
  $("cases").addEventListener("keydown", (e) => {
    const row = e.target.closest("tr[data-open]");
    if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openCase(row.dataset.open); }
  });
  $("detail-close").addEventListener("click", () => $("detail").close());

  try {
    renderMandate(await api("/mandate"));
    await refreshLedger();
  } catch (error) {
    $("queue").innerHTML = `<p class="empty-state">Could not load the ledger: ${esc(error.message)}</p>`;
  }
  try {
    renderEval(await api("/evals/latest"));
  } catch (error) {
    $("eval-tiles").innerHTML = `<p class="empty-state">No committed eval report found. Run <code>bnpl evals run</code>.</p>`;
  }
}

init();
