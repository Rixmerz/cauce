import { get } from "./net.js";
import { h, usd } from "./util.js";

const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : "—");
const cells = (counts) => Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c} ×${n}`).join(", ") || "—";

// The period, kept per viewer: rules change, and last week's failures of a rule
// fixed since are not evidence about it any more.
const PERIOD_KEY = "cauce.routing.days";

function period() {
  try { return localStorage.getItem(PERIOD_KEY) ?? "7"; } catch { return "7"; }
}

export async function renderRouting(ctx) {
  const days = period();
  const rows = await get(`/api/routing?days=${encodeURIComponent(days)}`);
  const climbs = rows.flatMap((r) => (r.climbs || []).map((c) => ({ kind: r.kind, c })));
  const picker = h("select", { "aria-label": "period", onchange: (e) => {
    try { localStorage.setItem(PERIOD_KEY, e.target.value); } catch { /* the choice just is not kept */ }
    ctx?.refresh?.();
  } }, [["7", "last 7 days"], ["30", "last 30 days"], ["0", "everything"]].map(([v, label]) =>
    h("option", { value: v, ...(v === days ? { selected: "" } : {}) }, label)));
  return h("div", {},
    h("div", { class: "toolbar" }, picker),
    h("p", { class: "empty" }, "Where each kind of task started, where it passed, and how often it had to climb. "
      + "The ladders are code; this is the evidence for changing them. Pinned runs are counted but never teach the router."),
    h("table", {},
      h("tr", {}, h("th", {}, "kind"), h("th", {}, "ladder"), h("th", { class: "num" }, "tasks"), h("th", { class: "num" }, "passed"),
        h("th", { class: "num" }, "climbed"), h("th", {}, "passed at"), h("th", { class: "num" }, "avg cost")),
      rows.map((r) => h("tr", {},
        h("td", {}, h("span", { class: "chip" }, r.kind)), h("td", {}, r.ladder.join(" → ")),
        h("td", { class: "num" }, r.tasks), h("td", { class: "num" }, pct(r.passed, r.tasks)),
        h("td", { class: "num" }, pct(r.climbed, r.tasks)), h("td", {}, cells(r.passes)),
        h("td", { class: "num" }, r.tasks ? usd(r.cost_usd / r.tasks) : "—")))),
    h("h2", { class: "spaced" }, "How each ladder was climbed"),
    h("p", { class: "empty" }, "Every move after a failed attempt: the cell it left, the failure behind it, where the next "
      + "attempt ran and how often that one passed. A rung that is always climbed past is a start to raise; a move "
      + "whose next attempt rarely passes is a rung that does not help."),
    climbs.length ? h("table", {},
      h("tr", {}, h("th", {}, "kind"), h("th", {}, "from"), h("th", {}, "after"), h("th", {}, "move"), h("th", {}, "to"),
        h("th", { class: "num" }, "times"), h("th", { class: "num" }, "next passed")),
      climbs.map(({ kind, c }) => h("tr", {},
        h("td", {}, h("span", { class: "chip" }, kind)), h("td", {}, c.from), h("td", {}, c.after), h("td", {}, c.move),
        h("td", {}, c.to || "— stopped"), h("td", { class: "num" }, c.times),
        h("td", { class: "num" }, c.then_passed === null ? "—" : pct(c.then_passed, c.times)))))
      : h("div", { class: "empty" }, "No task has climbed yet."));
}
