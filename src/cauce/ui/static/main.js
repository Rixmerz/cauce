import { follow, get } from "./net.js";
import { renderBoard } from "./board.js";
import { renderSessions, openSession } from "./sessions.js";
import { renderSpend } from "./spend.js";
import { renderRouting } from "./routing.js";
import { renderMemory } from "./memory.js";
import { renderHabits } from "./habits.js";
import { openTask } from "./task.js";
import { h, repoName } from "./util.js";

const views = { board: renderBoard, sessions: renderSessions, spend: renderSpend, routing: renderRouting,
                memory: renderMemory, habits: renderHabits };
// The board and the sessions are always one project's: never every card at once.
const scoped = new Set(["board", "sessions"]);
const main = document.getElementById("view");
const picker = document.getElementById("project");
let current = "board";
let project = null;
let pending = null;

function remembered(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* storage may be off: the page works without it */ }
  return null;
}

export function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 3500);
}

const ctx = () => ({ project, openTask, openSession, refresh: () => show(current), toast });

export async function show(name = current) {
  current = name;
  for (const tab of document.querySelectorAll(".tabs button")) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === name));
  }
  picker.disabled = !scoped.has(name);
  if (scoped.has(name) && !project) {
    main.replaceChildren(h("div", { class: "empty" },
      "No project is enrolled yet. A project enrolls when work is queued or run in it through cauce, or with cauce init; it gets a .cauce/ folder."));
    return;
  }
  try {
    main.replaceChildren(await views[name](ctx()));
  } catch (err) {
    main.replaceChildren(document.createTextNode(`Could not load this view: ${err.message}`));
  }
}

async function loadProjects() {
  const list = await get("/api/projects").catch(() => []);
  const keep = project || remembered("cauce.project");
  picker.replaceChildren(...list.map((p) => h("option", { value: p.repo, title: p.dir || p.repo },
    `${repoName(p.repo)}${p.counts && (p.counts.running || p.counts.queued) ? " •" : ""}`)));
  project = list.some((p) => p.repo === keep) ? keep : (list[0]?.repo ?? null);
  if (project) picker.value = project;
}

picker.addEventListener("change", () => {
  project = picker.value;
  remembered("cauce.project", project);
  show(current);
});
for (const tab of document.querySelectorAll(".tabs button")) {
  tab.addEventListener("click", () => show(tab.dataset.view));
}

// An event re-renders the scoped view (coalesced), and the open task if it is the one.
const live = follow(
  (event) => {
    if (scoped.has(current)) {
      clearTimeout(pending);
      pending = setTimeout(() => show(current), 300);
    }
    const drawer = document.getElementById("drawer");
    if (!drawer.hidden && Number(drawer.dataset.task) === event.task_id) openTask(event.task_id, true);
  },
  (on) => document.getElementById("live").classList.toggle("on", on),
);

// Prompts typed in sessions write no events, so the sessions list is looked at
// again on a clock; new projects show up the same way.
setInterval(() => {
  if (current === "sessions" && document.getElementById("drawer").hidden) show("sessions");
}, 15000);
setInterval(loadProjects, 60000);

// A server hands itself over to a newer cauce when one is installed: the page
// reloads to run that version's code too.
const served = await get("/api/version").then((v) => v.version).catch(() => null);
setInterval(async () => {
  const now = await get("/api/version").then((v) => v.version).catch(() => null);
  if (served && now && now !== served) location.reload();
}, 30000);

await loadProjects();
// The stream starts after the newest event the board knows; with no project yet there are no events.
(project ? get(`/api/board?repo=${encodeURIComponent(project)}`) : Promise.resolve({ last_event: 0 }))
  .then((b) => live.start(b.last_event)).catch(() => live.start(0));
show("board");
