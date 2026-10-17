import { get } from "./net.js";
import { cellChip, h, kindChip, repoName, usd } from "./util.js";

// What cauce's runs cost in time and money, and what they got for it: the evidence
// for changing the router, the briefs and the defaults. Read only; no model.

const PHASES = [
  ["queued_s", "queued", "waiting in its lane before it ran"],
  ["plan_s", "plan", "classifying and choosing the cell"],
  ["setup_s", "setup", "worktree, waiting for the checkout, servers"],
  ["worker_s", "worker", "the workers' own time"],
  ["check_s", "check", "cauce running --verify"],
  ["verifier_s", "verifier", "the independent check"],
  ["wait_s", "wait", "waiting for a network that dropped"],
  ["other_s", "other", "the rest of the run: recording, committing, notes"],
];
const PERIOD_KEY = "cauce.analysis.days";
const SCOPE_KEY = "cauce.analysis.scope";

function kept(key, fallback, value) {
  try {
    if (value !== undefined) localStorage.setItem(key, value);
    return localStorage.getItem(key) ?? fallback;
  } catch { return value ?? fallback; }
}

const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const rate = (v) => h("td", { class: `num ${v === null || v === undefined ? "" : v >= 0.8 ? "rate-ok" : v < 0.5 ? "rate-bad" : "rate-warn"}` }, pct(v));
export function secs(v) {
  if (v === null || v === undefined) return "—";
  if (v < 120) return `${Math.round(v)}s`;
  if (v < 7200) return `${(v / 60).toFixed(1)}m`;
  return `${(v / 3600).toFixed(1)}h`;
}
const money = (v) => (v === null || v === undefined ? "—" : usd(v));
const num = (v) => h("td", { class: "num" }, v ?? "—");
const head = (...cols) => h("tr", {}, cols.map((c) => (Array.isArray(c) ? h("th", { class: "num" }, c[0]) : h("th", {}, c))));

function phaseBar(phases) {
  const total = Object.values(phases.seconds).reduce((a, b) => a + b, 0);
  if (!phases.runs || !total) return h("div", { class: "empty" }, "No run has its phases timed yet: runs record them from this version on.");
  const bar = h("div", { class: "phasebar", role: "img", "aria-label": "where the time of the timed runs went" });
  for (const [key, label] of PHASES) {
    const value = phases.seconds[key] || 0;
    if (!value) continue;
    const seg = h("span", { class: `ph-${label}`, title: `${label}: ${secs(value)} (${pct(phases.share[key])})` });
    seg.style.flexGrow = String(value);
    bar.append(seg);
  }
  return h("div", {},
    bar,
    h("div", { class: "legend" }, PHASES.filter(([key]) => phases.seconds[key]).map(([key, label, what]) =>
      h("span", { title: what }, h("i", { class: `ph-${label}` }), `${label} ${secs(phases.seconds[key])} · ${pct(phases.share[key])}`))),
    h("p", { class: "empty" }, `Over the ${phases.runs} run(s) whose phases were timed. Queued time is before a run starts; the rest is its wall time.`));
}

function summaryTable(rows, key, label, render) {
  if (!rows.length) return h("div", { class: "empty" }, "Nothing in this period.");
  return h("table", {},
    head(label, ["runs"], ["passed"], ["first try"], ["climbed"], ["p50 time"], ["p90 time"], ["per pass"], ["verified"]),
    rows.map((r) => h("tr", {},
      h("td", {}, render ? render(r[key]) : r[key]), num(r.runs), rate(r.pass_rate), rate(r.first_try_rate),
      num(pct(r.climbed_rate)), num(secs(r.wall_p50_s)), num(secs(r.wall_p90_s)), num(money(r.cost_per_pass_usd)),
      num(pct(r.verified_rate)))));
}

