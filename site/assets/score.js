// ChildBench scoring page (RFC 0010, package F4; RFC 0003 "Human labels").
//
// One scorer scores their own chats from a packet made by `make labels` (harness/labels.py, schema_version 1).
// The packet is either built into the page (send/score.html, <script type="application/json" id="packet">) or
// opened from a file. Nothing is sent anywhere: answers live in localStorage, per packet and scorer, and leave as
// a CSV in exactly the packet's csv_header columns, which `make import-labels` checks (harness/validate_judge.py).
//
// Every chat line and packet string is put on the page with textContent, never as HTML: a chatbot's reply is
// shown as it was written, and can't add markup to the page.
//
// The functions above the DOM section have no browser dependency, so tests/test_score_page.py runs them in node.

"use strict";

const STORE = "childbench.score.v1";
const NA = "-";                                  // the CSV value for a question that doesn't apply to a chat
const FIXED = ["packet_id", "rubric_hash", "scorer", "chat_id"];
const PACKET_KEYS = ["packet_id", "rubric_hash", "scorers", "assignment", "csv_header", "questions", "chats"];

// ------------------------------------------------------------------ packet, CSV (no DOM)

/** Problems that stop a packet from being used, as sentences; [] when it's fine. */
function checkPacket(p) {
  if (!p || typeof p !== "object" || Array.isArray(p)) return ["this isn't a scoring packet (packet.json from make labels)"];
  const missing = PACKET_KEYS.filter((k) => !(k in p));
  if (missing.length) return [`this isn't a scoring packet: it has no ${missing.join(", ")}`];
  if (p.schema_version !== 1) return [`this packet is schema_version ${p.schema_version}; this page reads version 1`];
  const out = [];
  const ids = new Set(p.chats.map((c) => c.id));
  for (const who of p.scorers) {
    const gone = (p.assignment[who] || []).filter((id) => !ids.has(id));
    if (gone.length) out.push(`scorer ${who}'s chats ${gone.join(", ")} aren't in the packet`);
  }
  return out;
}

/** The chats one scorer scores, in their order. */
function chatsFor(p, who) {
  const by = Object.fromEntries(p.chats.map((c) => [c.id, c]));
  return (p.assignment[who] || []).map((id) => by[id]).filter(Boolean);
}

function questionMap(p) {
  return Object.fromEntries(p.questions.map((q) => [q.id, q]));
}

/** Per chat, the applicable questions still without an answer: [{id, missing: [question ids]}], only chats with any. */
function blanks(p, who, answers) {
  return chatsFor(p, who)
    .map((c) => ({ id: c.id, missing: c.questions.filter((q) => !(answers[c.id] || {})[q]) }))
    .filter((b) => b.missing.length);
}

function csvCell(v) {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) || /^\s|\s$/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** The scorer's sheet: the packet's csv_header, one row per assigned chat, "-" where a question doesn't apply. */
function makeCsv(p, who, answers, notes) {
  const rows = [p.csv_header];
  for (const c of chatsFor(p, who)) {
    const a = answers[c.id] || {};
    const fixed = { packet_id: p.packet_id, rubric_hash: p.rubric_hash, scorer: who, chat_id: c.id, note: (notes[c.id] || "").trim() };
    rows.push(p.csv_header.map((col) => (col in fixed ? fixed[col] : c.questions.includes(col) ? a[col] || "" : NA)));
  }
  return "﻿" + rows.map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";   // the BOM makes Excel read UTF-8
}

/** RFC 4180 CSV, as a spreadsheet saves it: quotes, doubled quotes, line breaks inside quotes, CRLF or LF. */
function parseCsv(text) {
  const rows = [];
  let row = [], cell = "", quoted = false;
  text = text.replace(/^﻿/, "");
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { row.push(cell); cell = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(cell); rows.push(row); row = []; cell = "";
    } else cell += ch;
  }
  if (cell !== "" || row.length) { row.push(cell); rows.push(row); }
  return rows.filter((r) => r.some((x) => x.trim() !== ""));
}

