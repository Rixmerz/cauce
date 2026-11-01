import { get } from "./net.js";
import { cellChip, h, kindChip, repoName, secs, usd } from "./util.js";

// How locks evolves and why: each metric's trend over the daily snapshots, every change of a
// standard with its rule, evidence and verdict, and the standards as they stand. Read only:
// a GET never calibrates or snapshots; no model decided any of it.

const PERIOD_KEY = "locks.evolution.days";
const SCOPE_KEY = "locks.evolution.scope";

function kept(key, fallback, value) {
  try {
    if (value !== undefined) localStorage.setItem(key, value);
    return localStorage.getItem(key) ?? fallback;
  } catch { return value ?? fallback; }
}

const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const num = (v) => h("td", { class: "num" }, v ?? "—");
const head = (...cols) => h("tr", {}, cols.map((c) => (Array.isArray(c) ? h("th", { class: "num" }, c[0]) : h("th", {}, c))));

// A word and a tone, never the tone alone.
const SAID = { better: ["ok", "✓"], worse: ["bad", "✕"], same: ["mute", "="], "one snapshot": ["mute", "·"],
  "not measured": ["mute", "–"] };
function said(word) {
  const [tone, icon] = SAID[word] || ["mute", "•"];
  return h("span", { class: `badge t-${tone}` }, h("span", { class: "icon", "aria-hidden": "true" }, icon), word);
}
const VERDICT = { kept: ["ok", "✓", "kept"], reverted: ["bad", "↺", "reverted"] };
function verdict(change) {
  const [tone, icon, word] = change.reverts ? ["warn", "↺", "a revert"]
    : change.verdict ? VERDICT[change.verdict.verdict] || ["mute", "•", change.verdict.verdict]
      : ["queue", "○", "not judged yet"];
  return h("span", { class: `badge t-${tone}` }, h("span", { class: "icon", "aria-hidden": "true" }, icon), word);
}

const METRICS = [
  ["velocity", "velocity (points/h)", (v) => (v === null || v === undefined ? "—" : v.toFixed(1))],
  ["cost_per_point", "cost per point", (v) => (v === null || v === undefined ? "—" : usd(v))],
  ["time_per_point_s", "time per point", secs],
  ["pass_first", "pass-first", pct],
  ["climbed", "climbs", pct],
];

function trendTable(trends) {
  if (!trends.length) {
    return h("div", { class: "empty" }, "No snapshot in this period yet: the first task to end on a new day takes one, "
      + "or `locks evolution --snapshot` takes one now.");
  }
  return h("table", {},
    head("kind", "project", ["snapshots"], ["points done"], ["estimated"], ["realized"], ...METRICS.map(([, label]) => label)),
    trends.map((t) => h("tr", {},
      h("td", {}, kindChip(t.kind)), h("td", {}, repoName(t.repo)), num(t.snapshots), num(t.points_done),
      num(t.points_estimated), num(t.points_realized),
      METRICS.map(([key, , show]) => {
        const m = t.metrics[key];
        return h("td", {}, h("div", {}, m.said === "one snapshot" ? show(m.to) : `${show(m.from)} → ${show(m.to)}`),
          said(m.said));
      }))));
}

function changeList(changes) {
  if (!changes.length) return h("div", { class: "empty" }, "No standard changed in this period.");
  return h("ul", { class: "timeline" }, changes.map((c) => h("li", {},
    h("div", { class: "row" }, h("b", {}, `#${c.id}`), verdict(c), kindChip(c.kind),
      c.field === "start_cell" && c.new_value ? cellChip(c.new_value) : null,
      c.field === "skip" ? h("span", { class: "muted" }, c.new_value ? `skips ${c.new_value.split(">")[1]} from ${c.new_value.split(">")[0]}` : "skip reverted") : null,
      h("span", { class: "muted" }, `locks ${c.version}`)),
    h("div", { class: "text" }, c.line),
    h("div", { class: "meta" }, `rule: ${c.rule}`))));
}

function standardTable(rows) {
  if (!rows.length) return h("div", { class: "empty" }, "No standard measured yet: a calibration runs after every few tasks end in a repository.");
  return h("table", {},
    head("kind", "project", ["points"], "starts at", ["turns"], ["tasks"], ["pass-first"], ["cost per point"], ["time per point"], "first tries by cell"),
    rows.map((s) => h("tr", {},
      h("td", {}, kindChip(s.kind)), h("td", {}, repoName(s.repo)), num(s.band),
      h("td", {}, s.start_cell ? cellChip(s.start_cell) : "locks' rules"), num(s.start_turns ?? "the kind's"),
      num(s.measured.tasks), num(pct(s.measured.pass_first)),
      num(s.measured.cost_per_point === null || s.measured.cost_per_point === undefined ? "—" : usd(s.measured.cost_per_point)),
      num(secs(s.measured.time_per_point_s)),
      h("td", {}, Object.entries(s.measured.cells || {}).map(([cell, [passed, tried]]) => `${cell} ${passed}/${tried}`).join(", ") || "—"))));
}

export async function renderEvolution(ctx) {
  const days = kept(PERIOD_KEY, "30");
  const scope = ctx.project ? kept(SCOPE_KEY, "project") : "all";
  const repo = scope === "project" && ctx.project ? `&repo=${encodeURIComponent(ctx.project)}` : "";
  const r = await get(`/api/evolution?days=${encodeURIComponent(days)}${repo}`);
  const period = h("select", { "aria-label": "period", onchange: (e) => { kept(PERIOD_KEY, "30", e.target.value); ctx.refresh(); } },
    [["7", "last 7 days"], ["30", "last 30 days"], ["90", "last 90 days"], ["0", "everything"]].map(([v, label]) =>
      h("option", { value: v, ...(v === days ? { selected: "" } : {}) }, label)));
  const where = h("select", { "aria-label": "scope", onchange: (e) => { kept(SCOPE_KEY, "project", e.target.value); ctx.refresh(); } },
    [...(ctx.project ? [["project", `this project (${repoName(ctx.project)})`]] : []), ["all", "every project"]].map(([v, label]) =>
      h("option", { value: v, ...(v === scope ? { selected: "" } : {}) }, label)));
  return h("div", {},
    h("div", { class: "toolbar" }, period, where),
    h("p", { class: "empty" }, "How locks evolves and why, read from its own records: every task's points, the standards "
      + "measured per kind and size, and each calibration with its rule and verdict. No model takes these decisions; "
      + "`locks evolution` prints the same."),
    h("h2", { class: "section" }, "Trends"),
    h("p", { class: "empty" }, "Each metric from the older half of the period's daily snapshots to the newer."),
    trendTable(r.trends),
    h("h2", { class: "section" }, "Calibration changes"),
    changeList(r.changes),
    h("h2", { class: "section" }, "Standards now"),
    standardTable(r.standards));
}
