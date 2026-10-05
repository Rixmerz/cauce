import { get } from "./net.js";
import { h } from "./util.js";

export async function renderHabits() {
  const d = await get("/api/habits");
  return h("div", {},
    h("p", { class: "empty" }, "An edit followed by the same command every time — a formatter, a linter, tests, a build — "
      + "in several sessions. Only these could run as a hook: the model looking around (find, ls, cat, cd) repeats too, "
      + "but nothing about it can run on its own. Installing one is a command you run: cauce habits install <id> --command \"…\"."),
    h("h2", {}, "Candidates"),
    d.candidates.length
      ? h("table", {}, h("tr", {}, h("th", {}, "id"), h("th", {}, "sequence"), h("th", { class: "num" }, "times"),
            h("th", { class: "num" }, "sessions"), h("th", { class: "num" }, "ok")),
          d.candidates.map((c) => h("tr", {}, h("td", {}, h("span", { class: "chip" }, c.id)), h("td", {}, c.steps.join(" → ")),
            h("td", { class: "num" }, c.occurrences), h("td", { class: "num" }, c.sessions),
            h("td", { class: "num" }, `${Math.round(c.success * 100)}%`))))
      : h("div", { class: "empty" }, "none yet: no edit has been followed by the same command often enough"),
    h("h2", { class: "section" }, "Installed"),
    d.installed.length
      ? h("table", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "after"), h("th", {}, "runs"), h("th", {}, "state")),
          d.installed.map((x) => h("tr", {}, h("td", {}, x.id), h("td", {}, `${x.steps[0]} → ${x.command}`),
            h("td", {}, x.runs), h("td", {}, x.disabled_at ? `off after ${x.failures} failures` : "on"))))
      : h("div", { class: "empty" }, "none"));
}
