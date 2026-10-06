// Why a task stopped, who made the call, and the command that continues it.
// The account comes from the server (`stops.view`); this only lays it out.
import { h, short } from "./util.js";

function copyButton(text) {
  const button = h("button", { class: "copy", title: text, onclick: async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "copied";
    } catch {
      button.textContent = "select it to copy";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1500);
  } }, "Copy");
  return button;
}

function command(text) {
  return h("div", { class: "next" }, h("code", {}, text), copyButton(text));
}

// The card's few lines: who, why, and the command.
export function stopCard(stop) {
  if (!stop) return null;
  return h("div", { class: "stop" },
    h("div", {}, h("span", { class: `by by-${stop.by}` }, `by ${stop.who}`), " ", short(stop.reason, 180)),
    stop.denied?.length ? h("div", { class: "refused" }, `refused: ${short(stop.denied.join(", "), 160)}`) : null,
    // The command says what to do; without one, the sentence does.
    stop.next ? command(stop.next) : h("div", { class: "asks" }, stop.todo));
}

// The drawer's section: all of it, the worker's own words included.
export function stopSection(stop) {
  if (!stop) return null;
  return h("div", { class: "section stop-detail" }, h("h2", {}, "Why it stopped"),
    h("div", { class: "row" },
      h("span", { class: `by by-${stop.by}` }, `by ${stop.who}`),
      h("span", { class: "chip" }, stop.cause),
      stop.attempt ? h("span", { class: "meta" }, `after attempt ${stop.attempt}`) : null),
    h("div", { class: "text" }, stop.reason),
    stop.denied?.length ? h("div", {}, h("b", {}, "Refused by your permission settings"),
      h("ul", {}, stop.denied.map((rule) => h("li", {}, h("code", {}, rule))))) : null,
    stop.allow?.length ? h("div", {}, h("b", {}, "The rules that let it through, one per program"),
      h("ul", {}, stop.allow.map((rule) => h("li", {}, h("code", {}, rule))))) : null,
    stop.next_cell ? h("div", { class: "meta" }, `the next cell would be ${stop.next_cell}`) : null,
    stop.account ? h("div", {}, h("b", {}, "The last worker's own account"), h("div", { class: "text" }, stop.account)) : null,
    h("div", { class: "asks" }, stop.todo),
    stop.next ? command(stop.next) : null,
    stop.keep ? h("div", { class: "meta" }, "to keep these rules for every task in this repository:") : null,
    stop.keep ? command(stop.keep) : null,
    stop.recovered ? h("div", { class: "meta" },
      "Read back from this task's records: it stopped before cauce kept an account of why.") : null);
}
