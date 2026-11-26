// The frame around every page: the navigation drawer on small screens, the light/dark switch, and "Search or jump"
// (Ctrl+K or /), which opens any page, any website's pages, or an action without hunting through the menu.
// ShellModel is plain data and functions (tested in tests/shell.test.cjs); everything under it touches the page.

const ShellModel = {
  PAGES: [
    ["Action Center", "#/", "home today next steps overview"],
    ["Alerts", "#/alerts", "warnings problems"],
    ["Growth", "#/growth", "search opportunities prospects rankings clicks"],
    ["Links", "#/links", "internal links approvals proposals link gaps"],
    ["Plan", "#/plan", "internal link plan prune"],
    ["Recovery", "#/recovery", "drop lost visibility cleanup"],
    ["Revival", "#/revival", "lost clicks traffic"],
    ["Fixes", "#/fixes", "technical fix list"],
    ["Requests", "#/requests", "request indexing search console"],
    ["Issues workspace", "/workspace", "queue urls issues"],
    ["Analytics", "#/overview", "overview indexed coverage trend"],
    ["Results", "#/results", "measured impact what worked"],
    ["Runs", "#/runs", "history log engine"],
    ["Connections", "#/connections", "google bing wordpress access check"],
    ["Settings", "#/settings", "websites schedule autopilot pause add site"],
  ],
  SITE_PAGES: [["overview", "site"], ["growth", "growth"], ["links", "links"], ["recovery", "recovery"], ["revival", "revival"],
    ["plan", "plan"], ["requests", "requests"]],

  /** Everything the box can open: pages, each website's pages, and actions. `sites` is a list of host names. */
  entries(sites) {
    const out = ShellModel.PAGES.map(([label, href, words]) => ({ kind: "page", label, href, words }));
    for (const site of sites || []) {
      for (const [name, route] of ShellModel.SITE_PAGES) {
        const href = route === "site" ? `#/site/${encodeURIComponent(site)}` : `#/${route}?site=${encodeURIComponent(site)}`;
        out.push({ kind: "site", label: `${site}: ${name}`, href, words: `website ${site.replace(/[.-]/g, " ")}`, site });
      }
      out.push({ kind: "action", label: `Run the engine for ${site}`, action: "run", site, words: "run now measure submit" });
    }
    out.push({ kind: "action", label: "Run the engine for all websites", action: "run", site: "", words: "run now measure submit" });
    out.push({ kind: "action", label: "Switch theme (light, dark, auto)", action: "theme", words: "dark mode light appearance" });
    out.push({ kind: "action", label: "Sign out", action: "logout", words: "log out exit" });
    return out;
  },

  /** The entries matching what was typed, best first. Every typed word must appear in the label or its keywords.
      A label that starts with the text ranks first, then one with a word starting with it, then the rest; ties keep
      the menu's order. An empty box shows the pages and the general actions. */
  match(entries, text, limit = 12) {
    const words = String(text || "").toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return entries.filter((e) => e.kind === "page" || (e.kind === "action" && !e.site)).slice(0, limit);
    const scored = [];
    entries.forEach((e, i) => {
      const label = e.label.toLowerCase();
      const hay = `${label} ${e.words || ""}`;
      if (!words.every((w) => hay.includes(w))) return;
      const first = words[0];
      const score = label.startsWith(first) ? 0 : new RegExp(`(^|[^a-z0-9])${first.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`).test(label) ? 1 : label.includes(first) ? 2 : 3;
      scored.push([score, i, e]);
    });
    return scored.sort((a, b) => a[0] - b[0] || a[1] - b[1]).slice(0, limit).map((x) => x[2]);
  },

  /** light -> dark -> auto -> light. */
  nextTheme: (now) => (now === "light" ? "dark" : now === "dark" ? "auto" : "light"),
};

