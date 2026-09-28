// Session page: render the event timeline (history + live SSE), drive Stop,
// follow-ups, and the per-step prompt view.
(() => {
  const S = window.SESSION;
  const $ = (id) => document.getElementById(id);
  const timeline = $("timeline"), thinking = $("thinking"), jump = $("jump");
  const statusPill = $("status"), usageEl = $("usage");
  const input = $("input"), send = $("send"), hint = $("hint");
  const TERMINAL = new Set(["completed", "failed", "cancelled", "waiting_for_user"]);

  let status = S.status;
  let lastId = 0;
  let es = null;
  const cards = new Map();            // call_id -> tool card element
  const usage = { steps: 0, prompt: 0, completion: 0, cached: 0 };
  let stickToBottom = true;

  // ---------- helpers
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function md(text) {
    // tiny markdown: fenced code, inline code, bold, line breaks
    let out = "", parts = String(text ?? "").split(/```(\w*)\n?([\s\S]*?)```/g);
    for (let i = 0; i < parts.length; i += 3) {
      out += esc(parts[i]).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>");
      if (i + 2 < parts.length) out += `<pre><code>${esc(parts[i + 2])}</code></pre>`;
    }
    return out;
  }
  function el(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
  function add(node) {
    timeline.appendChild(node);
    if (stickToBottom) window.scrollTo({ top: document.body.scrollHeight }); else jump.hidden = false;
  }
  function callSummary(name, a) {
    a = a || {};
    if (name === "bash") return `$ ${a.command || ""}`;
    if (["read_file", "write_file", "edit_file"].includes(name)) return a.path || "";
    if (name === "search") return `/${a.pattern || ""}/ ${a.path || ""}`;
    if (name === "list_files") return a.path || "/workspace";
    return "";
  }
  function fmtTokens(n) { return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n); }
  function renderUsage() {
    const cachedPct = usage.prompt ? Math.round(100 * usage.cached / usage.prompt) : 0;
    usageEl.textContent = `${usage.steps} steps · ${fmtTokens(usage.prompt + usage.completion)} tokens (${cachedPct}% cached)`;
  }
  function setDiffBadge(d) {
    $("diff-badge").textContent = d.files ? `${d.files} · +${d.insertions} −${d.deletions}` : "0";
  }
  function renderDiff(patch) {
    if (!patch.trim()) return `<p class="muted">No changes yet.</p>`;
    return `<pre class="diff">${patch.split("\n").map((line) => {
      const cls = line.startsWith("+++") || line.startsWith("---") ? "d-file"
        : line.startsWith("diff --git") ? "d-head"
        : line.startsWith("@@") ? "d-hunk"
        : line.startsWith("+") ? "d-add"
        : line.startsWith("-") ? "d-del" : "";
      return `<span class="${cls}">${esc(line)}</span>`;
    }).join("\n")}</pre>`;
  }
  async function showDiff() {
    const dlg = $("diff"), body = $("diff-body");
    body.innerHTML = `<p class="muted">loading…</p>`;
    dlg.showModal();
    const r = await fetch(`/api/sessions/${S.id}/events`.replace("/events", "/diff"));
    if (!r.ok) { body.innerHTML = `<p class="sys-err">could not load diff (${r.status})</p>`; return; }
    const d = await r.json();
    setDiffBadge(d.stats);
    $("diff-title").textContent = `Changes — ${d.stats.files} file${d.stats.files === 1 ? "" : "s"}, +${d.stats.insertions} −${d.stats.deletions}${d.live ? "" : " (sandbox gone; showing saved patch)"}`;
    body.innerHTML = renderDiff(d.patch);
  }
  function setStatus(st) {
    status = st;
    statusPill.textContent = st;
    statusPill.className = `pill pill-${st}`;
    const running = st === "queued" || st === "running";
    thinking.hidden = !running;
    $("stop").disabled = !running;
    input.disabled = running; send.disabled = running;
    document.getElementById("composer").classList.toggle("composer-waiting", st === "waiting_for_user");
    hint.textContent = st === "waiting_for_user" ? "The agent is waiting for your answer."
      : running ? "Running — send is enabled when the agent stops."
      : st === "completed" ? "Send a follow-up to continue in the same sandbox." : "";
  }

  // ---------- renderers, one per event type
  const render = {
    "user.message": (p) => add(el(`<div class="msg msg-user"><div class="who">you</div><div class="body">${md(p.content)}</div></div>`)),
    "agent.message": (p) => add(el(`<div class="msg msg-agent"><div class="who">agent</div><div class="body">${md(p.content)}</div></div>`)),
    "sandbox.ready": (p) => add(el(`<div class="sys">sandbox ${esc((p.container_id || "").slice(0, 12))} ${p.reused ? "reattached" : "ready"}${p.repo_url ? " · cloned " + esc(p.repo_url) : ""}</div>`)),
    "session.status": (p) => { setStatus(p.status); if (p.error) add(el(`<div class="sys sys-err">${esc(p.error)}</div>`)); if (TERMINAL.has(p.status)) { $("project").hidden = false; if (es) { es.close(); es = null; } } },
    "session.error": (p) => add(el(`<div class="sys sys-err">${esc(p.message)}</div>`)),
    "llm.usage": (p) => {
      usage.steps += 1; usage.prompt += p.prompt_tokens; usage.completion += p.completion_tokens; usage.cached += p.cached_tokens;
      renderUsage();
      const node = el(`<div class="step"><span>step ${p.step}</span><span class="muted">${fmtTokens(p.prompt_tokens)}+${p.completion_tokens} tok · ${p.latency_ms} ms</span>${p.run_id ? `<a href="#" class="show-prompt">show prompt</a>` : ""}</div>`);
      if (p.run_id) node.querySelector(".show-prompt").onclick = (e) => { e.preventDefault(); showTrace(p.run_id, p.step); };
      add(node);
    },
    "agent.tool_call": (p) => {
      const isTerminal = p.name === "finish" || p.name === "ask_user";
      const card = el(`<details class="tool tool-${p.name}"${isTerminal ? " open" : ""}>
        <summary><span class="tool-name">${esc(p.name)}</span><span class="tool-arg">${esc(callSummary(p.name, p.arguments))}</span><span class="tool-meta">running…</span></summary>
        <div class="tool-body">${p.name === "write_file" || p.name === "edit_file" ? `<pre class="tool-input">${esc(p.arguments.content || p.arguments.new || "")}</pre>` : ""}<pre class="tool-out"></pre></div>
      </details>`);
      cards.set(p.call_id, card);
      add(card);
    },
    "agent.tool_result": (p) => {
      const card = cards.get(p.call_id);
      if (!card) return;
      const meta = [];
      if (p.exit_code !== null && p.exit_code !== undefined) meta.push(`exit ${p.exit_code}`);
      if (p.duration_ms) meta.push(`${p.duration_ms} ms`);
      if (p.truncated) meta.push("truncated");
      card.querySelector(".tool-meta").textContent = meta.join(" · ");
      card.querySelector(".tool-out").textContent = p.output;
      card.classList.toggle("tool-error", !!p.is_error);
      if (p.name === "finish") { card.querySelector(".tool-out").innerHTML = md(p.output); card.classList.add("tool-finish"); }
      if (p.name === "ask_user") card.classList.add("tool-ask");
    },
    "diff.updated": (p) => setDiffBadge(p),
    "agent.review": (p) => add(el(p.error
      ? `<div class="sys">review failed, finish accepted</div>`
      : p.approve
        ? `<div class="sys">reviewer approved</div>`
        : `<div class="sys">reviewer sent it back: ${p.issues.map(esc).join(" · ")}</div>`)),
    "context.compacted": (p) => add(el(`<div class="sys">context compacted: ${p.elided} old output${p.elided === 1 ? "" : "s"} elided, ${fmtTokens(p.before_tokens)} → ${fmtTokens(p.after_tokens)} tokens</div>`)),
    "agent.ask_user": (p) => { add(el(`<div class="msg msg-agent msg-ask"><div class="who">agent asks</div><div class="body">${md(p.question)}</div></div>`)); input.focus(); },
  };
  function handle(ev) {
    lastId = Math.max(lastId, ev.id);
    (render[ev.type] || (() => {}))(ev.payload);
  }

  // ---------- live stream
  function connect() {
    if (es) es.close();
    es = new EventSource(`/api/sessions/${S.id}/events?after=${lastId}`);
    for (const t of Object.keys(render)) es.addEventListener(t, (m) => handle(JSON.parse(m.data)));
    es.onerror = () => { /* EventSource reconnects on its own with Last-Event-ID */ };
  }

  // ---------- trace dialog
  async function showTrace(runId, step) {
    const dlg = $("trace"), body = $("trace-body");
    $("trace-title").textContent = `Step ${step} — what the model saw`;
    body.innerHTML = `<p class="muted">loading…</p>`;
    dlg.showModal();
    const r = await fetch(`/api/traces/${runId}/steps/${step}`);
    if (!r.ok) { body.innerHTML = `<p class="sys-err">no trace for this step (${r.status})</p>`; return; }
    const t = await r.json();
    const msgs = t.new_messages.map((m) => {
      const role = m.role === "tool" ? `tool → ${esc(m.tool_call_id)}` : m.role;
      const calls = (m.tool_calls || []).map((c) => `<div class="tc">${esc(c.function.name)}(${esc(c.function.arguments)})</div>`).join("");
      return `<div class="tmsg tmsg-${m.role}"><div class="who">${role}</div><pre>${esc(m.content || "")}</pre>${calls}</div>`;
    }).join("");
    const resp = t.response.tool_calls.map((c) => `<div class="tc">${esc(c.name)}(${esc(JSON.stringify(c.arguments))})</div>`).join("");
    body.innerHTML = `
      <p class="muted">${esc(t.model)} · request had ${t.total_messages} messages · tools: ${t.tools.map(esc).join(", ")} · ${t.usage.prompt_tokens}+${t.usage.completion_tokens} tokens (${t.usage.cached_tokens} cached) · ${t.latency_ms} ms</p>
      ${t.system_prompt ? `<details class="tsys"><summary>system prompt (${t.system_prompt.length} chars)</summary><pre>${esc(t.system_prompt)}</pre></details>` : ""}
      <h3>New messages this step</h3>${msgs || "<p class='muted'>none</p>"}
      <h3>Model reply</h3>${t.response.content ? `<pre>${esc(t.response.content)}</pre>` : ""}${resp}${t.error ? `<p class="sys-err">${esc(t.error)}</p>` : ""}`;
  }
  $("trace-close").onclick = () => $("trace").close();
  $("changes").onclick = showDiff;
  $("diff-close").onclick = () => $("diff").close();

  // ---------- controls
  $("stop").onclick = () => fetch(`/api/sessions/${S.id}/stop`, { method: "POST" });
  async function sendMessage() {
    const content = input.value.trim();
    if (!content) return;
    send.disabled = true;
    const r = await fetch(`/api/sessions/${S.id}/messages`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ content }) });
    if (!r.ok) { hint.textContent = `could not send (${r.status})`; send.disabled = false; return; }
    input.value = "";
    connect();
  }
  send.onclick = sendMessage;
  input.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") sendMessage(); });
  window.addEventListener("scroll", () => {
    stickToBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
    if (stickToBottom) jump.hidden = true;
  });
  jump.onclick = () => { window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); jump.hidden = true; };

  // ---------- boot: render history, then go live
  setStatus(status);
  for (const ev of window.EVENTS) handle(ev);
  renderUsage();
  if (!TERMINAL.has(status)) connect();
})();
