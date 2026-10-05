import { get } from "./net.js";
import { h, short, usd, when } from "./util.js";

const drawer = () => document.getElementById("drawer");
const MARK = { completed: "☑", in_progress: "◐", pending: "☐" };

function copy(text, ctx) {
  return h("button", { onclick: async (e) => {
    e.stopPropagation();
    try { await navigator.clipboard.writeText(text); ctx.toast("copied"); } catch { ctx.toast(text); }
  } }, "Copy");
}

function progress(plan) {
  if (!plan) return null;
  return h("span", { class: plan.done === plan.total ? "chip ok" : "chip" }, `plan ${plan.done}/${plan.total}`);
}

export async function renderSessions(ctx) {
  const list = await get(`/api/sessions?repo=${encodeURIComponent(ctx.project)}`);
  if (!list.length) {
    return h("div", { class: "empty" }, "No session in this project yet.");
  }
  return h("div", { class: "sessions" }, list.map((s) => h("div", {
    class: "card session", tabindex: 0, onclick: () => openSession(s.id, ctx),
    onkeydown: (e) => { if (e.key === "Enter") openSession(s.id, ctx); } },
  h("div", { class: "title" }, short(s.name || s.last_prompt || "(no prompt yet)", 140)),
  s.name && s.last_prompt ? h("div", { class: "reason" }, `last: ${short(s.last_prompt, 120)}`) : null,
  h("div", { class: "meta" },
    s.running ? h("span", { class: "chip ok" }, `${s.running} running`) : null,
    progress(s.plan),
    h("span", {}, `${s.prompts} prompt${s.prompts === 1 ? "" : "s"}`),
    s.cost_usd ? h("span", {}, usd(s.cost_usd)) : null,
    h("span", {}, `last seen ${when(s.last_seen_at)}`)),
  h("div", { class: "resume" }, h("code", {}, s.resume), copy(s.resume, ctx)))));
}

function planList(plan) {
  if (plan.state === "unreadable") return h("div", { class: "asks" }, "Its task list exists but could not be read.");
  if (!plan.tasks.length) return h("div", { class: "empty" }, "This session has not written a task list.");
  return h("ul", { class: "plan" }, plan.tasks.map((t) => h("li", { class: `plan-${t.status}` },
    h("span", { class: "mark-box", "aria-label": t.status }, MARK[t.status] || "☐"),
    h("div", {},
      h("div", { class: "subject" }, t.status === "in_progress" && t.activeForm ? `${t.subject} — ${t.activeForm}` : t.subject),
      t.description ? h("div", { class: "meta" }, short(t.description, 300)) : null))));
}

export async function openSession(id, ctx) {
  const d = await get(`/api/sessions/${encodeURIComponent(id)}`);
  const el = drawer();
  delete el.dataset.task;
  const close = () => { el.hidden = true; el.replaceChildren(); };
  const s = d.session;
  el.replaceChildren(
    h("header", {},
      h("div", {},
        h("h1", {}, short(s.name || s.last_prompt || s.id, 120)),
        h("div", { class: "meta row" },
          s.name ? h("span", { class: "chip", title: "edit it in .cauce/sessions.json; a name you set is kept" },
            s.named_by === "you" ? "named by you" : "named by Haiku") : null,
          h("span", {}, `${s.prompts} prompt${s.prompts === 1 ? "" : "s"}`), h("span", {}, usd(s.cost_usd)),
          h("span", {}, `last seen ${when(s.last_seen_at)}`), h("code", {}, s.id))),
      h("div", { class: "row" }, copy(s.resume, ctx), h("button", { onclick: close, "aria-label": "close" }, "✕"))),
    h("div", { class: "section" }, h("h2", {}, "Its task list"), planList(d.plan)),
    h("div", { class: "section" }, h("h2", {}, "Work it gave cauce"),
      d.tasks.length ? d.tasks.map((t) => h("div", { class: "card", onclick: () => ctx.openTask(t.id) },
        h("div", { class: "title" }, short(t.title, 120)),
        h("div", { class: "meta" }, h("span", { class: `status-${t.status}` }, t.status),
          t.kind ? h("span", { class: "chip" }, t.kind) : null, t.cost_usd ? h("span", {}, usd(t.cost_usd)) : null)))
        : h("div", { class: "empty" }, "Nothing queued or run through cauce from this session.")),
    h("div", { class: "section" }, h("h2", {}, "Recent turns"),
      d.turns.map((t) => h("div", { class: "turn" },
        h("div", { class: "meta" }, h("span", { class: `status-${t.status}` }, t.status), h("span", {}, when(t.created_at))),
        h("div", { class: "subject" }, short(t.title, 200)),
        t.result ? h("div", { class: "text" }, short(t.result, 400)) : null))));
  el.scrollTop = 0;
  el.hidden = false;
}