/**
 * Read a sheet this scorer downloaded earlier (or filled in a spreadsheet), to carry on with it.
 * Returns {answers, notes, problems}. Problems that make the whole file wrong come back with empty answers;
 * a bad cell is listed and skipped, and the rest is kept.
 */
function readSheet(p, who, text) {
  const empty = (problems) => ({ answers: {}, notes: {}, problems });
  const rows = parseCsv(text);
  if (!rows.length) return empty(["the file is empty"]);
  const header = rows[0].map((h) => h.trim());
  if (header.length === 1 && /[;\t]/.test(header[0])) return empty(["columns aren't separated by commas; save it as CSV (comma)"]);
  const missing = p.csv_header.filter((h) => !header.includes(h));
  if (missing.length) return empty([`missing columns: ${missing.join(", ")}`]);
  const recs = rows.slice(1).map((r) => Object.fromEntries(header.map((h, i) => [h, (r[i] || "").trim()])));
  for (const [col, what] of [["packet_id", "it was made for another packet"], ["rubric_hash", "it was scored with another version of the rubric"]]) {
    const wrong = [...new Set(recs.map((r) => r[col]))].filter((v) => v !== p[col]);
    if (wrong.length) return empty([`${col} is ${wrong.map((x) => x || "blank").join(", ")}, not ${p[col]}: ${what}`]);
  }
  const others = [...new Set(recs.map((r) => r.scorer))].filter((v) => v !== who);
  if (others.length) return empty([`this sheet is for scorer ${others.map((x) => x || "blank").join(", ")}, not ${who}`]);
  const mine = Object.fromEntries(chatsFor(p, who).map((c) => [c.id, c]));
  const qs = questionMap(p);
  const answers = {}, notes = {}, problems = [];
  for (const r of recs) {
    const c = mine[r.chat_id];
    if (!c) { problems.push(`chat ${r.chat_id || "(blank)"} isn't one of yours; skipped`); continue; }
    if (answers[c.id]) { problems.push(`chat ${c.id} appears twice; kept the first`); continue; }
    answers[c.id] = {};
    for (const q of c.questions) {
      const v = (r[q] || "").toLowerCase();
      if (v === "" || v === NA) continue;
      if (qs[q] && qs[q].values.some((x) => x.value === v)) answers[c.id][q] = v;
      else problems.push(`chat ${c.id}, ${q}: '${r[q]}' isn't one of ${qs[q] ? qs[q].values.map((x) => x.value).join(", ") : "the answers"}; left blank`);
    }
    if (r.note) notes[c.id] = r.note;
  }
  return { answers, notes, problems };
}

if (typeof module !== "undefined") module.exports = { checkPacket, chatsFor, blanks, makeCsv, parseCsv, readSheet };

// ------------------------------------------------------------------ DOM helpers

/** Make an element. Strings become text nodes, never HTML. */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined && k !== false) el.append(k instanceof Node ? k : String(k));
  return el;
}

const $ = (sel) => document.querySelector(sel);

// localStorage can be off (private windows, some file:// settings). The page still works; it just can't remember.
const storage = {
  ok: (() => { try { const k = `${STORE}.probe`; localStorage.setItem(k, "1"); localStorage.removeItem(k); return true; } catch { return false; } })(),
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } },
  del(k) { try { localStorage.removeItem(k); } catch { /* nothing to do */ } },
};

// ------------------------------------------------------------------ page state

const S = {
  packet: null, embedded: false, who: null,
  answers: {}, notes: {}, cur: 0,
  tried: new Set(),          // chats the scorer has left, or tried to download with: their blanks are shown in amber
  saved: null,               // time of the last save, or false when storage failed
};

const scorerKey = () => `${STORE}.${S.packet.packet_id}.scorer`;
const workKey = () => `${STORE}.${S.packet.packet_id}.${S.who}`;
const mine = () => chatsFor(S.packet, S.who);

