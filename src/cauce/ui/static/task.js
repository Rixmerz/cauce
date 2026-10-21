import { toast } from "./main.js";
import { get, post } from "./net.js";
import { notifyButton, watchEnding } from "./notify.js";
import { stopSection } from "./stop.js";
import {
  cellChip, cost, h, kindChip, pointsChip, repoName, secs, short, stateIcon, stateWord, statusBadge, tabTitle, TURNS_TITLE, usd, when,
} from "./util.js";

const drawer = () => document.getElementById("drawer");
const solo = () => document.body.classList.contains("solo");
// How often the page asks what a running task's worker is doing: tool calls write no events.
const POLL_MS = 5000;
let polling = null;

function close() {
  clearTimeout(polling);
  drawer().hidden = true;
  drawer().replaceChildren();
  // A tab opened for this task becomes the board: its address, title and favicon say so.
  if (solo()) {
    document.body.classList.remove("solo");
    history.replaceState(null, "", location.pathname);
    document.title = "cauce";
    stateIcon(null);
  }
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

const DIAL = { effort: "more effort, same model", model: "a different model", turns: "more turns, same cell",
  retry: "the same cell again", stop: "stopped" };

// The move after a failed attempt: where it went, along which dial, and the evidence, in order.
function climb(c, move) {
  if (!c) return null;
  const head = c.to
    ? [h("b", {}, `${DIAL[c.axis] || c.axis}: `), cellChip(c.from), " → ", cellChip(c.to)]
    : [h("b", {}, `${move}: `), "no further attempt"];
  return h("div", { class: `climb climb-${c.axis}` },
    h("div", { class: "row" }, ...head,
      c.turns_to && c.turns_to !== c.turns_from ? h("span", { class: "meta" }, `turns ${c.turns_from} → ${c.turns_to}`) : null,
      c.spent_usd !== undefined && c.spent_usd !== null ? h("span", { class: "meta" }, `$${Number(c.spent_usd).toFixed(2)} spent`) : null),
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
      cellChip(cell), ...(ran.get(cell) || []).map((a) => h("sup", {}, ` #${a.seq}`)))]));
}

// A diff, line by line: what was added, what was removed, where each hunk starts.
function diffView(text, cut) {
  const tone = (line) => (line.startsWith("+++") || line.startsWith("---") ? "file"
    : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "");
  return h("pre", { class: "diff" }, text.split("\n").map((line) => h("span", { class: tone(line) }, `${line}\n`)),
    cut ? h("span", { class: "hunk" }, "… cut") : null);
}

function attempt(a) {
  return h("li", { class: a.passed ? "pass" : "fail" },
    h("div", { class: "row" },
      h("b", {}, `#${a.seq}`), cellChip(a.cell),
      a.served_model ? h("span", { class: "chip", title: "the model that served it, as the CLI reported" }, a.served_model) : null,
      h("span", { class: `badge ${a.passed ? "t-ok" : "t-bad"}` }, h("span", { class: "icon", "aria-hidden": "true" },
        a.passed ? "✓" : "✕"), a.passed ? "pass" : (a.failure || "fail")),
      h("span", {}, `${a.turns} turns`), h("span", {}, usd(a.cost_usd))),
    a.summary ? h("div", {}, a.summary) : null,
    a.denied.length ? h("div", { class: "refused" }, `refused: ${a.denied.join(", ")}`) : null,
    climb(a.climb, a.move),
    !a.passed && a.evidence?.trim() ? h("details", {}, h("summary", {}, "evidence"), h("pre", {}, a.evidence)) : null,
    a.changed_paths.length ? h("div", { class: "meta" }, `changed: ${a.changed_paths.join(", ")}`) : null,
    a.capabilities.length ? h("div", { class: "meta" }, `capabilities: ${a.capabilities.join(", ")}`) : null);
}

// What the worker of a running task is doing now: its phase and its last tool calls, as their
// signatures keep them (a tool and a verb, never an argument), newest last.
const clock = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso ?? "") : d.toLocaleTimeString();
};

