// Small DOM helpers. Every piece of task data is model-written text: it is only
// ever set as textContent, never parsed as HTML.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export const TURNS_TITLE = "the session's own turns, its subagents included, at API rates";
export const usd = (v) => `$${(Number(v) || 0).toFixed(2)}`;
// A cost as the API gives it (`usage.so_far`): "running" for an attempt whose cost so far could
// not be read, never $0.00; "so far" while a live one is counted.
export const cost = (item) => (item.cost_running
  ? (Number(item.cost_usd) ? `${usd(item.cost_usd)} + running` : "running")
  : `${usd(item.cost_usd)}${item.cost_live ? " so far" : ""}`);
export const short = (text, n = 140) => {
  const s = String(text ?? "").replace(/\s+/g, " ").trim();
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
};
export const when = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
};
export const repoName = (key) => (key ? String(key).split("/").slice(-2).join("/") : "—");

// Color carries meaning here, never decoration, and never alone: every color
// below sits beside the word it stands for.

// The kind of work, by family: eight hues in a fixed order, checked for
// colour-blind separation in both themes. A new kind joins a family.
const FAMILY = { chat: "read", explore: "read", classify: "read", docs: "docs", test: "test", refactor: "build",
  implement: "build", feature: "build", ui: "ui", "debug-repro": "debug", "debug-unclear": "debug",
  "review-routine": "review", "review-critical": "review", plan: "plan", frontier: "plan" };
export const family = (kind) => FAMILY[kind] || "other";
export function kindChip(kind) {
  if (!kind) return null;
  return h("span", { class: `chip kind k-${family(kind)}` }, h("i", { class: "dot", "aria-hidden": "true" }), kind);
}

// A cell: the model as depth (the stronger, the darker) and the effort as pips.
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
export function cellChip(cell) {
  if (!cell) return null;
  const [model, effort] = String(cell).split("/");
  const on = EFFORTS.indexOf(effort) + 1;
  return h("span", { class: `chip cell m-${model}`, title: effort ? `${model} at ${effort} effort` : model },
    h("b", {}, model),
    effort ? h("span", { class: "pips", "aria-label": `effort ${effort}` },
      EFFORTS.map((_, i) => h("i", { class: i < on ? "on" : "" }))) : null,
    effort ? h("span", { class: "effort" }, effort) : null);
}

// A task's size on the Fibonacci scale, with how it was reached as its title: a number, not a tone.
export function pointsChip(points, why) {
  if (!points) return null;
  return h("span", { class: "chip points", title: why ? `${points}: ${why}` : `${points} points` }, `${points} pts`);
}

// A task's state: a tone, an icon and its word, so it reads at a glance and
// without color.
const STATES = { done: ["ok", "✓", "done"], running: ["run", "●", "running"], queued: ["queue", "○", "queued"],
  blocked: ["warn", "!", "blocked"], needs_approval: ["warn", "◆", "needs approval"], replan: ["warn", "↻", "replan"],
  failed: ["bad", "✕", "failed"], interrupted: ["bad", "✕", "interrupted"], cancelled: ["mute", "–", "cancelled"],
  dismissed: ["mute", "–", "dismissed"] };
export const tone = (status) => (STATES[status] || ["mute"])[0];
export const stateWord = (status) => (STATES[status] || [null, null, status || ""])[2];
// A page opened on one piece of work (`#task=N`, `#run=N`) names it and its state: the
// state's mark and word, so a row of tabs reads at a glance without the colour.
export function tabTitle(status, text, said = null) {
  const [, icon, word] = STATES[status] || ["mute", "•", status || ""];
  document.title = `${icon} ${said || word} · ${short(text, 60)} · locks`;
  stateIcon(status);
}

// The tab's favicon wears the state too: the tone's colour from the page's own tokens (so it
// follows the theme), and a mark of its own per tone, so it does not lean on the colour alone.
const TONE_VAR = { ok: "--ok", run: "--accent", queue: "--queue", warn: "--warn", bad: "--bad", mute: "--muted" };
const MARKS = {
  ok: '<path d="M9.5 16.5l4.5 4.5 8.5-9.5" fill="none" stroke="P" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/>',
  run: '<circle cx="16" cy="16" r="6" fill="P"/>',
  queue: '<circle cx="16" cy="16" r="5.6" fill="none" stroke="P" stroke-width="3"/>',
  warn: '<path d="M16 8.5v9" stroke="P" stroke-width="3.6" stroke-linecap="round"/><circle cx="16" cy="23" r="2.2" fill="P"/>',
  bad: '<path d="M10.5 10.5l11 11m0-11l-11 11" stroke="P" stroke-width="3.4" stroke-linecap="round"/>',
  mute: '<path d="M10 16h12" stroke="P" stroke-width="3.4" stroke-linecap="round"/>',
};
let iconState = null;
const iconLink = () => document.querySelector('link[rel="icon"]');
const BOARD_ICON = iconLink()?.getAttribute("href") || "/static/favicon.svg";
const token = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

/** The data: URL of a state's favicon, its colours read from the tokens now (`img-src data:`). */
export function stateIconUrl(status) {
  const t = tone(status);
  const fill = token(TONE_VAR[t] || "--muted", "#4f5b55");
  const mark = MARKS[t] || MARKS.mute;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect x="1" y="1" width="30" height="30" rx="8" fill="${fill}"/>${
    mark.replaceAll("P", token("--panel", "#f7f8f5"))}</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** Tint the tab's favicon by a state; null gives the board its own back. */
export function stateIcon(status) {
  iconState = status;
  const link = iconLink();
  if (!link) return;
  link.setAttribute("href", status ? stateIconUrl(status) : BOARD_ICON);
  link.dataset.state = status || "";
}
// A theme switched while the tab is open: the same state, in the new theme's tones.
globalThis.matchMedia?.("(prefers-color-scheme: dark)")?.addEventListener?.("change", () => {
  if (iconState) stateIcon(iconState);
});

// Seconds as a person reads them.
export function secs(v) {
  if (v === null || v === undefined) return "—";
  if (v < 120) return `${Math.round(v)}s`;
  if (v < 7200) return `${(v / 60).toFixed(1)}m`;
  return `${(v / 3600).toFixed(1)}h`;
}
export function statusBadge(status) {
  if (!status) return null;
  const [t, icon, word] = STATES[status] || ["mute", "•", status];
  return h("span", { class: `badge t-${t}` }, h("span", { class: "icon", "aria-hidden": "true" }, icon), word);
}
