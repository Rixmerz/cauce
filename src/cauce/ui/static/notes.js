// One project's notes: what it knows, by topic, with the links between notes and
// the code each one is about. Read here; written by workers, sessions and `cauce note`.
import { get, post } from "./net.js";
import { h, when } from "./util.js";

const STATES = [["", "live"], ["review", "to review"], ["replaced", "replaced"], ["dropped", "dropped"]];

function anchors(note) {
  if (!note.anchors.length) return null;
  return h("div", { class: "meta" }, "about ",
    note.anchors.map((a) => h("code", { class: "anchor" }, a.symbol ? `${a.symbol} (${a.path})` : a.path)));
}

function links(note, focus) {
  if (!note.links.length) return null;
  return h("div", { class: "links" }, note.links.map((x) => h("button", {
    class: "link", title: x.title, onclick: () => focus(x.to),
  }, x.out ? `${x.kind} → #${x.to} ${x.title}` : `#${x.to} ${x.title} → ${x.kind} this`)));
}

function actions(note, ctx, reload) {
  const out = [];
  if (note.state === "review") {
    out.push(h("button", { onclick: async () => {
      await post(`/api/notes/${note.id}/ok`);
      ctx.toast(`#${note.id} is current again`);
      reload();
    } }, "Checked: still true"));
  }
  if (note.state === "current" || note.state === "review") {
    const drop = h("button", { class: "danger", onclick: async () => {
      if (drop.dataset.armed !== "1") {
        drop.dataset.armed = "1";
        drop.textContent = "Drop it? Click again";
        setTimeout(() => { drop.dataset.armed = ""; drop.textContent = "Drop"; }, 3000);
        return;
      }
      await post(`/api/notes/${note.id}/drop`);
      ctx.toast(`#${note.id} dropped`);
      reload();
    } }, "Drop");
    out.push(drop);
  }
  return out.length ? h("div", { class: "row" }, out) : null;
}

function card(note, ctx, reload, focus) {
  const showText = !note.text.startsWith(note.title.replace(/…$/, ""));
  return h("div", { class: `note state-${note.state}`, id: `note-${note.id}` },
    h("div", { class: "row" },
      h("b", {}, `#${note.id} ${note.title}`),
      h("span", { class: "chip" }, note.topic),
      note.state !== "current" ? h("span", { class: `tag ${note.state}` }, note.state) : null,
      note.why ? h("span", { class: "meta" }, note.why) : null),
    showText ? h("div", { class: "text" }, note.text) : null,
    note.state === "review" && note.state_reason ? h("div", { class: "asks" }, `to review: ${note.state_reason}`) : null,
    anchors(note),
    links(note, focus),
    h("div", { class: "meta" }, `${note.author} · filed by ${note.filed_by}${note.source ? ` · ${note.source}` : ""} · ${when(note.updated_at)}`),
    actions(note, ctx, reload));
}

export async function renderNotes(ctx) {
  let topic = "";
  let state = "";
  let query = "";
  const side = h("div", { class: "topics" });
  const list = h("div", { class: "notes" });
  const extra = h("div", {});

  const focus = (id) => {
    const el = document.getElementById(`note-${id}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("focused");
      setTimeout(() => el.classList.remove("focused"), 1500);
    } else {
      query = "";
      topic = "";
      load().then(() => focus(id));
    }
  };

  const load = async () => {
    const params = new URLSearchParams({ repo: ctx.project, topic, q: query, state });
    const data = await get(`/api/notes?${params}`);
    side.replaceChildren(
      h("button", { class: topic === "" ? "topic on" : "topic", onclick: () => { topic = ""; load(); } }, "every topic"),
      ...data.topics.map((t) => h("button", {
        class: topic === t.name ? "topic on" : "topic", title: t.description,
        onclick: () => { topic = t.name; load(); },
      }, `${t.name} ${t.live}`, t.review ? h("span", { class: "tag review" }, `${t.review} to review`) : null)));
    list.replaceChildren(...(data.notes.length
      ? data.notes.map((n) => card(n, ctx, load, focus))
      : [h("div", { class: "empty" }, query ? "No note matches." : "No notes here yet. Workers keep what they learned when they pass, sessions keep what they established before they compact, and `cauce note \"<fact>\" --topic <t>` keeps one by hand.")]));
    extra.replaceChildren(data.proposals.length ? h("div", { class: "section" }, h("h2", {}, "Topics Haiku proposed"),
      h("p", { class: "empty" }, "Filed under the closest topic for now. Add one with cauce notes topic add <name> \"<what it holds>\", then move notes to it."),
      data.proposals.map((p) => h("div", { class: "meta" }, `${p.topic} — for #${p.id} ${p.title}`))) : "");
  };

  const input = h("input", { placeholder: "Ask this project's notes…", "aria-label": "search notes" });
  const states = h("select", { "aria-label": "state", onchange: () => { state = states.value; load(); } },
    STATES.map(([value, label]) => h("option", { value }, label)));
  const form = h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); query = input.value; load(); } },
    input, h("button", { type: "submit" }, "Recall"), states);
  await load();
  return h("div", {}, form, h("div", { class: "notes-layout" }, side, list), extra);
}