function liveSection(live) {
  const calls = live.recent_calls || [];
  return h("div", { class: "section live-now", "aria-live": "polite" },
    h("h2", {}, `Now · attempt ${live.attempt_now ?? "?"}`),
    h("div", { class: "row" }, live.phase ? h("span", { class: `chip phase p-${live.phase}` }, live.phase)
      : h("span", { class: "meta" }, "no tool call yet in this attempt")),
    calls.length ? h("ol", { class: "calls" }, calls.map((c) => h("li", { class: c.ok ? "" : "failed-call" },
      h("span", { class: "meta" }, clock(c.ts)), " ", h("code", {}, c.sig), " ",
      h("span", { class: "meta" }, c.kind), c.ok ? null : h("span", { class: "badge t-bad" }, "✕ failed")))) : null);
}

function poll(id) {
  clearTimeout(polling);
  polling = setTimeout(async () => {
    const el = drawer();
    if (el.hidden || Number(el.dataset.task) !== id) return;
    const live = await get(`/api/tasks/${id}/live`).catch(() => null);
    if (el.hidden || Number(el.dataset.task) !== id) return;
    if (live && live.status !== el.dataset.status) {
      openTask(id, true);
      return;
    }
    if (live) el.querySelector(".live-now")?.replaceWith(liveSection(live));
    poll(id);
  }, POLL_MS);
}

// Two attempts side by side: which pair each task's page compares, kept across its re-renders.
const comparing = new Map();

function moveAfter(a) {
  const c = a.climb;
  if (c?.to) return [`${DIAL[c.axis] || c.axis}: `, cellChip(c.from), " → ", cellChip(c.to)];
  if (c || a.move) return `${a.move || c.axis}: no further attempt`;
  return a.passed ? "none: it passed" : "—";
}

function compareColumn(a) {
  const row = (label, ...value) => [h("dt", {}, label), h("dd", {}, ...value)];
  return h("div", { class: `compare-col ${a.passed ? "pass" : "fail"}` },
    h("h3", {}, `Attempt #${a.seq}`),
    h("dl", {},
      row("cell", cellChip(a.cell)),
      row("served by", a.served_model || "—"),
      row("turns", `${a.turns}${a.max_turns ? ` of ${a.max_turns}` : ""}`),
      row("cost", usd(a.cost_usd)),
      row("wall time", a.duration_s ? secs(a.duration_s) : "—"),
      row("ended", h("span", { class: `badge ${a.passed ? "t-ok" : "t-bad"}` },
        h("span", { class: "icon", "aria-hidden": "true" }, a.passed ? "✓" : "✕"), a.passed ? "pass" : (a.failure || "fail"))),
      row("move after", moveAfter(a)),
      row("changed", a.changed_paths.length ? h("ul", {}, a.changed_paths.map((p) => h("li", {}, h("code", {}, p))))
        : "nothing"),
      row("summary", a.summary ? h("div", { class: "text" }, a.summary) : "—"),
      row("check evidence", a.evidence?.trim() ? h("pre", {}, a.evidence) : "—")));
}

function compareView(taskId, attempts) {
  const pair = comparing.get(taskId);
  const bySeq = new Map(attempts.map((a) => [a.seq, a]));
  const pick = (side, label) => {
    const id = `cmp-${taskId}-${side}`;
    return h("label", { for: id }, `${label} `,
      h("select", { id, onchange: (e) => {
        pair[side] = Number(e.target.value);
        e.target.closest(".compare-box").replaceWith(compareView(taskId, attempts));
      } }, attempts.map((a) => {
        const opt = h("option", { value: a.seq }, `#${a.seq} ${a.cell}`);
        opt.selected = a.seq === pair[side];
        return opt;
      })));
  };
  return h("div", { class: "compare-box" },
    h("div", { class: "row" }, pick("a", "Left"), pick("b", "Right")),
    h("div", { class: "compare" }, [pair.a, pair.b].map((seq) => compareColumn(bySeq.get(seq) || attempts[0]))));
}

