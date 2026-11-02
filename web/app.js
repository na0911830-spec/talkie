"use strict";

/* ---------- storage that survives sandboxed browsers ---------- */
const store = (() => {
  const mem = {};
  return {
    get(k) { try { return localStorage.getItem(k); } catch { return mem[k] ?? null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { mem[k] = v; } },
    del(k) { try { localStorage.removeItem(k); } catch { delete mem[k]; } },
  };
})();

const $ = (sel) => document.querySelector(sel);
const els = {
  signin: $("#signin"), chat: $("#chat"), log: $("#log"), empty: $("#empty"),
  form: $("#composer"), input: $("#composer-input"), send: $("#send"), mic: $("#mic"),
  status: $("#status"), actions: $("#top-actions"), panel: $("#panel"), openPanel: $("#open-panel"),
};
let token = store.get("dost_token");
let busy = false;
let stickToBottom = true;
els.log.addEventListener("scroll", () => {
  stickToBottom = els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 80;
});
const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && path !== "/api/login") { signOut(); throw new Error("Please sign in again."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Try again.");
  return data;
}

/* ---------- tiny safe markdown ---------- */
function esc(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function inline(s) {
  return s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w*])\*(?!\s)([^*\n]+?)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
}
function renderMarkdown(src) {
  const blocks = [];
  let text = src.replace(/```(\w+)?\n?([\s\S]*?)```/g, (_, lang, code) => {
    blocks.push(`<pre data-lang="${esc(lang || "")}"><button class="copy" type="button">Copy</button><code>${esc(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  text = esc(text);
  const html = text.split(/\n{2,}/).map((para) => {
    if (/^\u0000\d+\u0000$/.test(para.trim())) return para.trim();
    const lines = para.split("\n");
    if (lines.every((l) => /^\s*([-*•]|\d+[.)])\s+/.test(l))) {
      const ordered = /^\s*\d/.test(lines[0]);
      const items = lines.map((l) => `<li>${inline(l.replace(/^\s*([-*•]|\d+[.)])\s+/, ""))}</li>`).join("");
      return ordered ? `<ol>${items}</ol>` : `<ul>${items}</ul>`;
    }
    const heading = para.match(/^#{1,6}\s+(.+)$/);
    if (heading && lines.length === 1) return `<p><strong>${inline(heading[1])}</strong></p>`;
    return `<p>${lines.map(inline).join("<br>")}</p>`;
  }).join("");
  return html.replace(/\u0000(\d+)\u0000/g, (_, i) => blocks[Number(i)]);
}

/* ---------- messages ---------- */
function mediaNode(att) {
  const wrap = document.createElement("figure");
  const url = att.url;
  const label = att.title || att.caption || "";
  if (att.type === "image") {
    const img = document.createElement("img");
    img.src = url; img.alt = label || "Image from Dost";
    img.onload = () => { if (stickToBottom) els.log.scrollTop = els.log.scrollHeight; };
    img.onerror = () => { wrap.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener">Open image</a>`; };
    wrap.append(img);
  } else if (att.type === "audio") {
    const audio = document.createElement("audio");
    audio.controls = true; audio.src = url; audio.preload = "none";
    audio.setAttribute("aria-label", label || "Audio");
    wrap.append(audio);
  } else if (att.type === "video") {
    const v = document.createElement("video");
    v.controls = true; v.src = url; v.preload = "metadata";
    wrap.append(v);
  } else {
    wrap.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener" download>Download ${esc(label || "file")}</a>`;
  }
  if (label && att.type !== "file") {
    const cap = document.createElement("figcaption"); cap.textContent = label; wrap.append(cap);
  }
  return wrap;
}

function addMessage(role, text, attachments = [], when = new Date()) {
  els.empty.hidden = true;
  const msg = document.createElement("div");
  msg.className = `msg ${role === "user" ? "me" : "bot"}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "user") bubble.textContent = text;
  else bubble.innerHTML = renderMarkdown(text);
  msg.append(bubble);
  if (attachments.length) {
    const m = document.createElement("div");
    m.className = "media";
    attachments.forEach((a) => m.append(mediaNode(a)));
    msg.append(m);
  }
  const meta = document.createElement("time");
  meta.className = "meta";
  meta.dateTime = when.toISOString();
  const today = new Date().toDateString() === when.toDateString();
  meta.textContent = (role === "user" ? "You" : "Dost") + " · " + (today ? timeFmt.format(when) : dateFmt.format(when));
  msg.append(meta);
  els.log.append(msg);
  els.log.scrollTop = els.log.scrollHeight;
  return msg;
}

function showTyping() {
  const t = document.createElement("div");
  t.className = "msg bot";
  t.innerHTML = '<div class="bubble typing" role="status" aria-label="Dost is typing"><span></span><span></span><span></span></div>';
  els.log.append(t);
  els.log.scrollTop = els.log.scrollHeight;
  return t;
}

function setBusy(on, label) {
  busy = on;
  els.send.disabled = on;
  els.status.textContent = on ? (label || "typing…") : "Your friend, in every language";
}

async function sendText(text) {
  text = text.trim();
  if (!text || busy) return;
  addMessage("user", text);
  els.input.value = ""; autoGrow();
  setBusy(true);
  const typing = showTyping();
  try {
    const r = await api("/api/chat", { method: "POST", json: { text } });
    typing.remove();
    addMessage("assistant", r.text, r.attachments || []);
  } catch (e) {
    typing.remove();
    addMessage("assistant", `Couldn't reach me just now: ${e.message}`);
  } finally { setBusy(false); els.input.focus(); }
}

