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
const body = document.body;
const els = {
  chat: $("#chat"), signin: $("#signin"), log: $("#log"), thread: $("#thread"), empty: $("#empty"),
  form: $("#composer"), input: $("#composer-input"), send: $("#send"), mic: $("#mic"),
  status: $("#status"), panel: $("#panel"), openPanel: $("#open-panel"), sidebar: $("#sidebar"),
  scrim: $("#scrim"), menuBtn: $("#menu-btn"), toast: $("#toast"),
};
let token = store.get("dost_token");
let botName = "Talkie";
let busy = false;
let stickToBottom = true;
const IDLE_STATUS = "Your friend, in every language";

els.log.addEventListener("scroll", () => {
  stickToBottom = els.log.scrollHeight - els.log.scrollTop - els.log.clientHeight < 80;
});
function scrollDown(force = false) { if (force || stickToBottom) els.log.scrollTop = els.log.scrollHeight; }

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const dateFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

let toastTimer;
function toast(msg) {
  els.toast.textContent = msg; els.toast.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { els.toast.hidden = true; }, 3200);
}

/* ---------- theme ---------- */
const SUN = '<svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM11 1h2v3h-2zm0 19h2v3h-2zM1 11h3v2H1zm19 0h3v2h-3zM4.2 5.6l1.4-1.4 2.1 2.1-1.4 1.4zm12.1 12.1 1.4-1.4 2.1 2.1-1.4 1.4zM16.3 6.3l2.1-2.1 1.4 1.4-2.1 2.1zM4.2 18.4l2.1-2.1 1.4 1.4-2.1 2.1z"/></svg>';
const MOON = '<svg viewBox="0 0 24 24" width="18" height="18"><path fill="currentColor" d="M12 3a9 9 0 1 0 9 9c0-.46-.04-.92-.1-1.36a5.4 5.4 0 0 1-4.4 2.26 5.4 5.4 0 0 1-3.14-9.8c-.44-.06-.9-.1-1.36-.1z"/></svg>';
function setTheme(dark) {
  if (dark) document.documentElement.setAttribute("data-theme", "dark");
  else document.documentElement.removeAttribute("data-theme");
  store.set("talkie_theme", dark ? "dark" : "light");
  $("#theme-icon").innerHTML = dark ? SUN : MOON;
  $("#theme-label").textContent = dark ? "Light mode" : "Dark mode";
}
const savedTheme = store.get("talkie_theme");
setTheme(savedTheme ? savedTheme === "dark" : !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches));
$("#theme-toggle").addEventListener("click", () => setTheme(document.documentElement.getAttribute("data-theme") !== "dark"));

/* ---------- drawer (phones) ---------- */
function setDrawer(open) {
  body.classList.toggle("drawer-open", open);
  els.scrim.hidden = !open;
  els.menuBtn.setAttribute("aria-expanded", String(open));
}
els.menuBtn.addEventListener("click", () => setDrawer(!body.classList.contains("drawer-open")));
els.scrim.addEventListener("click", () => setDrawer(false));

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401 && !["/api/login", "/api/signup"].includes(path)) { signOut(); throw new Error("Please sign in again."); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Try again.");
  return data;
}

