import { get, post } from "./net.js";
import { stopSection } from "./stop.js";
import { h, repoName, usd, when } from "./util.js";

const drawer = () => document.getElementById("drawer");

function close() {
  drawer().hidden = true;
  drawer().replaceChildren();
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

// Who wrote each message, in words: a person, the main session that sent the work, or cauce.
const AUTHOR = { user: "you", orchestrator: "main session (orchestrator)", assistant: "main session's answer",
  worker: "cauce report" };

const DIAL = { effort: "more effort, same model", model: "a different model", turns: "more turns, same cell",
  retry: "the same cell again", stop: "stopped" };

// The move after a failed attempt: where it went, along which dial, and the evidence, in order.
function climb(c, move) {
  if (!c) return null;
  const head = c.to
    ? [h("b", {}, `${DIAL[c.axis] || c.axis}: `), h("span", { class: "chip" }, c.from), " → ", h("span", { class: "chip" }, c.to)]
    : [h("b", {}, `${move}: `), "no further attempt"];
  return h("div", { class: `climb climb-${c.axis}` },
    h("div", { class: "row" }, ...head,
      c.turns_to && c.turns_to !== c.turns_from ? h("span", { class: "meta" }, `turns ${c.turns_from} → ${c.turns_to}`) : null,
      c.budget_left !== undefined && c.budget_left !== null ? h("span", { class: "meta" }, `$${Number(c.budget_left).toFixed(2)} left`) : null),
    c.because.length ? h("ol", {}, c.because.map((line) => h("li", {}, line))) : null,
    c.skipped.length ? h("div", { class: "meta" }, "skipped: ", c.skipped.join("; ")) : null,
    c.recovered ? h("div", { class: "meta" }, "from before cauce kept the evidence of each move: the reason is all it recorded") : null);
}

// The ladder, cheapest first, with each attempt where it ran: the shape of the climb at a glance.
function ladderPath(plan, attempts) {
  const ran = new Map();
  for (const a of attempts) {
    if (!ran.has(a.cell)) ran.set(a.cell, []);
    ran.get(a.cell).push(a);
  }
  const cells = [...(plan.ladder || [])];
  for (const cell of ran.keys()) if (!cells.includes(cell)) cells.push(cell);  // pinned or learned off the ladder
  return h("div", { class: "ladder" }, cells.map((cell, i) => [
    i ? h("span", { class: "rung-gap" }, "→") : null,
    h("span", { class: ["rung", ran.has(cell) ? (ran.get(cell).some((a) => a.passed) ? "rung-pass" : "rung-fail") : "",
      cell === plan.start ? "rung-start" : ""].join(" "), title: cell === plan.start ? "where it started" : "" },
      cell, ...(ran.get(cell) || []).map((a) => h("sup", {}, ` #${a.seq}`)))]));
}

function attempt(a) {
  return h("li", { class: a.passed ? "pass" : "fail" },
    h("div", { class: "row" },
      h("b", {}, `#${a.seq} ${a.cell}`),
      a.served_model ? h("span", { class: "chip", title: "the model that served it, as the CLI reported" }, a.served_model) : null,
      h("span", {}, a.passed ? "pass" : (a.failure || "fail")),
      h("span", {}, `${a.turns} turns`), h("span", {}, usd(a.cost_usd))),
    a.summary ? h("div", {}, a.summary) : null,
    a.denied.length ? h("div", { class: "refused" }, `refused: ${a.denied.join(", ")}`) : null,
    climb(a.climb, a.move),
    !a.passed && a.evidence?.trim() ? h("details", {}, h("summary", {}, "evidence"), h("pre", {}, a.evidence)) : null,
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
    // Two steps: a stray click in the drawer must not stop a run. The second click
    // has to come within a few seconds of the first.
    let armed = null;
    const cancel = h("button", { onclick: async () => {
      if (!armed) {
        cancel.textContent = `Stop task #${id}? Click again`;
        cancel.classList.add("danger");
        armed = setTimeout(() => { armed = null; cancel.textContent = "Cancel"; cancel.classList.remove("danger"); }, 4000);
        return;
      }
      clearTimeout(armed);
      await post(`/api/tasks/${id}/cancel`);
      openTask(id);
    } }, "Cancel");
    actions.push(cancel);
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
    stopSection(d.stop),
    h("div", { class: "section" }, h("h2", {}, "Task"), h("div", { class: "text" }, t.body)),
    t.parallel_reason ? h("div", { class: "section" }, h("h2", {}, "Dispatch"),
      h("div", {}, t.parallel ? "May run beside the work going in this repository: " : "Waits for the work ahead of it: ",
        t.parallel_reason)) : null,
    d.plan ? h("div", { class: "section" }, h("h2", {}, "Plan"),
      ladderPath(d.plan, d.attempts),
      h("div", { class: "meta" }, `started at ${d.plan.start}, because:`),
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
      d.messages.map((m) => h("div", {}, h("div", { class: "meta" }, `${AUTHOR[m.role] || m.role} · ${when(m.ts)}`), h("div", { class: "text" }, m.text)))),
  ].filter(Boolean));
  if (!quiet) el.scrollTop = 0;
  el.hidden = false;
}