/* ---------- voice ---------- */
let recorder = null;
els.mic.addEventListener("click", async () => {
  if (recorder) { recorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    addMessage("assistant", "This browser can't record voice notes. Try typing instead!");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      recorder = null;
      els.mic.setAttribute("aria-pressed", "false");
      els.mic.setAttribute("aria-label", "Record a voice note");
      const blob = new Blob(chunks, { type: chunks[0]?.type || "audio/webm" });
      const fd = new FormData(); fd.append("file", blob, "voice.webm");
      setBusy(true, "listening…");
      const typing = showTyping();
      try {
        const r = await api("/api/voice", { method: "POST", body: fd });
        typing.remove();
        addMessage("user", `🎙 ${r.transcript}`);
        addMessage("assistant", r.text, r.attachments || []);
      } catch (e) { typing.remove(); addMessage("assistant", e.message); }
      finally { setBusy(false); }
    };
    recorder.start();
    els.mic.setAttribute("aria-pressed", "true");
    els.mic.setAttribute("aria-label", "Stop recording and send");
    els.status.textContent = "Recording… tap the mic again to send";
  } catch { addMessage("assistant", "I need microphone permission for voice notes."); }
});

/* ---------- composer ---------- */
function autoGrow() { els.input.style.height = "auto"; els.input.style.height = Math.min(els.input.scrollHeight, 180) + "px"; }
els.input.addEventListener("input", autoGrow);
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendText(els.input.value); }
});
els.form.addEventListener("submit", (e) => { e.preventDefault(); sendText(els.input.value); });
document.querySelectorAll(".idea").forEach((b) => b.addEventListener("click", () => sendText(b.textContent)));
els.log.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy");
  if (!btn) return;
  try { await navigator.clipboard.writeText(btn.nextElementSibling.textContent); btn.textContent = "Copied"; }
  catch { btn.textContent = "Select and copy"; }
  setTimeout(() => { btn.textContent = "Copy"; }, 1500);
});

/* ---------- session ---------- */
function signOut() {
  token = null; store.del("dost_token");
  els.chat.hidden = true; els.actions.hidden = true; els.signin.hidden = false; closePanel();
}

async function enterChat() {
  els.signin.hidden = true; els.chat.hidden = false; els.actions.hidden = false;
  try {
    const me = await api("/api/me");
    document.title = `${me.bot}: chat`;
    $("#bot-name").textContent = me.bot;
    els.input.placeholder = `Message ${me.bot}…`;
    if (me.name) $("#empty-greet").textContent = `Kya haal hai, ${me.name}?`;
    els.log.querySelectorAll(".msg").forEach((m) => m.remove());
    const hist = await api("/api/history");
    hist.forEach((m) => addMessage(m.role, m.content, m.attachments || [], new Date(m.created_at)));
  } catch (e) { if (token) addMessage("assistant", e.message); }
  els.input.focus();
}

$("#signin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#signin-error"); err.textContent = "";
  try {
    const r = await api("/api/login", { method: "POST", json: { name: $("#name").value, password: $("#password").value } });
    token = r.token; store.set("dost_token", token);
    enterChat();
  } catch (ex) { err.textContent = ex.message; }
});
$("#sign-out").addEventListener("click", signOut);

/* ---------- panel ---------- */
function openPanel() {
  els.panel.hidden = false; els.openPanel.setAttribute("aria-expanded", "true");
  $("#close-panel").focus(); loadSkills(); loadMemories();
}
function closePanel() {
  if (els.panel.hidden) return;
  els.panel.hidden = true; els.openPanel.setAttribute("aria-expanded", "false"); els.openPanel.focus();
}
els.openPanel.addEventListener("click", () => (els.panel.hidden ? openPanel() : closePanel()));
$("#close-panel").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });

const tabs = [...document.querySelectorAll('[role="tab"]')];
function selectTab(tab) {
  tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  });
  tab.focus();
}
tabs.forEach((t, i) => {
  t.addEventListener("click", () => selectTab(t));
  t.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") selectTab(tabs[(i + 1) % tabs.length]);
    if (e.key === "ArrowLeft") selectTab(tabs[(i - 1 + tabs.length) % tabs.length]);
  });
});