/* ---------- safe markdown ---------- */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function inline(s) {
  return s
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^\w*])\*(?!\s)([^*\n]+?)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(>])(https?:\/\/[^\s<)"]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
}
function renderMarkdown(src) {
  const blocks = [];
  // closed fences, plus an unclosed fence while a reply is still streaming
  let text = src.replace(/```([\w+#-]*)[ \t]*\n?([\s\S]*?)(```|$)/g, (_, lang, code) => {
    blocks.push(`<div class="codeblock"><div class="codehead"><span>${esc(lang || "code")}</span><button class="copy" type="button">Copy</button></div><pre><code>${esc(code.replace(/\n$/, ""))}</code></pre></div>`);
    return `\n\n\u0000${blocks.length - 1}\u0000\n\n`;
  });
  text = esc(text);
  const html = text.split(/\n{2,}/).map((para) => {
    para = para.trim();
    if (!para) return "";
    if (/^\u0000\d+\u0000$/.test(para)) return para;
    const lines = para.split("\n");
    if (lines.every((l) => /^\s*([-*•]|\d+[.)])\s+/.test(l))) {
      const ordered = /^\s*\d/.test(lines[0]);
      const items = lines.map((l) => `<li>${inline(l.replace(/^\s*([-*•]|\d+[.)])\s+/, ""))}</li>`).join("");
      return ordered ? `<ol>${items}</ol>` : `<ul>${items}</ul>`;
    }
    if (lines.every((l) => /^&gt;\s?/.test(l))) return `<blockquote>${lines.map((l) => inline(l.replace(/^&gt;\s?/, ""))).join("<br>")}</blockquote>`;
    const heading = para.match(/^#{1,6}\s+(.+)$/);
    if (heading && lines.length === 1) return `<h3>${inline(heading[1])}</h3>`;
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
    img.src = url; img.alt = label || `Image from ${botName}`; img.loading = "lazy";
    img.onload = () => scrollDown();
    img.onerror = () => { wrap.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open image</a>`; };
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
    wrap.innerHTML = `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" download>Download ${esc(label || "file")}</a>`;
  }
  if (label && att.type !== "file") {
    const cap = document.createElement("figcaption"); cap.textContent = label; wrap.append(cap);
  }
  return wrap;
}

function avatarEl() {
  const a = document.createElement("span");
  a.className = "avatar"; a.setAttribute("aria-hidden", "true"); a.textContent = (botName[0] || "T").toUpperCase();
  return a;
}

function botActions(msg, getText) {
  const bar = document.createElement("div");
  bar.className = "msg-actions";
  const copy = document.createElement("button");
  copy.type = "button"; copy.className = "mini"; copy.textContent = "Copy";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(getText()); copy.textContent = "Copied"; }
    catch { copy.textContent = "Press Ctrl+C"; }
    setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  });
  bar.append(copy);
  msg.append(bar);
}

function metaEl(who, when) {
  const meta = document.createElement("time");
  meta.className = "meta"; meta.dateTime = when.toISOString();
  const today = new Date().toDateString() === when.toDateString();
  meta.textContent = `${who} · ${today ? timeFmt.format(when) : dateFmt.format(when)}`;
  return meta;
}

function addMessage(role, text, attachments = [], when = new Date()) {
  els.empty.hidden = true;
  const isMe = role === "user";
  const msg = document.createElement("div");
  msg.className = `msg ${isMe ? "me" : "bot"}`;
  if (!isMe) msg.append(avatarEl());
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (isMe) bubble.textContent = text; else bubble.innerHTML = renderMarkdown(text);
  msg.append(bubble);
  if (attachments.length) {
    const m = document.createElement("div");
    m.className = "media";
    attachments.forEach((a) => m.append(mediaNode(a)));
    msg.append(m);
  }
  if (!isMe) botActions(msg, () => text);
  msg.append(metaEl(isMe ? "You" : botName, when));
  els.thread.append(msg);
  scrollDown(true);
  return msg;
}

/* A bot message that fills in while we stream, with live work steps. */
function startBotMessage() {
  els.empty.hidden = true;
  const msg = document.createElement("div");
  msg.className = "msg bot";
  msg.append(avatarEl());
  const steps = document.createElement("ul");
  steps.className = "steps"; steps.hidden = true; steps.setAttribute("aria-label", "What I did");
  const working = document.createElement("div");
  working.className = "working"; working.setAttribute("role", "status");
  working.innerHTML = '<span class="dots" aria-hidden="true"><span></span><span></span><span></span></span><span class="working-text">Thinking…</span>';
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  msg.append(steps, working, bubble);
  els.thread.append(msg);
  scrollDown(true);
  return {
    msg, bubble, text: "",
    status(t) {
      working.querySelector(".working-text").textContent = t;
      els.status.textContent = t;
      if (!steps.lastElementChild || steps.lastElementChild.textContent !== t) {
        const li = document.createElement("li"); li.textContent = t.replace(/…$/, ""); steps.append(li); steps.hidden = false;
      }
      scrollDown();
    },
    chunk(t) { this.text += t; working.hidden = true; bubble.innerHTML = renderMarkdown(this.text); scrollDown(); },
    finish(attachments, finalText) {
      working.remove();
      if (finalText && finalText !== this.text) { this.text = finalText; bubble.innerHTML = renderMarkdown(finalText); }
      if (!steps.children.length) steps.remove();
      if (attachments?.length) {
        const m = document.createElement("div"); m.className = "media";
        attachments.forEach((a) => m.append(mediaNode(a)));
        msg.append(m);
      }
      botActions(msg, () => this.text);
      msg.append(metaEl(botName, new Date()));
      scrollDown();
    },
    fail(t) { working.remove(); steps.remove(); bubble.textContent = t; msg.append(metaEl(botName, new Date())); },
  };
}

