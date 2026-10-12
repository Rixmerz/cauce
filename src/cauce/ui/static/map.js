// One project, mapped from what cauce keeps — no model draws it:
// - the mind map: topics, each live note, the code it is about, and the problems worked
//   on here with every fix tried; the typed links between notes cross the tree;
// - the milestones: workflow runs that finished, decisions, features and refactors that passed.
// A note picked on the map can be carried into a workflow step's brief (a person's press).
import { get, post } from "./net.js";
import { curve, label, s } from "./svg.js";
import { h, repoName, short, when } from "./util.js";

const COL = 230, ROW = 26, W = 200, BOX = 20, PAD = 10;
const FOLDABLE = new Set(["project", "topic", "problem", "note"]);

/** Leaves take a row each; a parent sits halfway between its first and last child. */
function layout(nodes, collapsed) {
  const kids = {};
  for (const n of nodes) if (n.parent) (kids[n.parent] ||= []).push(n);
  const at = {};
  let row = 0;
  let deepest = 0;
  const place = (n, depth) => {
    deepest = Math.max(deepest, depth);
    const children = collapsed.has(n.id) ? [] : (kids[n.id] || []);
    if (!children.length) {
      at[n.id] = { x: PAD + depth * COL, y: PAD + row * ROW };
      row += 1;
      return;
    }
    children.forEach((c) => place(c, depth + 1));
    const first = at[children[0].id].y;
    const last = at[children[children.length - 1].id].y;
    at[n.id] = { x: PAD + depth * COL, y: (first + last) / 2 };
  };
  const root = nodes.find((n) => !n.parent);
  if (root) place(root, 0);
  return { at, kids, width: PAD * 2 + deepest * COL + W, height: PAD * 2 + Math.max(row, 1) * ROW };
}

/** What survives a filter: nodes whose label matches, with every ancestor. */
function pruned(nodes, query) {
  const q = query.trim().toLowerCase();
  if (!q) return nodes;
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const keep = new Set();
  for (const n of nodes) {
    if (!`${n.label} ${n.text || ""}`.toLowerCase().includes(q)) continue;
    for (let at = n; at && !keep.has(at.id); at = byId[at.parent]) keep.add(at.id);
  }
  return nodes.filter((n) => keep.has(n.id));
}

function attachForm(note, ctx, flows) {
  const usable = flows.filter((d) => d.steps?.length && !d.error);
  if (!usable.length) return h("div", { class: "meta" }, "No workflow to attach it to here.");
  const wf = h("select", { "aria-label": "workflow" }, usable.map((d) => h("option", { value: `${d.scope}|${d.name}` },
    `${d.name} (${d.scope})`)));
  const step = h("select", { "aria-label": "step" });
  const fill = () => {
    const [scope, name] = wf.value.split("|");
    const d = usable.find((x) => x.name === name && x.scope === scope);
    step.replaceChildren(...d.steps.map((st) => h("option", { value: st.id }, st.title || st.id)));
  };
  wf.addEventListener("change", fill);
  fill();
  const go = h("button", { class: "primary", onclick: async () => {
    const [scope, name] = wf.value.split("|");
    go.disabled = true;
    try {
      const saved = await post("/api/workflows/attach",
        { repo: ctx.project, workflow: name, scope, step: step.value, note: note.ref });
      ctx.toast(saved.held_by.length
        ? `In use by run ${saved.held_by.map((r) => `#${r}`).join(", ")}: kept as a pending change, applied when they end`
        : `#${note.ref} now goes into ${name} › ${step.value}${scope === "bundled" ? " (copied to your workflows)" : ""}`);
    } catch (err) {
      ctx.toast(err.message);
    } finally {
      go.disabled = false;
    }
  } }, "Attach");
  return h("div", { class: "attach" }, h("div", { class: "meta" }, "Carry this note into a workflow step's brief:"),
    h("div", { class: "row" }, wf, step, go));
}

function detail(node, ctx, flows, edges, byId) {
  if (!node) return h("div", { class: "empty" }, "Pick a note or a problem on the map.");
  const linked = edges.filter((e) => e.from === node.id || e.to === node.id).map((e) => {
    const other = byId[e.from === node.id ? e.to : e.from];
    return h("div", { class: "meta" }, e.from === node.id ? `${e.kind} → ${other?.label}` : `${other?.label} → ${e.kind} this`);
  });
  return h("div", {},
    h("div", { class: "row" }, h("b", {}, node.ref ? `#${node.ref} ` : "", node.label),
      node.state ? h("span", { class: `tag ${node.state}` }, node.state) : null, h("span", { class: "chip" }, node.kind)),
    node.text ? h("div", { class: "text" }, node.text) : null,
    linked,
    node.task ? h("button", { onclick: () => ctx.openTask(node.task) }, `task #${node.task}`) : null,
    node.kind === "note" ? attachForm(node, ctx, flows) : null);
}