if (typeof document !== "undefined") (() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  /* ---------------------------------------------------------------- navigation drawer (small screens) */
  const side = $("side"), shade = $("side-shade"), menu = $("menu-btn");
  const drawer = (open) => {
    side.classList.toggle("open", open);
    shade.hidden = !open;
    menu.setAttribute("aria-expanded", String(open));
  };
  menu.addEventListener("click", () => drawer(!side.classList.contains("open")));
  shade.addEventListener("click", () => drawer(false));
  side.addEventListener("click", (e) => { if (e.target.closest("a")) drawer(false); });

  /* ---------------------------------------------------------------- theme */
  const themeBtn = $("theme-btn");
  const savedTheme = () => { try { return localStorage.getItem("ie-theme") || "auto"; } catch { return "auto"; } };
  const showTheme = (t) => { themeBtn.textContent = `Theme: ${t}`; };
  const setTheme = (t) => {
    if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
    try { if (t === "auto") localStorage.removeItem("ie-theme"); else localStorage.setItem("ie-theme", t); } catch { /* a preference only */ }
    showTheme(t);
  };
  showTheme(savedTheme());
  themeBtn.addEventListener("click", () => setTheme(ShellModel.nextTheme(savedTheme())));

  /* ---------------------------------------------------------------- search or jump */
  const dlg = $("jump-dlg"), input = $("jump-input"), list = $("jump-list");
  let shown = [], active = 0;
  const siteNames = () => {            // app.js keeps the loaded data in S (same page, so the name is in scope once it has run)
    try { return (S.summary && S.summary.sites ? S.summary.sites : []).map((s) => s.site); } catch { return []; }
  };
  const KIND = { page: "Page", site: "Website", action: "Action" };
  const paint = () => {
    shown = ShellModel.match(ShellModel.entries(siteNames()), input.value);
    active = Math.min(active, Math.max(0, shown.length - 1));
    list.replaceChildren(...(shown.length ? shown.map((e, i) => {
      const li = el("li", i === active ? "on" : "");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      li.id = `jump-${i}`;
      li.append(el("span", "jump-kind", KIND[e.kind]), el("span", "jump-label", e.label));
      li.addEventListener("click", () => go(e));
      li.addEventListener("mousemove", () => { if (active !== i) { active = i; paint(); } });
      return li;
    }) : [el("li", "none muted", "Nothing matches. Try a page name or part of a website's address.")]));
    input.setAttribute("aria-activedescendant", shown.length ? `jump-${active}` : "");
    const on = list.querySelector("li.on");
    if (on) on.scrollIntoView({ block: "nearest" });
  };
  const go = (e) => {
    dlg.close();
    if (e.action === "run") openRunDialog(e.site || "");
    else if (e.action === "theme") setTheme(ShellModel.nextTheme(savedTheme()));
    else if (e.action === "logout") $("logout").click();
    else if (e.href.startsWith("#")) { if (location.hash === e.href) render(); else location.hash = e.href; }
    else location.assign(e.href);
  };
  const openJump = () => { if (dlg.open) return; input.value = ""; active = 0; dlg.showModal(); paint(); input.focus(); };
  $("jump-btn").addEventListener("click", openJump);
  $("jump-btn-top").addEventListener("click", openJump);
  input.addEventListener("input", () => { active = 0; paint(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % Math.max(1, shown.length); paint(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = (active - 1 + shown.length) % Math.max(1, shown.length); paint(); }
    else if (e.key === "Enter") { e.preventDefault(); if (shown[active]) go(shown[active]); }
  });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });       // a click on the backdrop
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName || "") || (document.activeElement || {}).isContentEditable;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openJump(); }
    else if (e.key === "/" && !typing && !document.querySelector("dialog[open]")) { e.preventDefault(); openJump(); }
    else if (e.key === "Escape" && side.classList.contains("open")) drawer(false);
  });
  if (/Mac|iPhone|iPad/.test(navigator.platform || "")) for (const k of document.querySelectorAll(".jump kbd")) k.textContent = "⌘K";
})();

if (typeof module !== "undefined") module.exports = ShellModel;
