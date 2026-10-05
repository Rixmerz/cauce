import { follow, get } from "./net.js";
import { renderBoard } from "./board.js";
import { renderSpend } from "./spend.js";
import { renderRouting } from "./routing.js";
import { renderMemory } from "./memory.js";
import { renderHabits } from "./habits.js";
import { openTask } from "./task.js";

const views = { board: renderBoard, spend: renderSpend, routing: renderRouting, memory: renderMemory, habits: renderHabits };
const main = document.getElementById("view");
let current = "board";
let pending = null;

export function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 3500);
}

export async function show(name = current) {
  current = name;
  for (const tab of document.querySelectorAll(".tabs button")) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === name));
  }
  try {
    main.replaceChildren(await views[name]({ openTask, refresh: () => show(current), toast }));
  } catch (err) {
    main.replaceChildren(document.createTextNode(`Could not load this view: ${err.message}`));
  }
}

for (const tab of document.querySelectorAll(".tabs button")) {
  tab.addEventListener("click", () => show(tab.dataset.view));
}

// An event re-renders the board (coalesced), and the open task if it is the one.
const live = follow(
  (event) => {
    if (current === "board") {
      clearTimeout(pending);
      pending = setTimeout(() => show("board"), 300);
    }
    const drawer = document.getElementById("drawer");
    if (!drawer.hidden && Number(drawer.dataset.task) === event.task_id) openTask(event.task_id, true);
  },
  (on) => document.getElementById("live").classList.toggle("on", on),
);

get("/api/board").then((b) => live.start(b.last_event)).catch(() => live.start(0));
show("board");