async function mindMap(ctx) {
  const [data, flows] = await Promise.all([
    get(`/api/map?repo=${encodeURIComponent(ctx.project)}`),
    get(`/api/workflows?repo=${encodeURIComponent(ctx.project)}`).then((w) => w.definitions).catch(() => []),
  ]);
  const byId = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
  const collapsed = new Set(data.nodes.filter((n) => n.kind === "note").map((n) => n.id));  // anchors folded at first
  let query = "";
  let picked = null;
  const canvas = h("div", { class: "map-scroll" });
  const side = h("aside", { class: "map-side" });

  const draw = () => {
    const nodes = pruned(data.nodes, query);
    const { at, kids, width, height } = layout(nodes, collapsed);
    const shown = (id) => at[id] !== undefined;
    const tree = nodes.filter((n) => n.parent && shown(n.id) && shown(n.parent)).map((n) => s("path", {
      class: "map-edge", d: curve(at[n.parent].x + W, at[n.parent].y + BOX / 2, at[n.id].x, at[n.id].y + BOX / 2) }));
    const cross = data.edges.filter((e) => shown(e.from) && shown(e.to)).map((e) => {
      const a = at[e.from], b = at[e.to];
      const bend = Math.max(40, Math.abs(b.y - a.y) / 2);
      return s("path", { class: `map-link ${e.kind}`,
        d: `M${a.x + W},${a.y + BOX / 2} C${a.x + W + bend},${a.y} ${b.x + W + bend},${b.y + BOX} ${b.x + W},${b.y + BOX / 2}` },
      s("title", {}, `${byId[e.from].label} — ${e.kind} → ${byId[e.to].label}`));
    });
    const boxes = nodes.filter((n) => shown(n.id)).map((n) => {
      const folds = FOLDABLE.has(n.kind) && (kids[n.id] || []).length;
      const hidden = folds && collapsed.has(n.id);
      const pick = () => {
        if (n.kind === "note" || n.kind === "problem" || n.kind === "fix") {
          picked = n;
          side.replaceChildren(detail(n, ctx, flows, data.edges, byId));
        }
        if (folds && n.kind !== "note") collapsed.has(n.id) ? collapsed.delete(n.id) : collapsed.add(n.id);
        if (folds && n.kind === "note" && picked === n && collapsed.has(n.id)) collapsed.delete(n.id);
        draw();
      };
      return s("g", { class: `map-node ${n.kind} ${n.state || ""}${picked?.id === n.id ? " picked" : ""}`,
        transform: `translate(${at[n.id].x},${at[n.id].y})`, tabindex: 0, role: "button", onclick: pick,
        onkeydown: (e) => { if (e.key === "Enter") pick(); } },
      s("title", {}, `${n.label}${n.text ? `\n\n${short(n.text, 400)}` : ""}`),
      s("rect", { width: W, height: BOX, rx: 6 }),
      label(`${hidden ? "▸ " : folds && n.kind !== "note" ? "▾ " : ""}${n.kind === "project" ? repoName(n.label) : n.label}`,
        30, { x: 8, y: 14 }));
    });
    canvas.replaceChildren(s("svg", { class: "map", width, height, viewBox: `0 0 ${width} ${height}`, role: "img",
      "aria-label": "project mind map" }, tree, cross, boxes));
  };

  const filter = h("input", { placeholder: "Filter the map…", "aria-label": "filter the map",
    oninput: () => { query = filter.value; draw(); } });
  const left = data.left_out.notes || data.left_out.problems
    ? h("div", { class: "meta" }, `Showing the newest: ${data.left_out.notes} notes and ${data.left_out.problems} problems left out.`)
    : null;
  side.replaceChildren(detail(null));
  draw();
  if (data.nodes.length <= 1) {
    return h("div", { class: "empty" }, "Nothing to map yet: notes and problems appear as work passes and sessions keep what they learn.");
  }
  return h("div", {}, h("div", { class: "toolbar" }, filter,
    h("span", { class: "meta" }, "click a topic or problem to fold it, a note to read it; dashed lines are links between notes")),
  left, h("div", { class: "map-layout" }, canvas, side));
}

async function milestones(ctx) {
  const list = await get(`/api/milestones?repo=${encodeURIComponent(ctx.project)}`);
  if (!list.length) {
    return h("div", { class: "empty" }, "No milestone yet: a workflow run that finishes, a decision a note keeps, a feature or refactor that passes.");
  }
  return h("ol", { class: "timeline" }, list.map((m) => h("li", {},
    h("time", { datetime: m.at }, when(m.at)),
    h("div", {}, h("div", { class: "row" }, h("span", { class: `chip ${m.kind}` }, m.kind),
      ["feature", "refactor", "plan"].includes(m.kind)
        ? h("button", { class: "linkish", onclick: () => ctx.openTask(m.ref) }, m.title) : h("b", {}, m.title)),
    m.detail ? h("div", { class: "meta" }, short(m.detail, 280)) : null))));
}

export async function renderMap(ctx) {
  let mode = "map";
  const body = h("div", {});
  const tabs = h("div", { class: "row segmented", role: "tablist" });
  const show = async () => {
    tabs.replaceChildren(...[["map", "Mind map"], ["milestones", "Milestones"]].map(([m, text]) =>
      h("button", { role: "tab", "aria-selected": String(mode === m), onclick: () => { mode = m; show(); } }, text)));
    body.replaceChildren(await (mode === "map" ? mindMap(ctx) : milestones(ctx)));
  };
  await show();
  return h("div", {}, tabs, body);
}
