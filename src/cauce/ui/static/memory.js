import { get } from "./net.js";
import { h, repoName, when } from "./util.js";

function problem(p) {
  return h("div", { class: "problem" },
    h("div", { class: "row" }, h("b", {}, p.title), h("span", { class: `chip` }, p.state), h("span", { class: "meta" }, repoName(p.repo))),
    p.symptom ? h("div", { class: "meta" }, p.symptom.slice(0, 300)) : null,
    p.fixes.map((f) => {
      const disproved = f.outcome === "worked" && f.invalidated_on;
      const tag = disproved ? "disproved" : f.outcome;
      return h("div", { class: "fix" }, h("span", { class: `tag ${tag}` }, tag), f.description,
        f.why ? h("span", { class: "meta" }, ` — ${f.why}`) : null,
        f.commit_sha ? h("span", { class: "chip" }, f.commit_sha.slice(0, 8)) : null,
        h("span", { class: "meta" }, ` ${when(f.created_at)}`));
    }));
}

export async function renderMemory() {
  const results = h("div", {});
  const load = async (q) => { results.replaceChildren(...(await get(`/api/memory?q=${encodeURIComponent(q)}`)).map(problem)); };
  const input = h("input", { placeholder: "Search problems and fixes across every repository…", "aria-label": "search" });
  const form = h("form", { class: "row", onsubmit: (e) => { e.preventDefault(); load(input.value); } }, input,
    h("button", { type: "submit" }, "Search"));
  await load("");
  if (!results.childElementCount) results.append(h("div", { class: "empty" }, "no problems recorded yet"));
  return h("div", {}, form, h("div", { class: "section" }, results));
}
