import { get, post } from "./net.js";
import { h, repoName, short, usd, when } from "./util.js";

function card(task, openTask, extra = []) {
  return h("div", { class: "card", onclick: () => openTask(task.id), tabindex: 0,
                    onkeydown: (e) => { if (e.key === "Enter") openTask(task.id); } },
    h("div", { class: "title" }, short(task.title, 120)),
    h("div", { class: "meta" },
      h("span", {}, repoName(task.repo)),
      task.kind ? h("span", { class: "chip" }, task.kind) : null,
      task.current_cell || task.final_cell || task.start_cell
        ? h("span", { class: "chip" }, task.current_cell || task.final_cell || task.start_cell) : null,
      task.cost_usd ? h("span", {}, usd(task.cost_usd)) : null,
      ...extra),
    task.asks ? h("div", { class: "asks" }, task.asks) : null);
}

function column(title, count, children) {
  return h("section", { class: "column" },
    h("h2", {}, title, h("span", { class: "count" }, String(count))),
    children.length ? children : h("div", { class: "empty" }, "nothing here"));
}

function queueForm(ctx) {
  const text = h("input", { placeholder: "Queue a task for a worker…", "aria-label": "task" });
  const repo = h("input", { placeholder: "/path/to/repository", "aria-label": "repository" });
  const verify = h("input", { placeholder: "verify command (optional)", "aria-label": "verify" });
  const kind = h("select", { "aria-label": "kind" }, h("option", { value: "" }, "kind: auto"),
    ...["implement", "debug-repro", "debug-unclear", "feature", "refactor", "ui", "test", "docs", "explore",
        "review-routine", "review-critical", "plan"].map((k) => h("option", { value: k }, k)));
  const submit = async (e) => {
    e.preventDefault();
    try {
      const res = await post("/api/queue", { text: text.value, repo_dir: repo.value, verify: verify.value, kind: kind.value });
      ctx.toast(`queued #${res.id}`);
      text.value = "";
      ctx.refresh();
    } catch (err) { ctx.toast(err.message); }
  };
  return h("form", { class: "queue-form", onsubmit: submit }, text, repo, verify, kind,
    h("button", { class: "primary", type: "submit" }, "Queue"));
}

export async function renderBoard(ctx) {
  const b = await get("/api/board");
  const lanes = b.queued.map((lane) => h("div", { class: "lane" },
    h("div", { class: "lane-head" },
      h("span", {}, repoName(lane.repo)),
      lane.paused
        ? h("span", { class: "paused" }, "paused ",
            h("button", { onclick: async (e) => { e.stopPropagation(); await post("/api/lanes/unpause", { repo: lane.repo }); ctx.refresh(); } }, "Unpause"))
        : h("button", { onclick: async () => { await post("/api/work", { repo_dir: lane.tasks[0]?.cwd }); ctx.toast("worker started"); } }, "Run lane")),
    lane.paused && lane.reason ? h("div", { class: "asks" }, short(lane.reason, 160)) : null,
    lane.tasks.map((t) => card(t, ctx.openTask))));
  const days = new Map();
  for (const t of b.done) {
    const day = (t.updated_at || "").slice(0, 10);
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(card(t, ctx.openTask, t.branch ? [h("span", { class: "chip" }, t.branch)] : []));
  }
  const diary = [...days.entries()].map(([day, cards]) => [h("div", { class: "lane-head" }, day), cards]);
  return h("div", {},
    queueForm(ctx),
    h("div", { class: "columns" },
      column("Needs you", b.counts.needs_you, b.needs_you.map((t) => card(t, ctx.openTask, [h("span", { class: `status-${t.status}` }, t.status)]))),
      column("Running", b.counts.running, b.running.map((t) => card(t, ctx.openTask, [h("span", {}, `attempt ${t.attempt ?? 1}`), h("span", {}, when(t.updated_at))]))),
      column("Queued", b.counts.queued, lanes),
      column("Done", b.counts.done, diary)));
}