function download(report) {
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
  const link = h("a", { href: URL.createObjectURL(blob), download: `cauce-analysis-${report.days || "all"}d.json` });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function runList(rows, title) {
  if (!rows.length) return [];
  return [h("h2", { class: "section" }, title),
    h("table", {}, head("task", "kind", "status", ["time"], ["attempts"], ["cost"], "cells"),
      rows.map((r) => h("tr", {},
        h("td", {}, `#${r.task} `, h("span", { class: "muted" }, r.title)), h("td", {}, kindChip(r.kind)),
        h("td", {}, r.stop ? `${r.status} (${r.stop})` : r.status), num(secs(r.wall_s)), num(r.attempts),
        num(money(r.cost_usd)), h("td", {}, h("span", { class: "row" }, (r.cells || []).map((c) => cellChip(c)))))))];
}

export async function renderAnalysis(ctx) {
  const days = kept(PERIOD_KEY, "30");
  const scope = ctx.project ? kept(SCOPE_KEY, "project") : "all";
  const repo = scope === "project" && ctx.project ? `&repo=${encodeURIComponent(ctx.project)}` : "";
  const r = await get(`/api/analysis?days=${encodeURIComponent(days)}${repo}`);
  const s = r.summary;
  const period = h("select", { "aria-label": "period", onchange: (e) => { kept(PERIOD_KEY, "30", e.target.value); ctx.refresh(); } },
    [["7", "last 7 days"], ["30", "last 30 days"], ["90", "last 90 days"], ["0", "everything"]].map(([v, label]) =>
      h("option", { value: v, ...(v === days ? { selected: "" } : {}) }, label)));
  const where = h("select", { "aria-label": "scope", onchange: (e) => { kept(SCOPE_KEY, "project", e.target.value); ctx.refresh(); } },
    [...(ctx.project ? [["project", `this project (${repoName(ctx.project)})`]] : []), ["all", "every project"]].map(([v, label]) =>
      h("option", { value: v, ...(v === scope ? { selected: "" } : {}) }, label)));
  const turns = r.turns.turns;
  const delegations = r.turns.delegations;
  return h("div", {},
    h("div", { class: "toolbar" }, period, where,
      h("button", { type: "button", onclick: () => download(r), title: "everything on this screen, as JSON" }, "Export JSON")),
    h("p", { class: "empty" }, "Where cauce's time and money go and what they buy, read from its own records. "
      + "`cauce stats --runs --csv` writes one row per run for a spreadsheet."),
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("b", {}, s.runs ? pct(s.pass_rate) : "—"), h("span", {}, `${s.passed} of ${s.runs} runs passed (${s.tasks} tasks)`)),
      h("div", { class: "stat" }, h("b", {}, pct(s.first_try_rate)), h("span", {}, "passed at the first attempt")),
      h("div", { class: "stat" }, h("b", {}, secs(s.wall_p50_s)), h("span", {}, `median run · p90 ${secs(s.wall_p90_s)}`)),
      h("div", { class: "stat" }, h("b", {}, money(s.cost_per_pass_usd)), h("span", {}, `per pass · ${usd(s.cost_usd)} in all · checks ${usd(s.verifier_cost_usd)}`)),
      h("div", { class: "stat" }, h("b", {}, s.minutes_per_pass === null ? "—" : `${s.minutes_per_pass}m`), h("span", {}, `of runs per pass · ${s.hours}h in all`)),
      h("div", { class: "stat" }, h("b", {}, secs(s.queued_p50_s)), h("span", {}, "median wait in the queue"))),
    h("h2", { class: "section" }, "Where the time went"),
    phaseBar(r.phases),
    h("h2", { class: "section" }, "By kind"),
    summaryTable(r.by_kind, "kind", "kind", kindChip),
    h("h2", { class: "section" }, "The session's thread in the brief"),
    h("p", { class: "empty" }, "Runs a session sent, with the scribe's extract of that session in their brief and without it. "
      + "Observed, not a trial: the two groups differ in more than the brief."),
    r.context.length ? h("table", {}, head("brief", ["runs"], ["passed"], ["first try"], ["attempts"], ["p50 time"], ["per pass"]),
      r.context.map((c) => h("tr", {}, h("td", {}, c.group), num(c.runs), rate(c.pass_rate), rate(c.first_try_rate),
        num(c.attempts_avg), num(secs(c.wall_p50_s)), num(money(c.cost_per_pass_usd)))))
      : h("div", { class: "empty" }, "No run a session sent has been briefed yet."),
    h("h2", { class: "section" }, "By cell"),
    r.cells.length ? h("table", {}, head("cell", ["attempts"], ["passed"], ["p50 time"], ["p50 turns"], ["p50 cost"], ["cost"], ["checks"], ["cache read"], "failures"),
      r.cells.map((c) => h("tr", {}, h("td", {}, cellChip(c.cell)), num(c.attempts), rate(c.attempts ? c.passed / c.attempts : null),
        num(secs(c.wall_p50_s)), num(c.turns_p50), num(money(c.cost_p50_usd)), num(usd(c.cost_usd)),
        num(usd(c.verifier_cost_usd)), num(pct(c.cache_read_share)),
        h("td", {}, Object.entries(c.failures).map(([f, n]) => `${f} ×${n}`).join(", ") || "—"))))
      : h("div", { class: "empty" }, "No attempt in this period."),
    h("div", { class: "split" },
      h("div", {}, h("h2", { class: "section" }, "How runs stopped"),
        Object.keys(r.stops).length ? h("table", {}, head("stop", ["runs"]),
          Object.entries(r.stops).map(([k, v]) => h("tr", {}, h("td", {}, k), num(v)))) : h("div", { class: "empty" }, "Every run passed.")),
      h("div", {}, h("h2", { class: "section" }, "Moves after a failed attempt"),
        Object.keys(r.moves).length ? h("table", {}, head("move", ["times"]),
          Object.entries(r.moves).map(([k, v]) => h("tr", {}, h("td", {}, k), num(v)))) : h("div", { class: "empty" }, "No run had to move."))),
    h("h2", { class: "section" }, "The session's own turns"),
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("b", {}, turns.count), h("span", {}, `turns · ${turns.interrupted} interrupted`)),
      h("div", { class: "stat" }, h("b", {}, secs(turns.seconds_p50)), h("span", {}, `median turn · p90 ${secs(turns.seconds_p90)}`)),
      h("div", { class: "stat" }, h("b", {}, usd(turns.usd)), h("span", {}, `the turns, at API rates · p50 ${money(turns.usd_p50)}`)),
      h("div", { class: "stat" }, h("b", {}, delegations.count), h("span", {}, `subagent delegations · p50 ${secs(delegations.seconds_p50)}`))),
    r.by_workflow.length ? [h("h2", { class: "section" }, "By workflow"), summaryTable(r.by_workflow, "workflow", "workflow")] : [],
    h("h2", { class: "section" }, "By day"),
    r.daily.length ? h("table", {}, head("day", ["turns"], ["runs"], ["passed"], ["hours"], ["cost"]),
      r.daily.map((d) => h("tr", {}, h("td", {}, d.day), num(d.turns), num(d.runs), num(d.passed), num(d.hours), num(usd(d.cost_usd)))))
      : h("div", { class: "empty" }, "Nothing in this period."),
    runList(r.slowest || [], "The slowest runs"),
    runList(r.costliest || [], "The costliest runs"));
}
