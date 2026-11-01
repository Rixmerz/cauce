import { follow, get } from "./net.js";
import { renderBoard } from "./board.js";
import { renderSessions, openSession } from "./sessions.js";
import { renderSpend } from "./spend.js";
import { renderRouting } from "./routing.js";
import { renderMemory } from "./memory.js";
import { renderNotes } from "./notes.js";
import { renderHabits } from "./habits.js";
import { renderWorkflows } from "./workflows.js";
import { renderMap } from "./map.js";
import { renderTeams } from "./teams.js";
import { renderAnalysis } from "./analysis.js";
import { renderEvolution } from "./evolution.js";
import { openTask } from "./task.js";
import { h, repoName } from "./util.js";

const views = { board: renderBoard, sessions: renderSessions, spend: renderSpend, routing: renderRouting,
                notes: renderNotes, memory: renderMemory, habits: renderHabits, workflows: renderWorkflows,
                map: renderMap, teams: renderTeams, analysis: renderAnalysis, evolution: renderEvolution };
// The board, the sessions, the notes, the workflows and the map are always one project's:
// never every card at once.
const scoped = new Set(["board", "sessions", "notes", "workflows", "map"]);
// The analysis and the evolution read one project or every one: the picker chooses which,
// but they are not redrawn on every event (they read every run of the period).
const pickable = new Set([...scoped, "analysis", "evolution"]);
const main = document.getElementById("view");
const picker = document.getElementById("project");
let current = "board";
let project = null;
let pending = null;
let pendingSession = null;
// The run a tab was opened for (`#run=N`): its card is marked and names the tab.
let focusRun = null;

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

const ctx = () => ({ project, openTask, openSession, refresh: () => show(current), toast, focusRun });

export async function show(name = current) {
  // A new view says it is on its way at once: Spend reads every session's
  // transcript and can take seconds, and an unchanged page reads as a dead click.
  if (name !== current) main.replaceChildren(h("div", { class: "empty" }, "Loading…"));
  current = name;
  for (const tab of document.querySelectorAll(".tabs button")) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === name));
  }
  picker.disabled = !pickable.has(name);
  if (scoped.has(name) && !project) {
    main.replaceChildren(h("div", { class: "empty" },
      "No project is enrolled yet. A project enrolls when work is queued or run in it through locks, or with locks init; it gets a .locks/ folder."));
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
  const keep = project || remembered("locks.project");
  picker.replaceChildren(...list.map((p) => h("option", { value: p.repo, title: p.dir || p.repo },
    `${repoName(p.repo)}${p.counts && (p.counts.running || p.counts.queued) ? " •" : ""}`)));
  project = list.some((p) => p.repo === keep) ? keep : (list[0]?.repo ?? null);
  if (project) picker.value = project;
}

// An address that opens one thing over its project's page (`locks ui --tabs` opens one tab
// per session, `#session=<id>`: all of that session's work, each task opened inside the page):
// `#task=N` opens that task over its project's board, `#run=N&repo=<key>` that run among its
// project's workflows. Not remembered as the person's project: it changes nothing in the others.
async function fromAddress() {
  const want = new URLSearchParams(location.hash.slice(1));
  const task = Number(want.get("task")) || null;
  const run = Number(want.get("run")) || null;
  const session = /^[A-Za-z0-9_-]{1,128}$/.test(want.get("session") || "") ? want.get("session") : null;
  const choose = (repo) => {
    if (repo && [...picker.options].some((o) => o.value === repo)) { project = repo; picker.value = repo; }
  };
  if (task) {
    const found = await get(`/api/tasks/${task}`).catch(() => null);
    if (!found) return false;
    choose(found.task.repo);
    document.body.classList.add("solo");
    await show("board");
    await openTask(task);
    return true;
  }
  if (session) {
    const found = await get(`/api/sessions/${encodeURIComponent(session)}`).catch(() => null);
    if (!found) return false;
    choose(found.session.repo);
    await show("sessions");
    await openSession(session, ctx());
    return true;
  }
  if (run) {
    choose(want.get("repo"));
    focusRun = run;
    await show("workflows");
    document.getElementById(`run-${run}`)?.scrollIntoView({ block: "start" });
    return true;
  }
  return false;
}
window.addEventListener("hashchange", () => { fromAddress(); });

picker.addEventListener("change", () => {
  project = picker.value;
  remembered("locks.project", project);
  show(current);
});
for (const tab of document.querySelectorAll(".tabs button")) {
  tab.addEventListener("click", () => show(tab.dataset.view));
}

// An event re-renders the scoped view (coalesced), and the open task if it is the one.
const live = follow(
  (event) => {
    // The map keeps what a person folded and filtered: nothing a run does changes it.
    if (scoped.has(current) && current !== "map") {
      clearTimeout(pending);
      pending = setTimeout(() => show(current), 300);
    }
    const drawer = document.getElementById("drawer");
    if (!drawer.hidden && Number(drawer.dataset.task) === event.task_id) openTask(event.task_id, true);
    // A session's page lists its work: any event may have moved it.
    if (!drawer.hidden && drawer.dataset.session) {
      clearTimeout(pendingSession);
      pendingSession = setTimeout(() => openSession(drawer.dataset.session, ctx()), 1000);
    }
  },
  (on) => document.getElementById("live").classList.toggle("on", on),
);

// Prompts typed in sessions write no events, so the sessions list is looked at
// again on a clock; new projects show up the same way.
setInterval(() => {
  if (current === "sessions" && document.getElementById("drawer").hidden) show("sessions");
}, 15000);
setInterval(loadProjects, 60000);

// A server hands itself over to a newer locks when one is installed: the page
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
if (!(await fromAddress())) show("board");
