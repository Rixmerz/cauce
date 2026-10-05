import { get } from "./net.js";
import { h, usd } from "./util.js";

const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : "—");
const cells = (counts) => Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${c} ×${n}`).join(", ") || "—";

export async function renderRouting() {
  const rows = await get("/api/routing");
  return h("div", {},
    h("p", { class: "empty" }, "Where each kind of task started, where it passed, and how often it had to climb. "
      + "The ladders are code; this is the evidence for changing them. Pinned runs are counted but never teach the router."),
    h("table", {},
      h("tr", {}, h("th", {}, "kind"), h("th", {}, "ladder"), h("th", { class: "num" }, "tasks"), h("th", { class: "num" }, "passed"),
        h("th", { class: "num" }, "climbed"), h("th", {}, "passed at"), h("th", { class: "num" }, "avg cost")),
      rows.map((r) => h("tr", {},
        h("td", {}, h("span", { class: "chip" }, r.kind)), h("td", {}, r.ladder.join(" → ")),
        h("td", { class: "num" }, r.tasks), h("td", { class: "num" }, pct(r.passed, r.tasks)),
        h("td", { class: "num" }, pct(r.climbed, r.tasks)), h("td", {}, cells(r.passes)),
        h("td", { class: "num" }, r.tasks ? usd(r.cost_usd / r.tasks) : "—")))));
}