function attemptsSection(taskId, attempts) {
  const box = h("div", {});
  const draw = () => box.replaceChildren(comparing.has(taskId) ? compareView(taskId, attempts) : []);
  const toggle = attempts.length >= 2 ? h("button", { type: "button", "aria-expanded": String(comparing.has(taskId)),
    onclick: () => {
      if (comparing.has(taskId)) comparing.delete(taskId);
      else comparing.set(taskId, { a: attempts.at(-2).seq, b: attempts.at(-1).seq });
      toggle.setAttribute("aria-expanded", String(comparing.has(taskId)));
      toggle.textContent = comparing.has(taskId) ? "Close the comparison" : "Compare";
      draw();
    } }, comparing.has(taskId) ? "Close the comparison" : "Compare") : null;
  draw();
  return h("div", { class: "section" }, h("div", { class: "row section-head" }, h("h2", {}, "Attempts"), toggle), box,
    h("ul", { class: "timeline" }, attempts.map(attempt)));
}

export async function openTask(id, quiet = false) {
  clearTimeout(polling);
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
  if (["failed", "blocked", "replan", "needs_approval", "interrupted"].includes(t.status)) {
    // It waits on the orchestrator; this takes it off the board. A resume brings it back.
    const dismiss = h("button", { onclick: async () => {
      if (dismiss.dataset.armed !== "1") {
        dismiss.dataset.armed = "1";
        dismiss.textContent = `Dismiss #${id}? Click again`;
        dismiss.classList.add("danger");
        setTimeout(() => { dismiss.dataset.armed = ""; dismiss.textContent = "Dismiss"; dismiss.classList.remove("danger"); }, 4000);
        return;
      }
      await post(`/api/tasks/${id}/dismiss`);
      openTask(id);
    } }, "Dismiss");
    actions.push(dismiss);
  }
  const stop = d.stop;
  if (stop && stop.next && t.status !== "cancelled") {
    // What a press resumes with (rules or an approval) is read from
    // cauce's account on the server, never sent from here.
    const label = { permission: "Allow & resume", approval: "Approve & resume" }[stop.cause] || "Resume";
    const press = (keep) => async () => {
      try {
        await post(`/api/tasks/${id}/resume`, { keep });
        toast(keep ? `Kept for this repository; resuming #${id}` : `Resuming #${id}`);
      } catch (err) {
        toast(String(err.message || err));
      }
      openTask(id);
    };
    if (stop.cause !== "permission" || (stop.allow && stop.allow.length)) {
      actions.unshift(h("button", { class: "primary", onclick: press(false) }, label));
    }
    if (stop.cause === "permission" && stop.allow && stop.allow.length) {
      actions.splice(1, 0, h("button", { onclick: press(true) }, "Always allow here"));
    }
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
          statusBadge(t.status), h("span", {}, repoName(t.repo)),
          kindChip(t.kind), pointsChip(t.points, t.points_why),
          h("span", { title: "cauce's workers" }, `workers ${cost(t)}`),
          t.turns_usd ? h("span", { title: TURNS_TITLE }, `turns ${usd(t.turns_usd)}`) : null,
          t.pinned ? h("span", { class: "chip" }, "pinned") : null, h("span", {}, when(t.created_at)))),
      h("div", { class: "row" }, ...actions, solo() ? notifyButton() : null,
        h("button", { onclick: close, "aria-label": "close" }, "✕"))),
    stopSection(d.stop),
    t.status === "running" ? liveSection(d) : null,
    h("div", { class: "section" }, h("h2", {}, "Task"), h("div", { class: "text" }, t.body)),
    t.parallel_reason ? h("div", { class: "section" }, h("h2", {}, "Dispatch"),
      h("div", {}, t.waits ? `${t.waits[0].toUpperCase()}${t.waits.slice(1)}` : t.parallel
        ? `May run beside the work going in this repository: ${t.parallel_reason}`
        : `Waits for the work ahead of it: ${t.parallel_reason}`)) : null,
    d.plan ? h("div", { class: "section" }, h("h2", {}, "Plan"),
      d.risk ? h("div", { class: "meta" }, h("span", { class: `badge risk r-${d.risk.tier}` },
        `risk ${d.risk.tier}`), (d.risk.reasons || []).join("; ")) : null,
      t.points ? h("div", { class: "meta" }, `points ${t.points}: ${t.points_why || "nothing says it is bigger"}`
        + (d.realized ? ` · realized ${d.realized.points}${d.realized.reasons.length ? `: ${d.realized.reasons.join(", ")}` : ""}` : "")) : null,
      ladderPath(d.plan, d.attempts),
      h("div", { class: "meta" }, `started at ${d.plan.start}, because:`),
      h("ul", {}, (d.plan.reasons || []).map((r) => h("li", {}, r))),
      (d.plan.neighbours || []).map((n) => h("div", { class: "meta" }, n))) : null,
    d.attempts.length ? attemptsSection(t.id, d.attempts) : null,
    (d.checks || []).length ? h("div", { class: "section" }, h("h2", {}, "Independent checks"),
      d.checks.map((c) => h("div", { class: "fix" },
        h("span", { class: `badge ${c.blocking ? "t-bad" : c.passed ? "t-ok" : "t-mute"}` },
          c.blocking ? `✕ ${c.blocking} problem(s)` : c.passed ? "✓ clean" : "– undecided"),
        ` attempt ${c.seq} at `, h("span", { class: "chip" }, c.cell), ` · ${c.summary || ""}`,
        (c.findings || []).length ? h("ul", {}, c.findings.map((f) => h("li", {},
          h("span", { class: `chip cause c-${f.cause}` }, f.cause), " ", h("code", {}, f.where || "unplaced"),
          ` ${f.what}`))) : null))) : null,
    d.branch || d.impact.length ? h("div", { class: "section" }, h("h2", {}, "Result"),
      d.branch ? h("div", {}, "branch ", h("span", { class: "chip" }, d.branch), " ",
        h("button", { onclick: async (e) => {
          const box = e.target.parentElement.nextElementSibling;
          const found = await get(`/api/tasks/${t.id}/diff`);
          box.replaceChildren(found.diff ? diffView(found.diff, found.cut) : h("div", { class: "empty" }, found.why));
        } }, "Show the change")) : null,
      d.branch ? h("div", { class: "diff-box" }) : null,
      d.impact.map((line) => h("div", { class: "meta" }, line))) : null,
    d.dead_ends.length ? h("div", { class: "section" }, h("h2", {}, "Dead ends it was shown"),
      d.dead_ends.map((x) => h("div", { class: "fix" },
        h("span", { class: `tag ${x.disproved_on ? "disproved" : "failed"}` }, x.disproved_on ? "disproved" : "failed"),
        `${x.problem}: ${x.tried}`, x.worked_instead ? ` — worked instead: ${x.worked_instead}` : ""))) : null,
    d.children.length ? h("div", { class: "section" }, h("h2", {}, "Delegations"),
      d.children.map((c) => h("div", { class: "fix" }, statusBadge(c.status), " ", c.title))) : null,
    h("div", { class: "section" }, h("h2", {}, "Messages"),
      d.messages.map((m) => h("div", {}, h("div", { class: "meta" }, h("span", {}, m.author || m.role), h("span", {}, when(m.ts))), h("div", { class: "text" }, m.text)))),
  ].filter(Boolean));
  if (!quiet) el.scrollTop = 0;
  el.hidden = false;
  el.dataset.status = t.status;
  if (t.status === "running") poll(id);
  if (solo() && Number(el.dataset.task) === t.id) {
    tabTitle(t.status, `#${t.id} ${t.title}`);
    watchEnding(`task:${t.id}`, t.status, `#${t.id} ${short(t.title, 80)}`,
      `${stateWord(t.status)}${d.stop?.reason ? `: ${short(d.stop.reason, 160)}` : ""}`);
  }
}