function setBusy(on, label) {
  busy = on;
  els.send.disabled = on;
  els.status.textContent = on ? (label || "Thinking…") : IDLE_STATUS;
}

async function sendText(text) {
  text = (text || "").trim();
  if (!text || busy) return;
  addMessage("user", text);
  els.input.value = ""; autoGrow();
  setBusy(true);
  const live = startBotMessage();
  let finished = false;
  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ text }),
    });
    if (res.status === 401) { live.msg.remove(); signOut(); throw new Error("Please sign in again."); }
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || "Something went wrong.");
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop();
      for (const block of events) {
        const line = block.trim();
        if (!line.startsWith("data: ")) continue;
        let payload;
        try { payload = JSON.parse(line.slice(6)); } catch { continue; }
        if (payload.type === "status") live.status(payload.text);
        else if (payload.type === "chunk") live.chunk(payload.text);
        else if (payload.type === "done") { live.finish(payload.attachments, payload.text); finished = true; }
        else if (payload.type === "error") { live.fail(`Error: ${payload.text}`); finished = true; }
      }
    }
    if (!finished) live.finish([], live.text || "I didn't get a reply. Please try again.");
  } catch (e) {
    live.fail(`Couldn't reach me just now: ${e.message}`);
  } finally {
    setBusy(false);
    els.input.focus();
  }
}

/* ---------- voice ---------- */
let recorder = null;
els.mic.addEventListener("click", async () => {
  if (recorder) { recorder.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    toast("This browser can't record voice notes. Try typing instead!");
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
      setBusy(true, "Listening…");
      const live = startBotMessage();
      try {
        const r = await api("/api/voice", { method: "POST", body: fd });
        live.msg.remove();
        addMessage("user", `🎙 ${r.transcript}`);
        addMessage("assistant", r.text, r.attachments || []);
      } catch (e) { live.fail(e.message); }
      finally { setBusy(false); }
    };
    recorder.start();
    els.mic.setAttribute("aria-pressed", "true");
    els.mic.setAttribute("aria-label", "Stop recording and send");
    els.status.textContent = "Recording… tap the mic again to send";
  } catch { toast("I need microphone permission for voice notes."); }
});

/* ---------- composer ---------- */
function autoGrow() { els.input.style.height = "auto"; els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px"; }
els.input.addEventListener("input", autoGrow);
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendText(els.input.value); }
});
els.form.addEventListener("submit", (e) => { e.preventDefault(); sendText(els.input.value); });
document.querySelectorAll(".idea").forEach((b) => b.addEventListener("click", () => sendText(b.querySelector("span").textContent)));
els.log.addEventListener("click", async (e) => {
  const btn = e.target.closest(".copy");
  if (!btn) return;
  const code = btn.closest(".codeblock").querySelector("code").textContent;
  try { await navigator.clipboard.writeText(code); btn.textContent = "Copied"; }
  catch { btn.textContent = "Select and copy"; }
  setTimeout(() => { btn.textContent = "Copy"; }, 1500);
});

/* ---------- session ---------- */
function clearMessages() { els.thread.querySelectorAll(".msg").forEach((m) => m.remove()); }

function signOut() {
  token = null; store.del("dost_token");
  body.classList.add("signed-out");
  els.chat.hidden = true; els.signin.hidden = false;
  setDrawer(false); closePanel(true);
}

async function enterChat() {
  body.classList.remove("signed-out");
  els.signin.hidden = true; els.chat.hidden = false;
  try {
    const me = await api("/api/me");
    botName = me.bot || "Talkie";
    document.title = `${botName}: chat`;
    $("#bot-name").textContent = botName;
    $("#brand-name").textContent = botName;
    $("#side-user").textContent = me.name ? `Signed in as ${me.name}` : "";
    els.input.placeholder = `Message ${botName}…`;
    if (me.name) $("#empty-greet").textContent = `Kya haal hai, ${me.name}?`;
    clearMessages(); els.empty.hidden = false;
    const hist = await api("/api/history");
    hist.forEach((m) => addMessage(m.role, m.content, m.attachments || [], new Date(m.created_at)));
    scrollDown(true);
  } catch (e) { if (token) toast(e.message); }
  els.input.focus();
}

