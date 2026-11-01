// SVG built node by node, like h(): every label is model- or person-written text,
// set as textContent, never parsed as markup.
const NS = "http://www.w3.org/2000/svg";

export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, String(v));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** A label cut to `n` characters, its whole text in a <title> a pointer reads. */
export function label(text, n, attrs = {}) {
  const full = String(text ?? "");
  const cut = full.length > n ? `${full.slice(0, n - 1)}…` : full;
  return s("text", attrs, cut);
}

/** A curve from the right edge of one box to the left edge of the next. */
export function curve(x1, y1, x2, y2) {
  const mid = (x1 + x2) / 2;
  return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}