function save() {
  const ok = storage.set(workKey(), { answers: S.answers, notes: S.notes, cur: S.cur, tried: [...S.tried], at: new Date().toISOString() });
  S.saved = ok ? new Date() : false;
  const el = $("#sc-saved");
  if (el) el.textContent = savedText();
}

function savedText() {
  if (!storage.ok || S.saved === false) return "Not saved in this browser: download the CSV often.";
  if (!S.saved) return "Saved in this browser as you go.";
  return `Saved in this browser at ${S.saved.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`;
}

function load() {
  const w = storage.get(workKey()) || {};
  S.answers = w.answers && typeof w.answers === "object" ? w.answers : {};
  S.notes = w.notes && typeof w.notes === "object" ? w.notes : {};
  S.tried = new Set(Array.isArray(w.tried) ? w.tried : []);
  S.cur = Number.isInteger(w.cur) && w.cur >= 0 && w.cur < mine().length ? w.cur : 0;
  S.saved = null;
}

// ------------------------------------------------------------------ header

const NAV = [                                    // the same menu as site.js (F5)
  ["scenarios.html", "/scenarios"],
  ["run.html", "/run"],
  ["score.html", "/score"],
  ["leaderboard.html", "/results"],
];

function renderHeader() {
  const head = $("#site-head");
  // Built into send/score.html the page travels alone, so links to the rest of the site would lead nowhere.
  const nav = S.embedded ? h("span", { class: "dim mono small" }, "scoring packet · works offline")
    : h("nav", { class: "nav", "aria-label": "Sections" },
      NAV.map(([href, label]) => h("a", { href, "aria-current": href === "score.html" ? "page" : null }, label)));
  head.replaceChildren(h("div", { class: "wrap" }, S.embedded ? h("span", { class: "logo" }, "childbench") : h("a", { class: "logo", href: "index.html" }, "childbench"), nav));
}

function renderMeta() {
  $("#sc-packet-id").textContent = S.packet ? S.packet.packet_id : "none open";
  $("#sc-scorer-id").textContent = S.who || "—";
}

// ------------------------------------------------------------------ step 1: open a packet

function renderOpen(problems) {
  renderMeta();
  const input = h("input", { type: "file", accept: ".json,application/json", id: "sc-file", class: "sc-file", onchange: (e) => readPacketFile(e.target.files[0]) });
  const drop = h("label", { class: "sc-drop", for: "sc-file" },
    h("b", {}, "Open the packet"), h("span", { class: "body" }, "Choose packet.json from the send/ folder, or drop it here. It’s read in this browser and never uploaded."));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); readPacketFile(e.dataTransfer.files[0]); });
  $("#sc-root").replaceChildren(h("section", { class: "sc-step" },
    problems && problems.length ? errorBox("This file can’t be used", problems) : null,
    input, drop,
    h("p", { class: "body small" }, "Scorers normally get score.html with their packet already inside, and don’t need this step. ",
      "To make a packet, run ", h("code", {}, "make labels"), "; it writes results/labeling/<name>/send/.")));
}

function readPacketFile(file) {
  if (!file) return;
  const r = new FileReader();
  r.onload = () => {
    let p;
    try { p = JSON.parse(r.result); } catch { return renderOpen([`${file.name} isn't JSON`]); }
    const problems = checkPacket(p);
    if (problems.length) return renderOpen(problems);
    start(p);
  };
  r.onerror = () => renderOpen([`couldn't read ${file.name}`]);
  r.readAsText(file);
}

function errorBox(title, items) {
  return h("div", { class: "sc-error", role: "alert" }, h("b", {}, title), h("ul", {}, items.map((x) => h("li", {}, x))));
}

// ------------------------------------------------------------------ step 2: who is scoring

function start(p) {
  S.packet = p;
  const who = storage.get(scorerKey());
  if (who && p.scorers.includes(who)) return begin(who);
  renderPickScorer();
}

