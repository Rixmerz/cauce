import { get, post } from "./net.js";
import { h, repoName, usd, when } from "./util.js";

const drawer = () => document.getElementById("drawer");

function close() {
  drawer().hidden = true;
  drawer().replaceChildren();
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

function attempt(a) {
  return h("li", { class: a.passed ? "pass" : "fail" },
    h("div", { class: "row" },
      h("b", {}, `#${a.seq} ${a.cell}`),
      h("span", {}, a.passed ? "pass" : (a.failure || "fail")),
      h("span", {}, `${a.turns} turns`), h("span", {}, usd(a.cost_usd))),
    a.summary ? h("div", {}, a.summary) : null,
    a.move ? h("div", { class: "move" }, `→ ${a.move}: ${a.move_reason}`) : null,
    a.changed_paths.length ? h("div", { class: "meta" }, `changed: ${a.changed_paths.join(", ")}`) : null,
    a.capabilities.length ? h("div", { class: "meta" }, `capabilities: ${a.capabilities.join(", ")}`) : null);
}

export async function openTask(id, quiet = false) {
  const d = await get(`/api/tasks/${id}`);
  const t = d.task;
  const el = drawer();
  el.dataset.task = String(id);
  const actions = [];
  if (t.status === "running" || t.status === "queued") {
    actions.push(h("button", { onclick: async () => { await post(`/api/tasks/${id}/cancel`); openTask(id); } }, "Cancel"));
  }
  if (d.branch) {
    const cmd = `git -C ${t.cwd} diff HEAD...${d.branch}`;
    actions.push(h("button", { onclick: () => navigator.clipboard?.writeText(cmd) }, "Copy diff command"));
  }
  el.replaceChildren(...[
    h("header", {},
      h("div", {},
        h("h1", {}, `#${t.id} ${t.title}`),
        h("div", { class: "meta row" },
          h("span", { class: `status-${t.status}` }, t.status), h("span", {}, repoName(t.repo)),
          t.kind ? h("span", { class: "chip" }, t.kind) : null, h("span", {}, usd(t.cost_usd)),
          t.pinned ? h("span", { class: "chip" }, "pinned") : null, h("span", {}, when(t.created_at)))),
      h("div", { class: "row" }, ...actions, h("button", { onclick: close, "aria-label": "close" }, "✕"))),
    h("div", { class: "section" }, h("h2", {}, "Task"), h("div", { class: "text" }, t.body)),
    t.parallel_reason ? h("div", { class: "section" }, h("h2", {}, "Dispatch"),
      h("div", {}, t.parallel ? "May run beside the work going in this repository: " : "Waits for the work ahead of it: ",
        t.parallel_reason)) : null,
    d.plan ? h("div", { class: "section" }, h("h2", {}, "Plan"),
      h("div", {}, `${(d.plan.ladder || []).join(" → ")}  ·  start ${d.plan.start}`),
      h("ul", {}, (d.plan.reasons || []).map((r) => h("li", {}, r))),
      (d.plan.neighbours || []).map((n) => h("div", { class: "meta" }, n))) : null,
    d.attempts.length ? h("div", { class: "section" }, h("h2", {}, "Attempts"), h("ul", { class: "timeline" }, d.attempts.map(attempt))) : null,
    d.branch || d.impact.length ? h("div", { class: "section" }, h("h2", {}, "Result"),
      d.branch ? h("div", {}, "branch ", h("span", { class: "chip" }, d.branch)) : null,
      d.impact.map((line) => h("div", { class: "meta" }, line))) : null,
    d.dead_ends.length ? h("div", { class: "section" }, h("h2", {}, "Dead ends it was shown"),
      d.dead_ends.map((x) => h("div", { class: "fix" },
        h("span", { class: `tag ${x.disproved_on ? "disproved" : "failed"}` }, x.disproved_on ? "disproved" : "failed"),
        `${x.problem}: ${x.tried}`, x.worked_instead ? ` — worked instead: ${x.worked_instead}` : ""))) : null,
    d.children.length ? h("div", { class: "section" }, h("h2", {}, "Delegations"),
      d.children.map((c) => h("div", { class: "fix" }, h("span", { class: `tag status-${c.status}` }, c.status), c.title))) : null,
    h("div", { class: "section" }, h("h2", {}, "Messages"),
      d.messages.map((m) => h("div", {}, h("div", { class: "meta" }, `${m.role} · ${when(m.ts)}`), h("div", { class: "text" }, m.text)))),
  ].filter(Boolean));
  if (!quiet) el.scrollTop = 0;
  el.hidden = false;
}
