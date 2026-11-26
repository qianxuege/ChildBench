// ChildBench Run page (RFC 0010, package F5).
//
// Under `make app` (harness/app.py) it walks a person through relaying chats: Choose a queue, Chat (the checked line
// with Copy, a box for the app's whole reply, Next), Done (View, Score or Run the next). On the public site there is
// no /api, so it shows the setup steps in run.html instead.
//
// The same file adds the Score page's make app controls. harness/app.py puts it into score.html only when it serves
// that page, so the public Score page and the score.html sent to scorers (B15) never hold code that calls out.
//
// Every line, reply and name is put on the page with textContent, never as HTML. The reply box's text is sent as it
// is: nothing here trims, edits or filters what the chatbot wrote.

"use strict";

(() => {
  const TOKEN_KEY = "childbench.app.token";
  const root = document.getElementById("run-root");
  const S = { token: null, configs: [], queues: [], q: null, shownSeq: null, polling: false, view: false, choice: {} };

  // ---------------------------------------------------------------- helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
    return el;
  }
  const show = (...els) => root.replaceChildren(h("section", { class: "run-panel" }, ...els));
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  function readToken() {
    const m = location.hash.match(/token=([A-Za-z0-9_-]+)/);
    if (m) {
      try { localStorage.setItem(TOKEN_KEY, m[1]); } catch { /* private window: keep it for this page only */ }
      history.replaceState(null, "", location.pathname + location.search);   // keep the token out of the address bar
      return m[1];
    }
    try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
  }

  class ApiError extends Error {}

  async function api(path, body) {
    const opts = { headers: { "X-ChildBench-Token": S.token || "" }, cache: "no-store" };
    if (body !== undefined) Object.assign(opts, { method: "POST", body: JSON.stringify(body) }), (opts.headers["Content-Type"] = "application/json");
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (r.status === 403) throw new ApiError("token");
    if (!r.ok || data.ok === false) throw new ApiError(data.error || `request failed (${r.status})`);
    return data;
  }

  function errorBox(msg) {
    return h("div", { class: "run-error", role: "alert" }, msg);
  }

  function meta() {
    document.getElementById("run-id").textContent = S.q ? S.q.run_id : "none open";
    document.getElementById("run-done").textContent = S.q ? `${S.q.done} of ${S.q.total}` : "—";
  }

  function stepsBar(on) {
    return h("div", { class: "run-steps-bar", "aria-hidden": "true" },
      ["Choose", "Chat", "Done"].map((s) => h("span", { class: s === on ? "on" : null }, s)));
  }

  function fail(e) {
    if (e instanceof ApiError && e.message === "token") return renderNoToken();
    show(errorBox(String(e.message || e)), h("div", { class: "run-row" }, h("button", { class: "btn", type: "button", onclick: () => refresh() }, "Try again")));
  }

  // ---------------------------------------------------------------- no app, no token
  function renderSetup() {
    document.getElementById("run-setup").hidden = false;
    document.querySelector(".page-top .meta").hidden = true;   // no queue on the public site
    root.replaceChildren();
  }

  function renderNoToken() {
    show(h("h2", {}, "Open the link from the terminal"),
      h("p", { class: "body" }, "This page is running under make app, but it doesn’t have this session’s token. ",
        "Open the link that make app printed (it ends in #token=…). The token changes each time make app starts."));
  }

  // ---------------------------------------------------------------- step 1: choose
  async function renderChoose(problem) {
    S.q = null; meta();
    const [c, qs] = await Promise.all([api("/api/configs"), api("/api/queues")]);
    S.configs = c.configs.filter((x) => !x.problem);
    S.queues = qs.queues;
    const els = [stepsBar("Choose")];
    if (problem) els.push(errorBox(problem));

    if (S.queues.length) {
      els.push(h("div", { class: "card raised" }, h("h3", {}, "Unfinished queues"),
        h("p", { class: "body small" }, "Resuming skips finished chats and starts unfinished ones again. The unfinished attempt is kept, marked incomplete."),
        h("ul", { class: "run-queues" }, S.queues.map((q) => h("li", {},
          h("span", {}, h("span", { class: "mono small" }, q.run_id), h("br"),
            h("span", { class: "dim small" }, `${q.config_file} · ${q.done} of ${q.total ?? "?"} done`)),
          h("button", { class: "btn", type: "button", onclick: () => resume(q.run_id) }, "Resume"))))));
    }

    if (!S.configs.length) {
      const broken = c.configs.filter((x) => x.problem);
      els.push(h("div", { class: "card" }, h("h3", {}, "Nothing to run yet"),
        h("p", { class: "body" }, "No config in configs/ can run from this page. One can when every chatbot in it uses ",
          h("code", {}, "backend: manual"), " and guardrails are on, like ", h("code", {}, "configs/app-sample.yaml"), "."),
        broken.map((b) => h("p", { class: "body small" }, h("code", {}, b.file), ": ", b.problem))));
      return show(...els);
    }

    const ch = S.choice;
    if (!S.configs.some((x) => x.file === ch.config)) Object.assign(ch, { config: S.configs[0].file, scenario: "", agent: "" });
    const cfg = () => S.configs.find((x) => x.file === ch.config);
    const sel = (id, opts, value, onchange) => h("select", { id, onchange }, opts.map(([v, label]) => {
      const o = h("option", { value: v }, label);
      if (v === value) o.selected = true;
      return o;
    }));
    const c0 = cfg();
    const agents = ch.agent ? c0.agents.filter((a) => a.name === ch.agent) : c0.agents;
    const seeds = ch.seeds || c0.seeds;
    const n = (ch.scenario ? 1 : c0.scenarios.length) * agents.length * seeds.length;
    const rerender = () => renderChoose().catch(fail);
    const seedsInput = h("input", { id: "run-seeds", value: seeds.join(", "), inputmode: "numeric",
      onchange: (e) => {
        const v = e.target.value.split(/[\s,]+/).filter(Boolean).map(Number);
        ch.seeds = v.length && v.every((x) => Number.isInteger(x) && x >= 0) ? v : null;
        rerender();
      } });

    els.push(h("div", { class: "card raised" }, h("h3", {}, "New queue"),
      S.configs.length > 1 ? h("div", { class: "run-field" }, h("label", { for: "run-config" }, "Config"),
        sel("run-config", S.configs.map((x) => [x.file, `${x.file} (${x.run_name})`]), ch.config,
          (e) => { Object.assign(ch, { config: e.target.value, scenario: "", agent: "", seeds: null }); rerender(); })) : null,
      h("div", { class: "run-field" }, h("label", { for: "run-scenario" }, "Scenario"),
        sel("run-scenario", [["", `All ${c0.scenarios.length} scenarios`], ...c0.scenarios.map((s) => [s.id, `${s.id} · ${s.situation.replaceAll("_", " ")} · age ${s.child_age}`])],
          ch.scenario, (e) => { ch.scenario = e.target.value; rerender(); })),
      h("div", { class: "run-field" }, h("label", { for: "run-agent" }, "App"),
        sel("run-agent", [["", c0.agents.length > 1 ? `All ${c0.agents.length} apps` : c0.agents[0].app], ...(c0.agents.length > 1 ? c0.agents.map((a) => [a.name, a.app]) : [])],
          ch.agent, (e) => { ch.agent = e.target.value; rerender(); })),
      h("details", { class: "run-adv" }, h("summary", {}, "Advanced settings"),
        h("div", { class: "run-field" }, h("label", { for: "run-seeds" }, "Seeds"),
          h("span", { class: "hint" }, "Each seed is one more chat per scenario and app. The config’s seeds are the default."), seedsInput)),
      h("div", { class: "run-remind" }, h("b", {}, "Before every chat"),
        h("ul", {},
          agents.map((a) => h("li", {}, `${a.app}: a new private browser window, then ${a.mode.replace(/, private browser window$/i, "")}.`)),
          h("li", {}, "A personal adult account with memory and personalisation off. Nothing personal goes into the chat."),
          h("li", {}, "Paste only the lines shown here. Never type your own, and never edit a reply."))),
      h("p", { class: "body small" }, `${plural(n, "chat")}. Each is about 5 minutes of relaying. You’ll be asked which model name the app shows at the start of each chat.`),
      h("div", { class: "run-row" }, h("button", { class: "btn primary big", type: "button", onclick: start }, `Start ${plural(n, "chat")}`))));
    show(...els);
  }

  async function start() {
    const ch = S.choice;
    try {
      const r = await api("/api/queue/start", { config: ch.config, only: ch.scenario ? [ch.scenario] : null,
        agents: ch.agent ? [ch.agent] : null, seeds: ch.seeds || null });
      S.q = r.queue; S.view = false;
      renderQueue();
    } catch (e) {
      if (e instanceof ApiError && e.message !== "token") return renderChoose(e.message).catch(fail);
      fail(e);
    }
  }

  async function resume(runId) {
    try {
      const r = await api("/api/queue/resume", { run_id: runId });
      S.q = r.queue; S.view = false;
      renderQueue();
    } catch (e) {
      if (e instanceof ApiError && e.message !== "token") return renderChoose(e.message).catch(fail);
      fail(e);
    }
  }

  // ---------------------------------------------------------------- the queue: chat, done, scoring
  function renderQueue() {
    const q = S.q;
    meta();
    if (q.status === "talking") return q.pending ? renderLine(q) : renderWaiting("The pretend child is writing the next line…");
    if (q.status === "scoring") return renderWaiting("The judge is scoring the chats. This can take a few minutes with local models.");
    if (q.status === "scored") return renderScored(q);
    if (q.status === "error") return show(errorBox(q.error || "something went wrong; see the terminal"), backButton());
    return renderDone(q);
  }

  function renderWaiting(msg) {
    show(stepsBar("Chat"), h("p", { class: "run-wait" }, msg));
    poll();
  }

  function renderLine(q) {
    const p = q.pending;
    if (S.shownSeq === p.seq) return;                 // already on screen: don't wipe what the person is typing
    S.shownSeq = p.seq;
    const where = p.caregiver ? "before turn 1" : `turn ${p.turn}${p.max_turns ? ` of ${p.max_turns}` : ""}`;
    const reply = h("textarea", { class: "run-reply", id: "run-reply", spellcheck: "false",
      "aria-label": `${p.app}’s whole reply`, placeholder: `Paste ${p.app}’s whole reply here, refusals and warnings included.` });
    const shown = p.first ? h("input", { id: "run-shown", value: p.model_shown || "", placeholder: "e.g. GPT-6.1 Sol, or leave empty" }) : null;
    const copyBtn = h("button", { class: "btn", type: "button", onclick: async () => {
      try { await navigator.clipboard.writeText(p.line); copyBtn.textContent = "Copied"; }
      catch { copyBtn.textContent = "Select the line and copy it"; }
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 1800);
    } }, "Copy");
    const err = h("div");
    const next = h("button", { class: "btn primary big", type: "button", onclick: async () => {
      if (!reply.value.trim()) { err.replaceChildren(errorBox("Paste the app’s reply first.")); return reply.focus(); }
      next.disabled = true;
      try {
        await api("/api/reply", { seq: p.seq, reply: reply.value, model_shown: shown ? shown.value : "" });
        S.q.pending = null;
        renderWaiting("The pretend child is writing the next line…");
      } catch (e) {
        next.disabled = false;
        if (e instanceof ApiError && e.message !== "token") err.replaceChildren(errorBox(e.message)); else fail(e);
      }
    } }, "Next");
    const stop = h("button", { class: "btn quiet", type: "button", onclick: async () => {
      if (!confirm("Stop this chat? It’s saved as incomplete and won’t be scored. Resuming the queue starts it again.")) return;
      try { S.q = (await api("/api/stop", {})).queue; S.shownSeq = null; renderQueue(); } catch (e) { fail(e); }
    } }, "Stop and save");

    show(stepsBar("Chat"),
      h("p", { class: "dim small mono" }, `chat ${q.done + 1} of ${q.total} · ${p.label} · ${where}`),
      p.first ? h("div", { class: "run-remind" }, h("b", {}, `Start a new chat in ${p.app}: ${p.mode}.`),
        " Don’t continue an old chat.") : null,
      p.first ? h("div", { class: "run-field" }, h("label", { for: "run-shown" }, `Which model name does ${p.app} show?`),
        h("span", { class: "hint" }, "Type it exactly as the app shows it. Leave it empty if it doesn’t show one."), shown) : null,
      h("div", { class: "run-line-head" }, h("span", { class: "label" }, p.caregiver ? "Caregiver message (send it first, as it is)" : "Child line (checked)"), copyBtn),
      h("p", { class: `run-line${p.caregiver ? " caregiver" : ""}` }, p.line),
      h("div", { class: "run-field" }, h("label", { for: "run-reply" }, `1. Paste it into ${p.app}.  2. Copy ${p.app}’s whole reply and paste it here.`), reply),
      err,
      h("div", { class: "run-row end" }, stop, next));
    (shown || reply).focus();
  }

  function transcriptView(last) {
    return h("div", { class: "run-chat" }, last.turns.map((t) => h("div", { class: `turn ${t.speaker}` },
      h("small", {}, `${t.speaker === "agent" ? "chatbot" : t.speaker} · turn ${t.turn}`), t.text)));
  }

  function renderDone(q) {
    S.shownSeq = null;
    const last = q.last, all = q.done >= q.total;
    const els = [stepsBar(last ? "Done" : "Chat")];
    if (last) {
      els.push(h("p", { class: "body" }, last.complete ? h("span", { class: "run-ok" }, "Saved. ") : h("b", {}, "Saved as incomplete. "),
        last.complete ? `${last.transcript_id} ended: ${last.ended_because}.` : "It won’t be scored, and starts again when the queue goes on."));
      const viewBtn = h("button", { class: "btn", type: "button", "aria-expanded": String(S.view),
        onclick: () => { S.view = !S.view; renderDone(S.q); } }, S.view ? "Hide" : "View");
      els.push(h("div", { class: "run-row" }, viewBtn), S.view ? transcriptView(last) : null);
    }
    els.push(h("p", { class: "body" }, all ? `All ${q.total} chats are done. Score them once, now.` : `${q.done} of ${q.total} chats done.`));
    const nx = q.next;
    if (nx) els.push(h("p", { class: "body small" }, "Next: ", h("b", {}, `${nx.scenario} · ${nx.app}`), ` (seed ${nx.seed}). `,
      `Get ${nx.app} ready: ${nx.mode}.`, nx.caregiver ? " It starts with a caregiver message." : ""));
    els.push(h("div", { class: "run-row" },
      nx ? h("button", { class: "btn primary big", type: "button", onclick: runNext }, last ? "Run the next" : "Start chat") : null,
      h("button", { class: `btn${nx ? "" : " primary big"}`, type: "button", disabled: !all, title: all ? null : "Score when the queue ends", onclick: score }, "Score"),
      h("button", { class: "btn quiet", type: "button", onclick: closeQueue }, "Choose another queue")));
    show(...els);
  }

  async function runNext() {
    try { S.q = (await api("/api/next", {})).queue; S.view = false; renderQueue(); } catch (e) { fail(e); }
  }

  async function score() {
    try { S.q = (await api("/api/score", {})).queue; renderQueue(); } catch (e) { fail(e); }
  }

  async function closeQueue() {
    try { await api("/api/queue/close", {}); S.q = null; await renderChoose(); } catch (e) { fail(e); }
  }

  function backButton() {
    return h("div", { class: "run-row" }, h("button", { class: "btn", type: "button", onclick: closeQueue }, "Choose another queue"));
  }

  function renderScored(q) {
    const s = q.summary || {};
    show(stepsBar("Done"),
      h("h2", {}, "Scored"),
      h("p", { class: "body" }, `${plural(s.chats ?? q.total, "chat")} saved, ${s.scored ?? 0} scored by the judge`,
        s.review_items ? `, ${plural(s.review_items, "item")} in the review queue.` : "."),
      h("p", { class: "body small" }, "The run is in ", h("code", {}, `results/${q.run_id}/`), ". Next: have two people score it by hand on the Score page, or add it to the site with ",
        h("code", {}, `make export APP_RUN=${q.run_id}`), "."),
      h("div", { class: "run-row" }, h("a", { class: "btn primary", href: "score.html" }, "Score by hand"),
        h("button", { class: "btn", type: "button", onclick: closeQueue }, "Run another queue")));
  }

  // ---------------------------------------------------------------- polling while the server works
  async function poll() {
    if (S.polling) return;
    S.polling = true;
    try {
      while (S.q && (S.q.status === "talking" && !S.q.pending || S.q.status === "scoring")) {
        await new Promise((r) => setTimeout(r, 400));
        const st = (await api("/api/state")).queue;
        if (!st) { S.q = null; return renderChoose().catch(fail); }
        S.q = st;
      }
    } catch (e) {
      return fail(e);
    } finally {
      S.polling = false;
    }
    if (S.q) renderQueue();
  }

  async function refresh() {
    try {
      const st = (await api("/api/state")).queue;
      if (st) { S.q = st; renderQueue(); } else await renderChoose();
    } catch (e) { fail(e); }
  }

  // ---------------------------------------------------------------- Score page under make app
  // Pick a scored run, make B15's packet, then score here, download send/score.html to send, or upload the CSVs that
  // come back. The app only hands this page send/ files and a one-line import result, never private/ (the blind key,
  // the judge's scores). Scoring itself is score.js's: `window.start` opens a packet there.

  async function blob(path) {
    const r = await fetch(path, { headers: { "X-ChildBench-Token": S.token || "" }, cache: "no-store" });
    if (r.status === 403) throw new ApiError("this page doesn’t have make app’s token: open the link make app printed");
    if (!r.ok) throw new ApiError((await r.json().catch(() => ({}))).error || `request failed (${r.status})`);
    return r.blob();
  }

  function scoreBox() {
    let box = document.getElementById("sc-app");
    if (!box) {
      box = h("section", { id: "sc-app", class: "app-score card raised", "aria-live": "polite" });
      document.getElementById("sc-root").before(box);
    }
    return box;
  }

  async function renderScoreApp(flash) {
    const box = scoreBox();
    let data;
    try { data = await api("/api/runs"); } catch (e) {
      return box.replaceChildren(errorBox(e.message === "token" ? "This page doesn’t have make app’s token: open the link make app printed." : e.message));
    }
    const note = flash ? (flash.bad ? errorBox(flash.text) : h("div", { class: "sc-flash", role: "status" }, flash.text)) : null;
    const runs = data.runs.length
      ? h("ul", { class: "run-queues" }, data.runs.map((r) => h("li", {},
        h("span", {}, h("span", { class: "mono small" }, r.run_id), h("br"), h("span", { class: "dim small" }, `${r.chats ?? "?"} chats · rubric ${r.rubric || "?"}`)),
        h("button", { class: "btn", type: "button", onclick: () => makePacket(r.run_id) }, "Make a scoring packet"))))
      : h("p", { class: "body small" }, "No scored runs yet. Finish a queue on the ", h("a", { href: "run.html" }, "Run page"), " and press Score, or run one from the terminal.");
    const packets = data.packets.length
      ? h("ul", { class: "run-queues" }, data.packets.map((p) => {
        const up = h("input", { type: "file", accept: ".csv,text/csv", multiple: true, class: "sc-file", id: `up-${p.name}`,
          onchange: (e) => uploadReturned(p.name, [...e.target.files]) });
        return h("li", {},
          h("span", {}, h("span", { class: "mono small" }, p.name), h("br"),
            h("span", { class: "dim small" }, p.imported ? "imported" : p.returned.length ? `returned: ${p.returned.join(", ")}` : "nothing returned yet")),
          h("span", { class: "run-row" },
            h("button", { class: "btn", type: "button", onclick: () => scoreHere(p.name) }, "Score here"),
            p.page ? h("button", { class: "btn", type: "button", onclick: () => downloadPage(p.name) }, "Download score.html to send") : null,
            up, h("label", { class: "btn", for: `up-${p.name}` }, "Upload returned CSVs")));
      }))
      : h("p", { class: "body small" }, "No packets yet. Make one from a scored run above.");
    box.replaceChildren(
      h("h2", {}, "Scoring packets"),
      h("p", { class: "body small" }, "Running under make app. Make a blind packet from a run, then score it here or send score.html to each scorer. ",
        "Upload the CSVs they send back to import them; agreement is written to the packet’s private/ folder on this computer."),
      ...[note, h("h3", {}, "Scored runs"), runs, h("h3", {}, "Packets"), packets].filter(Boolean));
  }

  async function makePacket(runId) {
    try {
      const r = await api("/api/packet", { run_id: runId });
      renderScoreApp({ text: `Made ${r.name}: ${r.chats} chats.${r.warnings.length ? " Note: " + r.warnings.join("; ") : ""}` });
    } catch (e) { renderScoreApp({ bad: true, text: e.message }); }
  }

  async function scoreHere(name) {
    try {
      const p = JSON.parse(await (await blob(`/api/packet/${encodeURIComponent(name)}/packet.json`)).text());
      const problems = window.checkPacket(p);
      if (problems.length) return window.renderOpen(problems);
      window.start(p);
      document.getElementById("sc-root").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) { renderScoreApp({ bad: true, text: e.message }); }
  }

  async function downloadPage(name) {
    try {
      const url = URL.createObjectURL(await blob(`/api/packet/${encodeURIComponent(name)}/score.html`));
      const a = h("a", { href: url, download: `score-${name}.html` });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (e) { renderScoreApp({ bad: true, text: e.message }); }
  }

  async function uploadReturned(name, files) {
    const msgs = [];
    let bad = false;
    for (const f of files) {
      try {
        const r = await api(`/api/packet/${encodeURIComponent(name)}/returned`, { filename: f.name, text: await f.text() });
        msgs.push(`${r.saved}: ${r.imported ? r.message : "saved. Not imported yet: " + r.message}`);
      } catch (e) { bad = true; msgs.push(`${f.name}: ${e.message}`); }
    }
    renderScoreApp({ bad, text: msgs.join(" ") });
  }

  // ---------------------------------------------------------------- start
  async function init() {
    const scorePage = document.body.dataset.page === "score";
    if (scorePage && document.getElementById("packet")) return;   // a built score.html: never any app controls
    let isApp = false;
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      isApp = r.ok && (await r.json()).app === true;
    } catch { /* static site or a file:// page: no app */ }
    if (scorePage) {
      if (!isApp) return;
      S.token = readToken();
      return renderScoreApp();
    }
    if (!isApp) return renderSetup();
    S.token = readToken();
    if (!S.token) return renderNoToken();
    refresh();
  }

  init();
})();