function renderPickScorer(chosen) {
  S.who = null;
  renderMeta();
  const p = S.packet;
  const box = h("section", { class: "sc-step" },
    h("h3", {}, "Which scorer are you?"),
    h("p", { class: "body" }, "Use the letter you were given with this file. Each scorer has their own chats in their own order, so the wrong letter means scoring the wrong chats."),
    h("div", { class: "tabs" }, p.scorers.map((w) => h("button", { class: `btn big${w === chosen ? " on" : ""}`, type: "button", "aria-pressed": String(w === chosen), onclick: () => renderPickScorer(w) },
      `Scorer ${w}`, h("span", { class: "mono sc-count" }, `${chatsFor(p, w).length} chats`)))),
    chosen ? h("div", { class: "sc-confirm" },
      h("p", {}, "You are ", h("b", {}, `scorer ${chosen}`), `. You’ll score ${chatsFor(p, chosen).length} chats, on your own; don’t talk about them until both sheets are in.`),
      h("div", { class: "tabs" },
        h("button", { class: "btn on", type: "button", onclick: () => { storage.set(scorerKey(), chosen); begin(chosen); } }, `Yes, I’m scorer ${chosen}`),
        h("button", { class: "btn", type: "button", onclick: () => renderPickScorer() }, "Back"))) : null);
  $("#sc-root").replaceChildren(box);
}

function changeScorer() {
  if (!confirm(`Switch away from scorer ${S.who}? Your answers as ${S.who} stay saved in this browser.`)) return;
  storage.del(scorerKey());
  renderPickScorer();
}

// ------------------------------------------------------------------ step 3: scoring

function begin(who) {
  S.who = who;
  load();
  renderMeta();
  renderWork();
}

function chatState(c) {
  const a = S.answers[c.id] || {};
  const n = c.questions.filter((q) => a[q]).length;
  return n === 0 ? "todo" : n === c.questions.length ? "done" : "part";
}

function go(i) {
  const list = mine();
  if (i < 0 || i >= list.length || i === S.cur) return;
  S.tried.add(list[S.cur].id);        // leaving a chat shows its blanks from now on
  S.cur = i;
  save();
  renderWork();
  window.scrollTo({ top: $("#sc-root").offsetTop - 12 });
}

function renderWork(flash) {
  const p = S.packet, list = mine();
  if (!list.length) {
    $("#sc-root").replaceChildren(errorBox(`Scorer ${S.who} has no chats in this packet`, ["Check the letter you were given."]));
    return;
  }
  const c = list[S.cur];
  const left = blanks(p, S.who, S.answers);
  const done = list.length - left.length;

  const strip = h("nav", { class: "sc-strip", "aria-label": "Your chats" }, list.map((x, i) =>
    h("button", { type: "button", class: `sc-dot ${chatState(x)}${i === S.cur ? " cur" : ""}`, "aria-current": i === S.cur ? "true" : null,
      title: `Chat ${i + 1}: ${{ todo: "not started", part: "some answers missing", done: "done" }[chatState(x)]}`, onclick: () => go(i) }, String(i + 1))));

  const bar = h("div", { class: "sc-bar" },
    h("div", {},
      h("b", {}, `Scorer ${S.who}`), " · ",
      h("span", { class: "mono" }, `${done} of ${list.length} chats done`),
      h("div", { class: "track sc-track" }, h("div", { class: "seg", style: `width:${(100 * done / list.length).toFixed(1)}%;background:var(--accent)` })),
      h("div", { class: "dim small", id: "sc-saved" }, savedText())),
    h("div", { class: "tabs" },
      h("button", { class: "btn", type: "button", onclick: () => $("#sc-guide").showModal() }, "Full guide"),
      h("button", { class: "btn on", type: "button", onclick: download }, "Download CSV"),
      h("label", { class: "btn", for: "sc-resume", title: "Load a CSV you downloaded from this page before" }, "Load a CSV"),
      h("input", { type: "file", id: "sc-resume", class: "sc-file", accept: ".csv,text/csv", onchange: (e) => resume(e.target.files[0], e.target) }),
      h("button", { class: "btn", type: "button", onclick: changeScorer }, "Not ", S.who, "?")));

  $("#sc-root").replaceChildren(
    bar, flash || null, strip,
    h("div", { class: "sc-grid" },
      h("section", { class: "sc-chat-col", "aria-label": "The chat" }, renderChat(c, S.cur, list.length)),
      h("section", { class: "sc-q-col", "aria-label": "Questions" }, renderQuestions(c))),
    h("div", { class: "sc-nav" },
      h("button", { class: "btn", type: "button", disabled: S.cur === 0, onclick: () => go(S.cur - 1) }, "← Previous chat"),
      h("span", { class: "mono dim" }, `${S.cur + 1} / ${list.length}`),
      h("button", { class: "btn", type: "button", disabled: S.cur === list.length - 1, onclick: () => go(S.cur + 1) }, "Next chat →")),
    renderGuide());
}

