import { get } from "./net.js";
import { h } from "./util.js";

function table(rows, withRepos) {
  return h("table", {}, h("tr", {}, h("th", {}, "id"), h("th", {}, "sequence"), h("th", { class: "num" }, "times"),
      h("th", { class: "num" }, "sessions"), withRepos ? h("th", { class: "num" }, "projects") : null,
      h("th", { class: "num" }, "ok")),
    rows.map((c) => h("tr", {}, h("td", {}, h("span", { class: "chip" }, c.id)), h("td", {}, c.steps.join(" → ")),
      h("td", { class: "num" }, c.occurrences), h("td", { class: "num" }, c.sessions),
      withRepos ? h("td", { class: "num" }, c.repos) : null,
      h("td", { class: "num" }, `${Math.round(c.success * 100)}%`))));
}

// One project's habits: mined from its own sessions, since a command is a
// project's tooling; the ones other projects share are offered apart.
export async function renderHabits(ctx) {
  const d = await get(`/api/habits?${new URLSearchParams({ repo: ctx.project || "" })}`);
  return h("div", {},
    h("p", { class: "empty" }, "An edit followed by the same command every time — a formatter, a linter, tests, a build — "
      + "in several of this project's sessions. Only these could run as a hook: the model looking around (find, ls, cat, cd) "
      + "repeats too, but nothing about it can run on its own. Run /cauce:habits in a session here to review and install them."),
    h("h2", {}, "In this project"),
    d.candidates.length ? table(d.candidates, false)
      : h("div", { class: "empty" }, "none yet: no edit here has been followed by the same command often enough"),
    d.elsewhere.length ? h("h2", { class: "section" }, "In other projects, not here yet") : null,
    d.elsewhere.length ? h("p", { class: "empty" }, "Worth one here only when this project has the same tool.") : null,
    d.elsewhere.length ? table(d.elsewhere, true) : null,
    h("h2", { class: "section" }, "Installed here"),
    d.installed.length
      ? h("table", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "after"), h("th", {}, "runs"), h("th", {}, "state")),
          d.installed.map((x) => h("tr", {}, h("td", {}, x.id), h("td", {}, `${x.steps[0]} → ${x.command}`),
            h("td", {}, x.runs), h("td", {}, x.disabled_at ? `off after ${x.failures} failures` : "on"))))
      : h("div", { class: "empty" }, "none"));
}