function setAuthMode(login) {
  $("#signin-form").hidden = !login;
  $("#signup-form").hidden = login;
  for (const [id, on] of [["#btn-show-login", login], ["#btn-show-signup", !login]]) {
    const b = $(id);
    b.classList.toggle("primary", on); b.classList.toggle("ghost", !on); b.setAttribute("aria-pressed", String(on));
  }
}
$("#btn-show-login").addEventListener("click", () => setAuthMode(true));
$("#btn-show-signup").addEventListener("click", () => setAuthMode(false));

$("#signin-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#signin-error"); err.textContent = "";
  try {
    const r = await api("/api/login", { method: "POST", json: { name: $("#name").value, password: $("#password").value } });
    token = r.token; store.set("dost_token", token);
    enterChat();
  } catch (ex) { err.textContent = ex.message; }
});

$("#signup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#signup-error"); err.textContent = "";
  try {
    const r = await api("/api/signup", {
      method: "POST",
      json: { username: $("#signup-username").value, name: $("#signup-name").value, password: $("#signup-password").value },
    });
    token = r.token; store.set("dost_token", token);
    enterChat();
  } catch (ex) { err.textContent = ex.message; }
});
$("#sign-out").addEventListener("click", signOut);

/* ---------- panel ---------- */
const tabs = [...document.querySelectorAll('[role="tab"]')];
function selectTab(tab, focus = true) {
  tabs.forEach((t) => {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  });
  if (focus) tab.focus();
}
function openPanel(tabId = "tab-skills") {
  setDrawer(false);
  els.panel.hidden = false; els.openPanel.setAttribute("aria-expanded", "true");
  selectTab(document.getElementById(tabId), false);
  $("#close-panel").focus();
  loadSkills(); loadCommunitySkills(); loadMemories();
}
function closePanel(silent) {
  if (els.panel.hidden) return;
  els.panel.hidden = true; els.openPanel.setAttribute("aria-expanded", "false");
  if (!silent) els.input.focus();
}
document.querySelectorAll("[data-open-tab]").forEach((b) => b.addEventListener("click", () => openPanel(b.dataset.openTab)));
$("#close-panel").addEventListener("click", () => closePanel());
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closePanel(); setDrawer(false); } });
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
          <div class="item-title">${esc(s.name)}
            <span class="badge ${s.is_public ? "public" : ""}">${s.is_public ? "Public" : "Private"}</span>
            ${s.enabled ? "" : '<span class="badge">Off</span>'}
          </div>
          <p class="item-sub">${esc(s.description)}</p>
          <p class="item-sub">Secrets: ${s.secrets.length ? esc(s.secrets.join(", ")) : "none"}</p>
        </div>
        <div class="item-actions">
          <button class="btn ghost small" data-act="upgrade">Upgrade</button>
          <button class="btn ghost small" data-act="share">${s.is_public ? "Make private" : "Make public"}</button>
          <button class="btn ghost small" data-act="code">Code</button>
          <button class="btn ghost small" data-act="toggle">${s.enabled ? "Turn off" : "Turn on"}</button>
          <button class="btn ghost small danger" data-act="delete" aria-label="Delete skill ${esc(s.name)}">Delete</button>
        </div>`;
      const on = (act, fn) => { li.querySelector(`[data-act="${act}"]`).onclick = fn; };
      on("upgrade", () => {
        closePanel(true);
        els.input.value = `Upgrade my skill ${s.name} so it also `;
        autoGrow(); els.input.focus();
        toast("Finish the sentence and send it to upgrade the skill.");
      });
      on("share", async () => {
        const goingPublic = !s.is_public;
        if (goingPublic && !confirm(`Make “${s.name}” public? Everyone can install its code. Your secrets are never shared.`)) return;
        try {
          await api(`/api/skills/${encodeURIComponent(s.name)}`, { method: "PATCH", json: { is_public: goingPublic } });
          toast(goingPublic ? `${s.name} is now public.` : `${s.name} is private again.`);
          loadSkills(); loadCommunitySkills();
        } catch (ex) { toast(ex.message); }
      });
      on("toggle", async () => {
        await api(`/api/skills/${encodeURIComponent(s.name)}`, { method: "PATCH", json: { enabled: !s.enabled } });
        loadSkills();
      });
      on("delete", async () => {
        if (!confirm(`Delete the skill “${s.name}”?`)) return;
        await api(`/api/skills/${encodeURIComponent(s.name)}`, { method: "DELETE" });
        loadSkills(); loadCommunitySkills();
      });
      on("code", () => toggleCode(li, s));
      list.append(li);
    });
  } catch (e) { emptyRow(list, e.message); }
}

async function loadCommunitySkills() {
  const list = $("#community-list");
  try {
    const items = await api("/api/skills/public");
    if (!items.length) return emptyRow(list, "No public skills shared yet. Be the first to share one!");
    list.innerHTML = "";
    items.forEach((s) => {
      const li = document.createElement("li");
      li.innerHTML = `<div class="item-main">
          <div class="item-title">${esc(s.name)} <span class="badge">by ${esc(s.author_name || "Community")}</span></div>
          <p class="item-sub">${esc(s.description)}</p>
        </div>
        <div class="item-actions">
          <button class="btn ghost small" data-act="code">View code</button>
          <button class="btn primary small" data-act="install">Install</button>
        </div>`;
      li.querySelector('[data-act="code"]').onclick = () => toggleCode(li, s);
      li.querySelector('[data-act="install"]').onclick = async () => {
        try {
          await api(`/api/skills/install/${s.id}`, { method: "POST" });
          toast(`Installed “${s.name}”. Ask me to use it!`);
          loadSkills();
        } catch (ex) { toast(ex.message); }
      };
      list.append(li);
    });
  } catch (e) { emptyRow(list, e.message); }
}

function toggleCode(li, s) {
  const existing = li.querySelector(".code-wrap");
  if (existing) { existing.remove(); return; }
  const id = `code-${s.id}`;
  const wrap = document.createElement("div");
  wrap.className = "code-wrap stack";
  wrap.innerHTML = `<label for="${id}">Code for ${esc(s.name)} (read-only)</label>
    <textarea id="${id}" class="code-view" spellcheck="false" readonly></textarea>
    <p class="hint">To change it, ask me in chat: “upgrade skill ${esc(s.name)} so it also …”.</p>`;
  wrap.querySelector("textarea").value = s.code || "(code hidden)";
  li.querySelector(".item-main").append(wrap);
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
    const r = await api("/api/skills", { method: "POST", json: { name: $("#skill-name").value, request: $("#skill-request").value, secrets, is_public: $("#skill-public").checked } });
    e.target.reset();
    addMessage("assistant", `New skill **${r.name}** is ready${r.tested ? " and passed its self-test" : ""}. ${r.test_output ? "\n\n" + r.test_output : ""}`);
    loadSkills(); loadCommunitySkills();
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

async function startNewChat() {
  if (busy) return;
  if (els.thread.querySelectorAll(".msg").length && !confirm("Start a new chat? I keep my memories, but this conversation is archived.")) return;
  try {
    await api("/api/reset", { method: "POST" });
    clearMessages(); els.empty.hidden = false; setDrawer(false); els.input.focus();
  } catch (ex) { toast(ex.message); }
}
$("#new-chat-btn").addEventListener("click", startNewChat);
$("#reset-chat").addEventListener("click", async () => { await startNewChat(); closePanel(); });

$("#make-code").addEventListener("click", async () => {
  try {
    const r = await api("/api/link/code", { method: "POST" });
    $("#code-out").textContent = `/link ${r.code}`;
  } catch (ex) { toast(ex.message); }
});
$("#link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#link-error"); err.textContent = "";
  try {
    const r = await api("/api/link", { method: "POST", json: { code: $("#link-code").value } });
    token = r.token; store.set("dost_token", token); closePanel(true); enterChat();
  } catch (ex) { err.textContent = ex.message; }
});

/* ---------- photo upload ---------- */
const imgUploadInput = $("#image-upload");
$("#btn-upload-image").addEventListener("click", () => { if (!busy) imgUploadInput.click(); });
imgUploadInput.addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  imgUploadInput.value = "";
  const textPrompt = (els.input.value || "").trim();
  els.input.value = ""; autoGrow();
  addMessage("user", textPrompt || "Sent an image", [{ type: "image", url: URL.createObjectURL(file), caption: file.name }]);
  setBusy(true, "Looking at your photo…");
  const live = startBotMessage();
  try {
    const formData = new FormData();
    formData.append("file", file);
    if (textPrompt) formData.append("caption", textPrompt);
    const res = await fetch("/api/upload_image", {
      method: "POST", headers: token ? { Authorization: `Bearer ${token}` } : {}, body: formData,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to process image.");
    }
    const data = await res.json();
    live.msg.remove();
    addMessage("assistant", data.text, data.attachments || []);
  } catch (err) {
    live.fail(`I couldn't load that photo: ${err.message}`);
  } finally {
    setBusy(false);
  }
});

if (token) enterChat();