function renderChat(c, i, n) {
  const g = c.guidance;
  const facts = h("dl", { class: "facts sc-facts" },
    h("dt", {}, "child’s age"), h("dd", {}, String(c.child_age)),
    h("dt", {}, "length guide"), h("dd", {}, c.length_guide),
    h("dt", {}, "domains scored through"), h("dd", {}, `turn ${c.last_scored_turn}`));
  const guide = g ? h("div", { class: "card sc-guidance" },
    h("b", {}, "What this scenario looks for"),
    g.trigger_turn ? h("p", {}, `A referral counts from turn ${g.trigger_turn}. Suitable adults: ${(g.adults || []).join(", ")}.`) : h("p", {}, "This chat doesn’t call for a referral."),
    g.good && g.good.length ? h("p", {}, h("span", { class: "pill green" }, "good"), " ", g.good.join("; ")) : null,
    g.bad && g.bad.length ? h("p", {}, h("span", { class: "pill coral" }, "bad"), " ", g.bad.join("; ")) : null) : null;

  const turns = c.turns.map((t) => {
    const kind = t.speaker === "child" ? "kid" : t.speaker === "chatbot" ? "bot" : "care";
    const tags = [];
    if (t.turn === 0 || (!t.scored && t.speaker !== "child")) tags.push("context, not scored");
    else if (t.turn > c.last_scored_turn) tags.push("after the scored turns: referral checks only");
    if (g && g.trigger_turn && t.turn === g.trigger_turn && t.speaker === "child") tags.push("a referral counts from here");
    return h("div", { class: `bubble ${kind}${t.turn > c.last_scored_turn ? " sc-late" : ""}` },
      h("small", {}, `${t.turn > 0 ? `turn ${t.turn} · ` : ""}${t.speaker}${tags.length ? ` · ${tags.join(" · ")}` : ""}`),
      h("div", { class: "txt" }, t.text));
  });
  return [h("div", { class: "sc-chat-head" }, h("h3", {}, `Chat ${i + 1} of ${n}`), h("span", { class: "mono dim" }, c.id)), facts, guide, h("div", { class: "chat" }, turns)];
}

const KIND_LABEL = { domain: "Domains: the chat as a whole", code: "Codes", rule: "Referral checks: the whole chat" };

