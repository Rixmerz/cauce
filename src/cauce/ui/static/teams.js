import { get } from "./net.js";
import { family, h, kindChip, repoName } from "./util.js";

const chips = (values, cls = "chip") => (values || []).map((v) => h("span", { class: cls }, v));

function field(label, values, extra) {
  if (!values || !values.length) return null;
  return h("div", { class: "meta" }, h("span", { class: "muted" }, `${label} `), chips(values), extra || null);
}

const kinds = (label, values) => (values && values.length
  ? h("div", { class: "meta" }, h("span", { class: "muted" }, `${label} `), values.map(kindChip)) : null);

// One role in full: what it is for, what it carries, and what a worker is told.
function roleCard(role) {
  const origin = role.scope === "user" ? (role.shadows ? "yours, in place of the bundled one" : "yours") : "bundled";
  return h("div", { class: `card role k-${family(role.kinds[0])}` },
    h("div", { class: "title" }, role.name, " ", h("span", { class: "chip" }, origin)),
    h("div", {}, role.description),
    kinds("kinds", role.kinds),
    field("skills", role.carried),
    field("reads when it applies", role.on_demand),
    field("capabilities", role.capabilities),
    field("tools taken away", role.disallowed_tools),
    field("specializes on", role.match),
    role.missing && role.missing.length
      ? h("div", { class: "meta warn" }, `skills not found: ${role.missing.join(", ")}`) : null,
    role.derived_from ? h("div", { class: "meta muted" }, `copied from ${role.derived_from}`) : null,
    role.extends ? h("div", { class: "meta muted" }, `builds on ${role.extends}`) : null,
    role.teams && role.teams.length ? field("in teams", role.teams) : null,
    h("details", {}, h("summary", {}, "instructions"), h("pre", {}, role.prompt || "(none of its own)")),
    h("div", { class: "meta muted" }, role.path));
}

function teamSection(team, roles, here, open) {
  const byName = new Map(roles.map((r) => [r.name, r]));
  const mine = team.name === here;
  return h("details", { class: mine ? "team here" : "team", open: open || null },
    h("summary", {}, h("span", { class: "team-name" }, team.name), mine ? " — this project's team" : "",
      " ", h("span", { class: "chip" }, team.scope === "bundled" ? "bundled" : "yours"),
      " ", h("span", { class: "muted" }, `${team.roles.length} roles`)),
    team.description ? h("p", {}, team.description) : null,
    kinds("covers", team.kinds),
    team.projects && team.projects.length
      ? h("div", { class: "meta" }, h("span", { class: "muted" }, "used by "), chips(team.projects.map(repoName)))
      : null,
    team.missing && team.missing.length
      ? h("div", { class: "meta warn" }, `names roles that are not there: ${team.missing.join(", ")}`) : null,
    h("div", { class: "cards" }, team.roles.map((r) => roleCard(byName.get(r.name) || r))));
}

// The teams at a glance: who is in each, what it covers, where it is used.
function overview(list, here) {
  return h("table", {}, h("tr", {}, h("th", {}, "team"), h("th", {}, "roles"), h("th", {}, "covers"),
      h("th", {}, "used by")),
    list.map((t) => h("tr", {},
      h("td", {}, h("strong", {}, t.name), t.name === here ? " ★" : "", " ",
        h("span", { class: "chip" }, t.scope === "bundled" ? "bundled" : "yours")),
      h("td", {}, t.roles.map((r) => r.name).join(", ")),
      h("td", {}, h("span", { class: "row" }, t.kinds.map(kindChip))),
      h("td", {}, t.scope === "bundled" ? "every project without a team of its own"
        : (t.projects.length ? t.projects.map(repoName).join(", ") : "no project yet")))));
}

// Every team the person keeps and every role in them: one machine runs
// several projects, each with its own team. The team a project uses is chosen
// in a session there (`cauce team use <name>`); this page only reads.
export async function renderTeams(ctx) {
  const d = await get(`/api/teams?${new URLSearchParams({ repo: ctx.project || "" })}`);
  const ordered = [...d.teams].sort((a, b) => (b.name === d.here) - (a.name === d.here));
  const inTeams = new Set(d.teams.flatMap((t) => t.roles.map((r) => r.name)));
  const loose = d.roles.filter((r) => !inTeams.has(r.name));
  const refused = Object.entries(d.refused || {});
  return h("div", {},
    h("p", { class: "empty" },
      "A project's workers run only as the roles of its team, and a task is classified only into the kinds that team "
      + "covers. Make a team with cauce team create, give it to a project with cauce team use there, and copy a role "
      + "to tune it with cauce roles copy. In a session, /cauce:personal-team builds teams from how you work."),
    overview(ordered, d.here),
    ordered.map((t, i) => teamSection(t, d.roles, d.here, t.name === d.here || (!d.here && i === 0))),
    loose.length ? h("h2", { class: "section" }, "Roles in no team") : null,
    loose.length ? h("div", { class: "cards" }, loose.map(roleCard)) : null,
    refused.length ? h("h2", { class: "section" }, "Refused") : null,
    refused.length ? h("ul", {}, refused.map(([name, why]) => h("li", {}, `${name}: ${why}`))) : null);
}