function emptyRow(list, text) { list.innerHTML = `<li class="empty-row">${esc(text)}</li>`; }

async function loadSkills() {
  const list = $("#skill-list");
  try {
    const items = await api("/api/skills");
    if (!items.length) return emptyRow(list, "No skills yet. Build one below or ask me in chat.");
    list.innerHTML = "";
    items.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = `<div class="item-main">
          <div class="item-title">${esc(s.name)} ${s.enabled ? "" : '<span class="state-off">· Off</span>'}</div>
          <p class="item-sub">${esc(s.description)}</p>
          <p class="item-sub">Secrets: ${s.secrets.length ? esc(s.secrets.join(", ")) : "none"}</p>
        </div>
        <div class="item-actions">
          <button class="btn ghost small" data-act="code">Code</button>
          <button class="btn ghost small" data-act="toggle">${s.enabled ? "Turn off" : "Turn on"}</button>
          <button class="btn ghost small" data-act="delete" aria-label="Delete skill ${esc(s.name)}">Delete</button>
        </div>`;
      li.querySelector('[data-act="toggle"]').onclick = async () => {
        await api(`/api/skills/${encodeURIComponent(s.name)}`, { method: "PATCH", json: { enabled: !s.enabled } });
        loadSkills();
      };
      li.querySelector('[data-act="delete"]').onclick = async () => {
        if (!confirm(`Delete the skill “${s.name}”?`)) return;
        await api(`/api/skills/${encodeURIComponent(s.name)}`, { method: "DELETE" });
        loadSkills();
      };
      li.querySelector('[data-act="code"]').onclick = () => toggleCode(li, s);
      list.append(li);
    });
  } catch (e) { emptyRow(list, e.message); }
}

function toggleCode(li, s) {
  const existing = li.querySelector(".stack");
  if (existing) { existing.remove(); return; }
  const id = `code-${s.id}`;
  const f = document.createElement("form");
  f.className = "stack"; f.style.width = "100%";
  f.innerHTML = `<label for="${id}">Code for ${esc(s.name)}</label>
    <textarea id="${id}" class="code-view" spellcheck="false"></textarea>
    <p class="error" role="alert"></p>
    <button class="btn primary small" type="submit">Save code</button>`;
  f.querySelector("textarea").value = s.code;
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api(`/api/skills/${encodeURIComponent(s.name)}`, { method: "PATCH", json: { code: f.querySelector("textarea").value } });
      loadSkills();
    } catch (ex) { f.querySelector(".error").textContent = ex.message; }
  };
  li.querySelector(".item-main").append(f);
}

$("#skill-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#skill-submit"), err = $("#skill-error");
  err.textContent = ""; btn.disabled = true; btn.textContent = "Writing and testing code…";
  const secrets = {};
  $("#skill-secrets").value.split("\n").forEach((line) => {
    const i = line.indexOf("=");
    if (i > 0) secrets[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  });
  try {
    const r = await api("/api/skills", { method: "POST", json: { name: $("#skill-name").value, request: $("#skill-request").value, secrets } });
    e.target.reset();
    err.textContent = "";
    addMessage("assistant", `New skill **${r.name}** is ready${r.tested ? " and passed its self-test" : ""}. ${r.test_output ? "\n\n" + r.test_output : ""}`);
    loadSkills();
  } catch (ex) { err.textContent = ex.message; }
  finally { btn.disabled = false; btn.textContent = "Build and test skill"; }
});

async function loadMemories() {
  const list = $("#memory-list");
  try {
    const items = await api("/api/memories");
    if (!items.length) return emptyRow(list, "Nothing yet. Tell me about yourself!");
    list.innerHTML = "";
    items.forEach((m) => {
      const li = document.createElement("li");
      li.innerHTML = `<div class="item-main">${esc(m.content)}<p class="item-sub">${dateFmt.format(new Date(m.updated_at))}</p></div>
        <div class="item-actions"><button class="btn ghost small" aria-label="Forget: ${esc(m.content)}">Forget</button></div>`;
      li.querySelector("button").onclick = async () => { await api(`/api/memories/${m.id}`, { method: "DELETE" }); loadMemories(); };
      list.append(li);
    });
  } catch (e) { emptyRow(list, e.message); }
}

$("#reset-chat").addEventListener("click", async () => {
  await api("/api/reset", { method: "POST" });
  els.log.querySelectorAll(".msg").forEach((m) => m.remove());
  els.empty.hidden = false; closePanel();
});

$("#make-code").addEventListener("click", async () => {
  const r = await api("/api/link/code", { method: "POST" });
  $("#code-out").textContent = `/link ${r.code}`;
});
$("#link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#link-error"); err.textContent = "";
  try {
    const r = await api("/api/link", { method: "POST", json: { code: $("#link-code").value } });
    token = r.token; store.set("dost_token", token); closePanel(); enterChat();
  } catch (ex) { err.textContent = ex.message; }
});

if (token) enterChat();