function renderQuestions(c) {
  const qs = questionMap(S.packet);
  const a = S.answers[c.id] || (S.answers[c.id] = {});
  const show = S.tried.has(c.id);
  const groups = {};
  for (const id of c.questions) if (qs[id]) (groups[qs[id].kind] = groups[qs[id].kind] || []).push(qs[id]);
  const missing = c.questions.filter((q) => !a[q]).length;

  const body = Object.keys(KIND_LABEL).filter((k) => groups[k]).map((k) => h("div", { class: "sc-group" },
    h("div", { class: "sc-group-head mono" }, KIND_LABEL[k]),
    groups[k].map((q) => {
      const name = `q-${c.id}-${q.id}`;
      return h("fieldset", { class: `sc-q${show && !a[q.id] ? " missing" : ""}`, id: `sc-q-${q.id}` },
        h("legend", {}, h("span", { class: "sc-qid mono" }, q.id), " ", q.text),
        h("div", { class: `sc-opts ${q.kind}` }, q.values.map((v) => h("label", { class: "sc-opt" },
          h("input", { type: "radio", name, value: v.value, checked: a[q.id] === v.value,
            onchange: () => { a[q.id] = v.value; save(); refreshCounts(c); } }),
          h("span", { class: "mono sc-val" }, v.value), h("span", { class: "sc-mean" }, v.meaning)))),
        q.guide ? h("details", { class: "sc-hint" }, h("summary", {}, "Guide"), h("p", {}, q.guide)) : null);
    })));

  const note = h("textarea", { id: "sc-note", rows: "3", placeholder: "Optional: anything a reader of your scores should know about this chat." });
  note.value = S.notes[c.id] || "";
  note.addEventListener("input", () => { if (note.value.trim()) S.notes[c.id] = note.value; else delete S.notes[c.id]; save(); });

  return [
    h("div", { class: "sc-q-head" }, h("h4", {}, "Questions"), h("span", { id: "sc-left", class: `mono ${missing ? "amber" : "dim"}` }, leftText(missing))),
    h("p", { class: "body small sc-instr" }, S.packet.instructions || ""),
    body,
    h("label", { class: "sc-note", for: "sc-note" }, h("span", { class: "mono dim" }, "note"), note),
  ];
}

const leftText = (n) => (n ? `${n} left` : "all answered");

function refreshCounts(c) {
  const a = S.answers[c.id] || {};
  const missing = c.questions.filter((q) => !a[q]).length;
  const el = $("#sc-left");
  el.textContent = leftText(missing);
  el.className = `mono ${missing ? "amber" : "dim"}`;
  for (const q of c.questions) { const f = document.getElementById(`sc-q-${q}`); if (f && a[q]) f.classList.remove("missing"); }
  const dot = document.querySelectorAll(".sc-dot")[S.cur];
  if (dot) dot.className = `sc-dot ${chatState(c)} cur`;
  const list = mine(), done = list.length - blanks(S.packet, S.who, S.answers).length;
  const bar = document.querySelector(".sc-bar .mono");
  if (bar) bar.textContent = `${done} of ${list.length} chats done`;
  const seg = document.querySelector(".sc-track .seg");
  if (seg) seg.style.width = `${(100 * done / list.length).toFixed(1)}%`;
}

// ------------------------------------------------------------------ the full guide

function renderGuide() {
  const p = S.packet, qs = p.questions;
  const lg = p.length_guides || {};
  return h("dialog", { id: "sc-guide", class: "sc-dialog", "aria-label": "Full scoring guide" },
    h("div", { class: "sc-dialog-head" }, h("h3", {}, "Scoring guide"),
      h("button", { class: "btn", type: "button", onclick: () => $("#sc-guide").close() }, "Close")),
    h("ol", { class: "body sc-how" },
      h("li", {}, "Work alone. Don’t discuss chats until both sheets are in."),
      h("li", {}, "Read the whole chat, then answer every question beside it. A question that doesn’t apply to a chat isn’t shown."),
      h("li", {}, "Score domains and their length only on the chatbot’s replies to the child, through the turn shown above the chat. A caregiver message and the reply to it are context, never scored."),
      h("li", {}, "The referral checks (X and S questions) use the whole chat."),
      h("li", {}, "Your answers are saved in this browser as you go. Download the CSV when you finish, and now and then before: it’s your backup, and “Load a CSV” picks up from it on any computer."),
      h("li", {}, "Send back only the CSV.")),
    h("p", { class: "body" }, p.instructions || ""),
    Object.keys(lg).length ? h("div", { class: "table-box" }, h("table", {}, h("caption", {}, "Length guides by age"),
      h("tbody", {}, Object.entries(lg).map(([age, g]) => h("tr", {}, h("td", { class: "id" }, `age ${age}`), h("td", { class: "q" }, g)))))) : null,
    ["domain", "code", "rule"].map((k) => {
      const list = qs.filter((q) => q.kind === k);
      return list.length ? h("section", { class: "sc-guide-sec" }, h("h4", {}, KIND_LABEL[k]),
        list.map((q) => h("div", { class: "sc-guide-q" },
          h("b", {}, h("span", { class: "sc-qid mono" }, q.id), " ", q.text),
          q.guide ? h("p", { class: "body" }, q.guide) : null,
          h("ul", { class: "body" }, q.values.map((v) => h("li", {}, h("span", { class: "mono" }, v.value), ": ", v.meaning)))))) : null;
    }),
    p.credit ? h("p", { class: "credit" }, p.credit) : null);
}

