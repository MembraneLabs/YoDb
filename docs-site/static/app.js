const escapeHtml = (value) => value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");

function inline(value) {
  let text = escapeHtml(value);
  text = text.replace(/`([^`]+)`/g, "<code>$1</code>");
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => {
    const path = url.replace(/^\//, "").replace(/\.mdx?$/, "");
    return url.startsWith("/") ? `<a href="#/${path}">${label}</a>` : `<a href="${url}">${label}</a>`;
  });
}

function parseFrontmatter(markdown) {
  if (!markdown.startsWith("---\n")) return [{}, markdown];
  const end = markdown.indexOf("\n---", 4);
  if (end < 0) return [{}, markdown];
  const metadata = {};
  markdown.slice(4, end).split("\n").forEach((line) => {
    const [key, ...value] = line.split(":");
    if (key && value.length) metadata[key.trim()] = value.join(":").trim();
  });
  return [metadata, markdown.slice(end + 4).trim()];
}

function render(markdown) {
  const [meta, body] = parseFrontmatter(markdown);
  const lines = body.split("\n");
  const output = meta.title ? [`<h1>${inline(meta.title)}</h1>`, meta.description ? `<p class="description">${inline(meta.description)}</p>` : ""] : [];
  let paragraph = [], list = [], code = null, table = [];
  const flushParagraph = () => { if (paragraph.length) output.push(`<p>${inline(paragraph.join(" "))}</p>`); paragraph = []; };
  const flushList = () => { if (list.length) output.push(`<ul>${list.map((item) => `<li>${inline(item)}</li>`).join("")}</ul>`); list = []; };
  const flushTable = () => { if (table.length) { const rows = table.filter((row) => !/^\|?\s*[-:]+/.test(row.replaceAll("|", "").trim())); const cells = (row) => row.split("|").slice(1, -1).map((cell) => `<td>${inline(cell.trim())}</td>`).join(""); if (rows.length) output.push(`<table><tbody>${rows.map((row, i) => `<tr>${i === 0 ? cells(row).replaceAll("<td>", "<th>").replaceAll("</td>", "</th>") : cells(row)}</tr>`).join("")}</tbody></table>`); table = []; } };
  for (const line of lines) {
    if (line.startsWith("```")) { flushParagraph(); flushList(); flushTable(); if (code === null) code = []; else { output.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`); code = null; } continue; }
    if (code !== null) { code.push(line); continue; }
    if (/^<CardGroup/.test(line) || /^<\/CardGroup/.test(line)) continue;
    const card = line.match(/^\s*<Card title="([^"]+)"[^>]*href="([^"]+)">/);
    if (card) { flushParagraph(); output.push(`<a class="card" href="#/${card[2].replace(/^\//, "")}"><strong>${inline(card[1])}</strong>`); continue; }
    if (/^\s*<\/Card>/.test(line)) { output.push("</a>"); continue; }
    if (line.startsWith("|")) { flushParagraph(); flushList(); table.push(line); continue; } else flushTable();
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) { flushParagraph(); flushList(); output.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); continue; }
    const item = line.match(/^[-*]\s+(.+)$/);
    if (item) { flushParagraph(); list.push(item[1]); continue; }
    if (!line.trim()) { flushParagraph(); flushList(); continue; }
    paragraph.push(line.trim());
  }
  flushParagraph(); flushList(); flushTable();
  return output.join("\n");
}

async function start() {
  const config = await fetch("site.config.json").then((response) => response.json());
  document.title = `${config.site_name} documentation`;
  const nav = document.querySelector("#navigation");
  const content = document.querySelector("#content");
  const menu = document.querySelector(".menu-toggle");
  const pages = config.navigation.flatMap((group) => group.pages);
  nav.innerHTML = config.navigation.map((group) => `<section class="nav-group"><h2>${group.label}</h2>${group.pages.map((page) => `<a href="#/${page.path}" data-page="${page.path}">${page.label}</a>`).join("")}</section>`).join("");
  menu.addEventListener("click", () => { nav.classList.toggle("open"); menu.setAttribute("aria-expanded", nav.classList.contains("open")); });
  async function load() {
    const requested = location.hash.replace(/^#\//, "") || "index";
    const page = pages.find((entry) => entry.path === requested) || pages[0];
    document.querySelectorAll("[data-page]").forEach((link) => link.classList.toggle("active", link.dataset.page === page.path));
    nav.classList.remove("open"); menu.setAttribute("aria-expanded", "false");
    try { content.innerHTML = render(await fetch(`content/${page.path}.mdx`).then((response) => { if (!response.ok) throw new Error("missing"); return response.text(); })); content.focus(); }
    catch { content.innerHTML = "<h1>Documentation unavailable</h1><p>Build the site again to include the requested page.</p>"; }
  }
  addEventListener("hashchange", load); await load();
}
start();