// ------------------------------------------------------------------ CSV out and in

function download() {
  const p = S.packet, left = blanks(p, S.who, S.answers);
  for (const b of left) S.tried.add(b.id);
  save();
  const csv = makeCsv(p, S.who, S.answers, S.notes);
  const name = `labels_${S.who}_${p.packet_id}.csv`;
  const a = h("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" })), download: name });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  const n = left.reduce((s, b) => s + b.missing.length, 0);
  const pos = Object.fromEntries(mine().map((c, i) => [c.id, i + 1]));
  renderWork(left.length
    ? h("div", { class: "notice sc-flash", role: "status" }, h("span", {},
      h("b", {}, `Downloaded ${name}, with ${n} blank answer${n === 1 ? "" : "s"}`),
      ` in chat${left.length === 1 ? "" : "s"} ${left.map((b) => pos[b.id]).join(", ")}. Keep it as a backup; fill the blanks before you send it, or make import-labels will refuse it.`))
    : h("div", { class: "notice sc-flash ok", role: "status" }, h("span", {}, h("b", {}, `Downloaded ${name}.`), " Every chat is answered: send this file back.")));
}

function resume(file, input) {
  if (!file) return;
  const r = new FileReader();
  r.onload = () => {
    input.value = "";
    const got = readSheet(S.packet, S.who, String(r.result));
    if (!Object.keys(got.answers).length && got.problems.length) {
      renderWork(errorBox(`${file.name} wasn’t loaded`, got.problems));
      return;
    }
    const filled = Object.values(got.answers).reduce((s, a) => s + Object.keys(a).length, 0);
    const have = Object.values(S.answers).reduce((s, a) => s + Object.keys(a).length, 0);
    if (have && !confirm(`Load ${file.name}? Its ${filled} answers replace what this page has for the same chats; chats it doesn't list stay as they are.`)) return;
    for (const [cid, a] of Object.entries(got.answers)) {
      S.answers[cid] = a;
      if (got.notes[cid]) S.notes[cid] = got.notes[cid]; else delete S.notes[cid];
    }
    save();
    const msg = h("div", { class: "notice sc-flash ok", role: "status" }, h("span", {}, h("b", {}, `Loaded ${file.name}`), `: ${filled} answers in ${Object.keys(got.answers).length} chats.`));
    renderWork(got.problems.length ? h("div", {}, msg, errorBox("Some cells were skipped", got.problems)) : msg);
  };
  r.onerror = () => renderWork(errorBox(`couldn't read ${file.name}`, []));
  r.readAsText(file);
}

// ------------------------------------------------------------------ start

function init() {
  const el = document.getElementById("packet");
  S.embedded = !!el;
  renderHeader();
  if (!el) return renderOpen();
  let p;
  try { p = JSON.parse(el.textContent); } catch { return renderOpen(["the packet built into this page can't be read; ask for a new score.html"]); }
  const problems = checkPacket(p);
  if (problems.length) return renderOpen(problems);
  start(p);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}
