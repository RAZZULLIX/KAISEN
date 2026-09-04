// KAISEN framework dashboard — old style, new backend
const tabsContainer = document.getElementById('worker-tabs');
const panelsContainer = document.getElementById('worker-panels');
const emptyState = document.getElementById('empty-state');
const workerCountEl = document.getElementById('worker-count');
const lastUpdateEl = document.getElementById('last-update');
const NOTE_COLORS = ['#FF595E', '#FFCA3A', '#8AC926', '#1982C4', '#6A4C93', '#F15BB5', '#00F5D4', '#00BBF9'];

let activeTab = null;
let activeProjectId = null;
let activeSpec = null;
let notesData = [];
let showArchivedNotes = false;
let iterationsData = [];
let knownIterationIds = new Set();
let iterationPollTimer = null;
let iterationLimit = 100;
let llmControlState = 'running';
let llmPressTracked = false; // becomes true on the first button press
let liveAutoscrollEnabled = true;
let livePollInterval = null;
let logsPollInterval = null;
let metricSchema = {};
let championGen = null;
let scoreTypes = {};
let activeScoreKey = null;
let activeScoreDirection = 'higher';

function scoreOf(typeSpec, metrics) {
  if (!typeSpec || !metrics) return null;
  try {
    if (typeSpec.compose === 'product') {
      const keys = typeSpec.metrics || [];
      const vals = keys.map(k => metrics[k]).filter(v => v !== undefined && v !== null && v !== '');
      if (vals.length !== keys.length) return null;
      return vals.reduce((a, b) => a * Number(b), 1);
    }
    if (typeSpec.compose === 'single') {
      const v = metrics[typeSpec.metric];
      return (v === undefined || v === null || v === '') ? null : Number(v);
    }
    let total = 0, used = false;
    for (const [k, w] of Object.entries(typeSpec.weights || {})) {
      const v = metrics[k];
      if (v === undefined || v === null || v === '') continue;
      const weight = typeof w === 'object' ? Number(w.weight || 1) : Number(w);
      if (!weight) continue;
      total += weight * Number(v);
      used = true;
    }
    return used ? total : null;
  } catch (e) { return null; }
}
function fmtScore(v) {
  if (v === null || v === undefined || isNaN(v)) return '--';
  const a = Math.abs(v);
  if (a !== 0 && (a >= 1000 || a < 0.001)) return v.toExponential(4);
  return v.toFixed(4);
}
function populateScoreTypes(snap) {
  const s = (snap && snap.scores) || {};
  const types = s.types || {};
  const keys = Object.keys(types);
  if (!keys.length) { scoreTypes = {}; return; }
  const changed = JSON.stringify(scoreTypes) !== JSON.stringify(types);
  scoreTypes = types;
  const sel = document.getElementById('score-type-select');
  if (changed || sel.options.length !== keys.length) {
    sel.innerHTML = '';
    keys.forEach(k => {
      const opt = document.createElement('option');
      opt.value = k;
      opt.textContent = types[k].label || k;
      sel.appendChild(opt);
    });
  }
  if (s.active && types[s.active]) {
    if (sel.value !== s.active && !changed) sel.value = s.active;
    else if (changed) sel.value = s.active;
    activeScoreKey = s.active;
  } else {
    activeScoreKey = sel.value || keys[0];
  }
  const t = types[activeScoreKey] || {};
  activeScoreDirection = t.direction === 'lower' ? 'lower' : 'higher';
}
function changeScoreType() {
  const sel = document.getElementById('score-type-select');
  activeScoreKey = sel.value;
  const t = scoreTypes[activeScoreKey] || {};
  activeScoreDirection = t.direction === 'lower' ? 'lower' : 'higher';
  renderIterations();
  renderBestFromRows();
}

// ------------------------------------------------------------------ //
// helpers
// ------------------------------------------------------------------ //
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  // innerHTML escapes only & < > — add quotes so the result is safe inside
  // double-quoted attributes (title="...") as well as in text content.
  return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function systemAlert(message) {
  const modal = document.getElementById('system-modal');
  const text = document.getElementById('system-modal-text');
  const btn = document.getElementById('system-modal-ok');
  text.textContent = message;
  btn.style.display = 'inline-flex';
  document.getElementById('system-modal-cancel').style.display = 'none';
  modal.style.display = 'flex';
  btn.onclick = () => { modal.style.display = 'none'; };
}
function systemConfirm(message, onConfirm) {
  const modal = document.getElementById('system-modal');
  const text = document.getElementById('system-modal-text');
  const okBtn = document.getElementById('system-modal-ok');
  const cancelBtn = document.getElementById('system-modal-cancel');
  text.textContent = message;
  okBtn.style.display = 'inline-flex';
  cancelBtn.style.display = 'inline-flex';
  modal.style.display = 'flex';
  cancelBtn.onclick = () => { modal.style.display = 'none'; };
  okBtn.onclick = () => { modal.style.display = 'none'; if (onConfirm) onConfirm(); };
}
function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || isNaN(bytes)) return '--';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (bytes >= 1000 && i < units.length - 1) { bytes /= 1000; i++; }
  return `${bytes.toFixed(2)} ${units[i]}`;
}
function formatTime(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return '--';
  return `${Number(sec).toFixed(2)}s`;
}
function formatETA(etaStr) {
  if (etaStr === null || etaStr === undefined || etaStr === '--') return '--';
  let seconds;
  if (typeof etaStr === 'number') seconds = etaStr;
  else {
    const match = String(etaStr).match(/([\d.]+)s/);
    if (!match) return etaStr;
    seconds = parseFloat(match[1]);
  }
  if (seconds >= 3600) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds)}s`;
}
function formatTimestamp(ts) {
  if (!ts) return '--';
  return new Date(ts * 1000).toLocaleString();
}
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}
function closeModal(id) { document.getElementById(id).style.display = 'none'; }

// ------------------------------------------------------------------ //
// navigation
// ------------------------------------------------------------------ //
let currentView = 'dashboard';
let prevView = 'dashboard';
function switchView(viewName) {
  // No dependence on `event`: callable from buttons AND programmatically.
  if (viewName !== 'config') prevView = viewName;
  currentView = viewName;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === viewName));
  ['dashboard', 'projects', 'notes', 'config', 'debug'].forEach(v => {
    const el = document.getElementById(`view-${v}`);
    if (el) el.style.display = v === viewName ? 'block' : 'none';
  });
  if (viewName === 'projects') loadProjects();
  if (viewName === 'notes') loadNotes();
  if (viewName === 'config') switchSettingsTab(settingsTab || 'general');
  if (viewName === 'dashboard') { startIterationPolling(); loadActive(); }
}

// ------------------------------------------------------------------ //
// settings view — General / Active Project / LLM Servers
// ------------------------------------------------------------------ //
let settingsTab = 'general';
function openSettingsBar(tab) {
  switchView('config');
  if (tab) switchSettingsTab(tab);
}
function closeSettingsBar() {
  switchView(prevView);
}
function switchSettingsTab(tab) {
  if (tab === 'project' && projectTabHidden()) tab = 'general';
  settingsTab = tab;
  document.querySelectorAll('#view-config .tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  ['general', 'project', 'servers'].forEach(k => {
    const el = document.getElementById(`cfg-${k}`);
    if (el) el.style.display = k === tab ? 'block' : 'none';
  });
  const apply = document.getElementById('settings-apply');
  if (apply) apply.style.display = tab === 'servers' ? 'none' : 'inline-flex';
  if (tab === 'general') loadConfig();
  if (tab === 'project') loadProjectConfig();
  if (tab === 'servers') loadActive();
}

function setProjectTabVisible(visible) {
  const tab = document.querySelector('#view-config .tab[data-tab="project"]');
  if (tab) tab.style.display = visible ? '' : 'none';
}
function projectTabHidden() {
  const tab = document.querySelector('#view-config .tab[data-tab="project"]');
  return !tab || tab.style.display === 'none';
}
function toggleStatusExpand() {
  const expand = document.getElementById('status-expand');
  const pill = document.getElementById('status-pill');
  pill.classList.toggle('expanded', expand.style.display !== 'flex');
  expand.style.display = expand.style.display === 'flex' ? 'none' : 'flex';
  if (expand.style.display === 'flex') updateStatusPill();
}

// ------------------------------------------------------------------ //
// Welcome state: when no engine is running, hide the telemetry panels and
// show a clean picker instead of rows of empty divs.
function showWelcome(show) {
  const w = document.getElementById('welcome');
  if (!w) return;
  w.style.display = show ? 'flex' : 'none';
  const grid = document.getElementById('dash-grid');
  const iter = document.getElementById('iter-dash');
  if (grid) grid.style.display = show ? 'none' : '';
  if (iter) iter.style.display = show ? 'none' : '';
  const kpis = document.getElementById('metrics-card');
  if (kpis) kpis.style.display = show ? 'none' : '';
  const tag = document.getElementById('worker-count');
  if (tag) tag.style.display = show ? 'none' : '';
  const inject = document.getElementById('btn-inject-code');
  if (inject) inject.style.display = show ? 'none' : '';
}
async function fetchState() {
  try {
    const [wRes, sRes] = await Promise.all([api('/api/workers'), api('/api/state')]);
    if (wRes.no_engine || sRes.no_engine) return; // no engine — loadActive() owns the welcome state
    showWelcome(false);
    renderWorkers(wRes.workers || {}, wRes.schema || {}, wRes.telemetry || {}, sRes.best_metrics || {});
    document.getElementById('kpi-gen').textContent = sRes.generation ?? '--';
    if (wRes.multi != null) syncMultiFromServer(Number(wRes.multi));
  } catch (e) {
    // One failed poll tick is not "engine gone" — never flip the
    // dashboard to the welcome picker over a transient hiccup.
    // loadActive() is the authority on whether a project is active.
    console.error('state poll failed:', e);
  }
}
async function addWorker() {
  try {
    await api('/api/workers/add', { method: 'POST' });
    toast('Worker added.');
  } catch (e) { systemAlert('Add worker failed: ' + e.message); }
}
// ---- parallel gens (+/-): optimistic UI, coalesced absolute sends ----
let multiValue = 1;    // what the UI shows (source of truth for clicks)
let multiServer = 1;   // last server-confirmed value
let multiSending = false;

function renderMulti() {
  const el = document.getElementById('multi-count');
  if (el) el.textContent = multiValue;
}

function syncMultiFromServer(n) {
  // Adopt the server value only when no local change is pending,
  // so the 1s poll never clobbers an optimistic in-flight click.
  if (multiSending || multiValue !== multiServer) return;
  multiValue = multiServer = n;
  renderMulti();
}

async function pushMulti() {
  if (multiSending || multiValue === multiServer) return;
  multiSending = true;
  const target = multiValue;
  try {
    const r = await api('/api/engine/multi', { method: 'POST', body: JSON.stringify({ multi: target }) });
    if (!r || !r.ok || r.multi == null) throw new Error('multi request rejected');
    const n = Number(r.multi);
    if (n !== target) { multiValue = n; renderMulti(); } // server clamped — reflect truth
    if (multiValue !== multiServer) {
      // Clicks arrived while this round-trip was in flight: send the latest.
      multiSending = false;
      pushMulti();
      return;
    }
  } catch (e) {
    // Server didn't take it: snap back to the last confirmed value.
    console.error(e);
    multiValue = multiServer;
    renderMulti();
  }
  multiSending = false;
}

function adjustMulti(delta) {
  multiValue = Math.max(1, multiValue + delta);
  renderMulti();
  pushMulti();
}
async function killWorkerProcess(id) {
  systemConfirm(`Kill the process worker ${id} is currently running? The worker survives and returns to idle.`, async () => {
    try {
      const data = await api(`/api/workers/${id}/kill-process`, { method: 'POST' });
      systemAlert(data.message || (data.ok ? 'Process killed.' : 'Nothing to kill.'));
    } catch (e) { console.error(e); }
  });
}



// ------------------------------------------------------------------ //
// worker telemetry (old style: tabs + per-worker metric cards)
// ------------------------------------------------------------------ //
// worker telemetry — per-project metric cards, live from the harness
// ------------------------------------------------------------------ //

// Format a metric value for a card, honoring the metric schema's unit.
function fmtMetricValue(key, val, schema) {
  if (val === null || val === undefined || val === '') return '--';
  const num = Number(val);
  const spec = schema[key] || {};
  const unit = (spec.unit || '').toLowerCase();
  if (!isNaN(num)) {
    if (unit === 'bytes') return formatBytes(num);
    if (unit === 'b') return formatBytes(num);
    if (num >= 10000) return num.toLocaleString();
    if (Number.isInteger(num)) return String(num);
    return num.toFixed(2);
  }
  return String(val);
}

function fmtDuration(sec) {
  if (sec === null || sec === undefined) return '--';
  const s = Number(sec);
  if (isNaN(s)) return '--';
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60);
  return m + 'm ' + Math.floor(s % 60) + 's';
}

async function renderWorkers(workers, schema, telemetry, bestMetrics) {
  const ids = Object.keys(workers).map(Number).sort((a, b) => a - b);
  const runningCount = ids.filter(id => workers[id]?.status === 'running').length;
  workerCountEl.textContent = `${runningCount} ACTIVE`;
  if (lastUpdateEl) lastUpdateEl.textContent = `Sync: ${new Date().toLocaleTimeString()}`;
  if (ids.length === 0) {
    emptyState.style.display = 'flex';
    tabsContainer.innerHTML = '';
    panelsContainer.innerHTML = '';
    return;
  }
  emptyState.style.display = 'none';
  const idSet = new Set(ids.map(String));
  [...tabsContainer.children].forEach(c => { if (!idSet.has(c.dataset.id)) c.remove(); });
  [...panelsContainer.children].forEach(c => { if (!idSet.has(c.dataset.id)) c.remove(); });
  const existingTabs = new Set([...tabsContainer.children].map(c => c.dataset.id));
  ids.forEach(id => {
    if (!existingTabs.has(String(id))) {
      const tab = document.createElement('div');
      tab.className = 'tab';
      tab.dataset.id = id;
      tab.textContent = `Worker ${id}`;
      tab.setAttribute('role', 'button');
      tab.tabIndex = 0;
      tab.onclick = () => switchTab(id);
      tabsContainer.appendChild(tab);
      const panel = document.createElement('div');
      panel.className = 'worker-content';
      panel.dataset.id = id;
      panelsContainer.appendChild(panel);
    }
  });
  // Project metric cards: declared live fields (defaults to ALL metrics).
  let liveFields = (telemetry.live_fields || []).slice();
  if (!liveFields.length) liveFields = Object.keys(schema || {});
  ids.forEach(id => {
    const w = workers[id];
    const stage = (w.current_stage || w.status || 'idle').toLowerCase();
    const isIdle = w.status === 'idle';
    const isRunning = !isIdle;
    const stageDisplay = isIdle ? 'IDLE' : stage.replace(/_/g, ' ').toUpperCase();
    let tabStateClass = 'tab-idle';
    let workerStateClass = 'worker-idle';
    if (isRunning) {
      if (stage.includes('score')) { tabStateClass = 'tab-decompressing'; workerStateClass = 'worker-decompressing'; }
      else if (stage.includes('build')) { tabStateClass = 'tab-compressing-yellow'; workerStateClass = 'worker-compressing'; }
      else { tabStateClass = 'tab-compressing'; workerStateClass = 'worker-compressing'; }
      // Live pressure color: how close is a lower-better metric to the best?
      for (const key of liveFields) {
        const spec = schema[key] || {};
        const live = Number(w.live && w.live[key]);
        const best = Number(bestMetrics[key]);
        if (spec.direction === 'lower' && !isNaN(live) && !isNaN(best) && best > 0) {
          const ratio = (best - live) / best;
          if (ratio <= 0.01) { tabStateClass = 'tab-compressing-red'; }
          else if (ratio <= 0.10) { tabStateClass = 'tab-compressing-yellow'; }
          break;
        }
      }
    }
    const panel = panelsContainer.querySelector(`.worker-content[data-id="${id}"]`);
    if (panel) {
      panel.className = `worker-content ${workerStateClass}`;
      if (activeTab == id) panel.classList.add('active');
      let cards = '';
      cards += `<div class="metric-card"><div class="metric-label">Current Stage</div><div class="metric-val ${isIdle ? 'ok' : 'warn'}">${stageDisplay}</div></div>`;
      cards += `<div class="metric-card"><div class="metric-label">Generation</div><div class="metric-val">${w.generation ?? 'N/A'}</div></div>`;
      // One card per project metric (the project's custom cards).
      for (const key of liveFields) {
        const spec = schema[key] || {};
        const dir = spec.direction === 'lower' ? '▼' : '▲';
        const live = w.live && w.live[key];
        const final = w.result_metrics && w.result_metrics[key];
        const val = (live !== undefined && live !== null) ? live : final;
        const label = spec.label || key;
        cards += `<div class="metric-card"><div class="metric-label">${escapeHtml(label)} ${dir}</div><div class="metric-val">${fmtMetricValue(key, val, schema)} <span style="font-size:10px;color:var(--muted);">${escapeHtml(spec.unit || '')}</span></div></div>`;
      }
      if (isRunning) {
        if (w.model && w.model !== '—') {
          cards += `<div class="metric-card"><div class="metric-label">Model Routed</div><div class="metric-val">${escapeHtml(String(w.model))}</div></div>`;
        }
        if (w.current_ram) {
          cards += `<div class="metric-card"><div class="metric-label">Live RAM</div><div class="metric-val">${formatBytes(w.current_ram)}</div></div>`;
        }
        cards += `<div class="metric-card"><div class="metric-label">Elapsed</div><div class="metric-val">${fmtDuration(w.elapsed)}</div></div>`;
      } else if (w.outcome) {
        const ok = w.result_ok;
        cards += `<div class="metric-card"><div class="metric-label">Outcome</div><div class="metric-val ${ok ? 'ok' : 'err'}">${escapeHtml(String(w.outcome).toUpperCase())}</div></div>`;
        const t = w.result_timings || {};
        if (t.build != null || t.score0 != null) {
          const total = (Object.values(t).reduce((a, b) => a + (Number(b) || 0), 0));
          cards += `<div class="metric-card"><div class="metric-label">Pipeline Time</div><div class="metric-val">${fmtDuration(total)}</div></div>`;
        }
      }
      panel.innerHTML = `
        <div class="metrics-grid">
          ${cards}
        </div>
        <div class="action-row">
          <div class="path-display" title="${escapeHtml(w.temp_dir || 'No active directory')}">${escapeHtml(w.temp_dir || 'No active directory')}</div>
          <button class="btn btn-primary" onclick="openFolder('${escapeHtml(w.temp_dir || '')}')"><span>📂</span> Open Folder</button>
          ${isRunning ? `<button class="btn" style="border-color: var(--warning); color: var(--warning);" onclick="killWorkerProcess(${id})" title="Kill the running harness/candidate process — the worker survives">☠ Kill Process</button>` : ''}
          <button class="btn btn-danger" onclick="killWorker(${id})"><span>⚠</span> Kill Worker</button>
        </div>
      `;
    }
    const tab = tabsContainer.querySelector(`.tab[data-id="${id}"]`);
    if (tab) {
      tab.className = `tab ${tabStateClass}`;
      tab.textContent = isRunning ? `● Worker ${id} ●` : `Worker ${id}`;
      if (activeTab == id) tab.classList.add('active');
    }
  });
  if (activeTab === null && ids.length > 0) switchTab(ids[0]);
}
function switchTab(id) {
  activeTab = id;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.id == id));
  document.querySelectorAll('.worker-content').forEach(p => p.classList.toggle('active', p.dataset.id == id));
}
async function killWorker(id) {
  systemConfirm(`Terminate worker ${id}?`, async () => {
    try {
      const data = await api(`/api/workers/${id}/kill`, { method: 'POST' });
      if (data.ok) systemAlert(data.message);
    } catch (e) { console.error(e); }
  });
}
async function openFolder(path) {
  if (!path) return;
  try {
    const data = await api(`/open_folder/${encodeURIComponent(path)}`);
    if (!data.ok) systemAlert('Failed to open folder: ' + (data.error || ''));
  } catch (e) { console.error(e); }
}

// ------------------------------------------------------------------ //
// status pill — buttons show the LAST PRESSED request; the pill shows the
// system's ACTUAL state.  pause = drain then stop (both buttons light up
// while draining, pill stays green), stop = kill everything now.
// ------------------------------------------------------------------ //
async function handleLlmControl(action) {
  try {
    if (action === 'stop') {
      await api('/api/llm/stop', { method: 'POST' });
      llmControlState = 'stopped';
    } else if (action === 'pause') {
      await api('/api/override/llm_pause', { method: 'POST', body: 'true' });
    } else if (action === 'play') {
      const r = await api('/api/llm/resume', { method: 'POST' });
      if (r && r.error) {
        // start was refused (e.g. missing toolchain) — keep the button state
        // honest and tell the user why instead of pretending it ran.
        updateLlmButtons();
        updateStatusPill();
        systemAlert(r.error);
        return;
      }
      llmControlState = 'running';
    }
    llmPressTracked = true;
    updateLlmButtons();
    updateStatusPill();
  } catch (e) {
    console.error('LLM control failed', e);
    systemAlert('Command transmission failed.');
  }
}
function updateLlmButtons(engineState, generating) {
  const playBtn = document.getElementById('btn-play');
  const pauseBtn = document.getElementById('btn-pause');
  const stopBtn = document.getElementById('btn-stop');
  if (!playBtn || !pauseBtn || !stopBtn) return;
  [playBtn, pauseBtn, stopBtn].forEach(b => { b.className = 'llm-btn'; });
  if (llmControlState === 'running') {
    playBtn.classList.add('active-play');
  } else if (llmControlState === 'pausing') {
    // pause is the active request; play stays lit while the drain runs.
    pauseBtn.classList.add('active-pause');
    if (engineState === 'running' || engineState === 'pausing' || generating) {
      playBtn.classList.add('active-play');
    }
  } else if (llmControlState === 'stopped') {
    stopBtn.classList.add('active-stop');
  }
}
async function updateStatusPill() {
  try {
    const [statusData, activeData] = await Promise.all([
      api('/api/llm/status'), api('/api/active').catch(() => null),
    ]);
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className = 'status-dot';
    const engines = (activeData && activeData.engines) || [];
    const hasPool = !!(activeData && Array.isArray(activeData.engines));
    renderFleet(engines);
    if ((statusData && statusData.no_engine) || (hasPool && engines.length === 0)) {
      dot.classList.add('red');
      text.textContent = 'SYSTEM DOWN';
      document.getElementById('detail-project').textContent = 'none';
      document.getElementById('detail-status').textContent = 'NO ENGINE';
      document.getElementById('detail-guardrails').textContent = '--';
      renderLlmRows([]);
      updateLlmButtons('stopped', false);
      return;
    }
    const aggTps = statusData.agg_tps || statusData.tps || 0;
    const es = statusData.engine_state || 'unknown';
    // Before the user presses anything, mirror the real state so a reload
    // while paused/stopped shows the right button lit.
    if (!llmPressTracked) {
      if (es === 'paused' || es === 'pausing') llmControlState = 'pausing';
      else if (es === 'stopped' || es === 'stopping') llmControlState = 'stopped';
      else llmControlState = 'running';
    }
    const servers = statusData.servers || [];
    const selectedIds = [...new Set(servers.filter(s => s.active).map(s => s.id))];
    const anyOffline = servers.some(s => s.active && (s.banned || s.online === false));
    const generating = statusData.status === 'generating' && aggTps > 0;
    if (generating) {
      text.textContent = es === 'pausing'
        ? `PAUSING AFTER THIS @ ${aggTps.toFixed(1)} TPS`
        : `GENERATING @ ${aggTps.toFixed(1)} TPS`;
    } else if (es === 'pausing') {
      text.textContent = 'PAUSING AFTER THIS';
    } else if (statusData.status === 'paused') {
      text.textContent = 'SYSTEM PAUSED';
    } else if (statusData.status === 'stopped') {
      text.textContent = 'SYSTEM STOPPED';
    } else if (statusData.status === 'generating') {
      // Producers are running but no tokens flow: never call it GENERATING.
      text.textContent = 'ACTIVE';
    } else {
      text.textContent = 'IDLE';
    }
    // Engine pool: more than one engine → summarize the pool instead of a
    // single engine's generation state (single engine keeps the label above).
    if (hasPool && engines.length > 1) {
      text.textContent = `SYSTEM — ${engines.length} engines`;
    }
    // The pill LED reflects the selected servers: red when any is not
    // answering, green when all are available, yellow when none selected.
    if (statusData.status === 'stopped') {
      dot.classList.add('red');
    } else if (anyOffline) {
      dot.classList.add('red');
    } else if (selectedIds.length === 0) {
      dot.classList.add('yellow');
    } else {
      dot.classList.add('green');
    }
    document.getElementById('detail-project').textContent = activeData ? activeData.project_id : 'none';
    const statusEl = document.getElementById('detail-status');
    const esLabel = es === 'pausing' ? 'PAUSING (DRAINING)' : es === 'paused' ? 'PAUSED' : es === 'stopped' ? 'STOPPED' : es === 'running' ? 'RUNNING' : es.toUpperCase();
    statusEl.textContent = esLabel;
    statusEl.style.color = generating ? 'var(--accent)' : statusData.status === 'paused' ? 'var(--warning)' : statusData.status === 'stopped' ? 'var(--danger)' : 'var(--muted)';
    const gr = activeData && activeData.guardrails;
    const grEl = document.getElementById('detail-guardrails');
    grEl.textContent = gr && gr.global_off ? 'OFF' : (gr && !gr.project_enabled ? 'PROJECT OFF' : 'ON');
    grEl.style.color = gr && (gr.global_off || !gr.project_enabled) ? 'var(--danger)' : 'var(--accent)';
    renderLlmRows(statusData.servers || []);
    updateLlmButtons(es, statusData.generating);
  } catch (e) { console.warn('Status poll failed', e); }
}

// ------------------------------------------------------------------ //
// active engines fleet panel — one row per pool engine
// ------------------------------------------------------------------ //
function renderFleet(engines) {
  const c = document.getElementById('fleet-rows');
  if (!c) return;
  const list = Array.isArray(engines) ? engines : [];
  if (!list.length) {
    c.innerHTML = '<div class="empty-state fleet-empty"><span>No engines running — pick a project and press Run.</span></div>';
    return;
  }
  c.innerHTML = '';
  list.forEach(e => {
    if (!e || e.project_id == null) return;
    const id = e.project_id;
    const running = e.engine_state === 'running';
    const paused = e.engine_state === 'paused' || e.paused === true;
    const stateCls = running ? 'running' : paused ? 'paused' : 'stopped';
    const gen = e.generation != null ? e.generation : '?';
    const best = fmtScore(e.best_fitness);
    const metricBits = Object.entries(e.best_metrics || {})
      .map(([k, v]) => `${escapeHtml(k)}=${escapeHtml(fmtScore(v))}`)
      .join(' ');
    const name = e.name != null ? e.name : String(id);
    const row = document.createElement('div');
    row.className = 'fleet-row';
    row.innerHTML = `
      <span class="fleet-dot ${stateCls}"></span>
      <span class="fleet-name">${escapeHtml(name)}<span class="fleet-id">${escapeHtml(String(id))}</span></span>
      <span class="fleet-stat">gen <b>${escapeHtml(String(gen))}</b></span>
      <span class="fleet-stat">best <b>${escapeHtml(best)}</b>${metricBits ? ' <span class="fleet-metrics">' + metricBits + '</span>' : ''}</span>
      <span class="fleet-stat">multi <b>${escapeHtml(String(e.multi != null ? e.multi : 1))}</b></span>
      <span class="fleet-stat">nw <b>${escapeHtml(String(e.workers != null ? e.workers : 0))}</b></span>
      ${e.engine_error ? `<span class="fleet-error" title="${escapeHtml(e.engine_error)}">${escapeHtml(e.engine_error)}</span>` : ''}
      <span class="fleet-actions">
        <button class="btn btn-sm" onclick="switchProject('${id}')">Select</button>
        <button class="btn btn-sm" onclick="fleetTogglePause('${id}', ${paused ? 'false' : 'true'})">${paused ? 'Resume' : 'Pause'}</button>
        <button class="btn btn-sm" onclick="stopEngine('${id}')">Stop</button>
      </span>`;
    c.appendChild(row);
  });
}

async function fleetTogglePause(id, paused) {
  const wantPause = paused === true || paused === 'true';
  try {
    const r = await api('/api/engine/pause', { method: 'POST', body: JSON.stringify({ project_id: id, paused: wantPause }) });
    if (r && r.error) { systemAlert(r.error); return; }
    toast(wantPause ? 'Engine paused' : 'Engine resumed');
  } catch (e) {
    console.error('Pause toggle failed', e);
    systemAlert('Pause failed: ' + e.message);
  }
}

// Per-LLM rows in the pill: one clickable row per ACTIVE CHAT, numbered
// when a server runs more than one.  Clicking opens that chat's view.
function renderLlmRows(servers) {
  const c = document.getElementById('llm-rows');
  if (!c) return;
  c.innerHTML = '';
  servers.forEach(s => {
    const stateCls = !s.active || !s.enabled ? 'disabled'
      : (s.banned || s.online === false) ? 'banned'
      : (s.streaming && s.tps > 0) ? 'streaming'
      : 'busy';
    const el = document.createElement('div');
    el.className = 'llm-row';
    el.setAttribute('role', 'button');
    el.tabIndex = 0;
    el.setAttribute('aria-label', `Open live chat view for ${s.display || s.id}`);
    el.title = `click to see ${s.display} — ${s.id}`;
    el.onclick = () => openServerChat(s.id, s.chat_slot);
    el.innerHTML = `
      <span class="llm-dot ${stateCls}"></span>
      <span class="llm-name">${escapeHtml(s.display)}</span>
      <span class="llm-model">${escapeHtml(s.model || s.type || '')}</span>
      <span class="llm-stats">${s.requests ?? 0} req · ${s.failures ?? 0} fail${s.avg_seconds ? ' · ' + Number(s.avg_seconds).toFixed(0) + 's' : ''} · ${(s.tps ?? 0).toFixed(1)} tps</span>`;
    c.appendChild(el);
  });
}

// ------------------------------------------------------------------ //
// live generations window (source selectable)
// ------------------------------------------------------------------ //
function handleLiveContainerScroll() {
  const container = document.getElementById('live-output-content');
  if (!container) return;
  liveAutoscrollEnabled = container.scrollHeight - container.scrollTop - container.clientHeight <= 5;
}
let liveServer = null;  // which endpoint's chats to focus (scroll target)
let liveSlot = null;    // which chat to scroll to (focus, not filter)
function openServerChat(serverId, slot) {
  liveServer = serverId;
  liveSlot = slot != null ? slot : null;
  openLiveModal();
}
function openLiveModal() {
  const modal = document.getElementById('live-output-modal');
  modal.style.display = 'flex';
  liveAutoscrollEnabled = true;
  clearInterval(livePollInterval);
  livePollInterval = setInterval(fetchLiveOutput, 150);
  fetchLiveOutput();
  const container = document.getElementById('live-output-content');
  container.removeEventListener('scroll', handleLiveContainerScroll);
  container.addEventListener('scroll', handleLiveContainerScroll);
  requestAnimationFrame(() => { container.scrollTop = container.scrollHeight; });
}
function closeLiveModal() {
  document.getElementById('live-output-modal').style.display = 'none';
  clearInterval(livePollInterval);
  livePollInterval = null;
  const container = document.getElementById('live-output-content');
  if (container) container.removeEventListener('scroll', handleLiveContainerScroll);
}

async function fetchLiveOutput() {
  try {
    // ALWAYS fetch every active chat — one pill per chat, never filtered.
    const data = await api('/api/llm/live');
    const container = document.getElementById('live-output-content');
    if (!container) return;
    renderLiveSessions(container, data);
    if (liveAutoscrollEnabled) container.scrollTop = container.scrollHeight;
    // Focus the clicked chat (if any): scroll its pill into view.
    if (liveSlot != null) {
      const target = [...container.querySelectorAll('.live-chat-card')]
        .find(card => card.dataset.slot === String(liveSlot));
      if (target) {
        target.scrollIntoView({ block: 'start', behavior: 'smooth' });
        target.classList.add('flash');
        setTimeout(() => target.classList.remove('flash'), 1600);
      }
      liveSlot = null;
    }
  } catch (e) { console.warn('Live poll failed', e); }
}
function renderLiveSessions(container, data) {
  // ONE PILL PER CHAT: every active chat gets its own console block,
  // numbered after the server name when a server runs more than one.
  const sessions = (data.sessions || []).filter(s => s.status === 'generating' || s.waiting);
  if (!sessions.length) {
    container.innerHTML = `<div class="console-line status-line" style="color:var(--muted);">${liveServer ? `No active chat on ${escapeHtml(liveServer)}.` : 'No active generation — press play.'}</div>`;
    container._liveChats = new Map();
    return;
  }
  const perServer = {};
  sessions.forEach(s => { perServer[s.server_id] = (perServer[s.server_id] || 0) + 1; });
  // Legacy autoscroll contract: DOM nodes REUSED and updated in place —
  // no rebuilds, no phantom scroll events.
  const chats = container._liveChats || (container._liveChats = new Map());
  const seen = new Set();
  for (const s of sessions) {
    seen.add(s.id);
    let el = chats.get(s.id);
    if (!el) {
      const root = document.createElement('div');
      root.className = 'live-chat-card';
      root.dataset.slot = s.slot;
      root.innerHTML = `
        <div class="console-line chat-title-line"></div>
        <div class="console-line">&gt; PROMPT:</div>
        <div class="console-block prompt-block"></div>
        <div class="console-line" style="margin-top:10px;">&gt; OUTPUT:</div>
        <div class="console-block output-block"></div>
        <div class="console-line status-line"></div>`;
      el = {
        root,
        title: root.querySelector('.chat-title-line'),
        prompt: root.querySelector('.prompt-block'),
        output: root.querySelector('.output-block'),
        status: root.querySelector('.status-line'),
      };
      chats.set(s.id, el);
    }
    const name = s.display || s.server_id || '';
    const chatLabel = s.waiting
      ? 'WAITING FOR FREE LLM'
      : (perServer[s.server_id] > 1 ? `${name} ${s.slot}` : name);
    el.title.textContent = `> ${chatLabel} · gen ${s.gen}`;
    el.prompt.textContent = s.prompt || 'Awaiting input...';
    el.output.textContent = s.text || '… waiting for first token …';
    el.status.textContent = s.waiting ? '[WAITING — ALL SERVERS BUSY/BANNED/DISABLED]' : `[STREAMING ACTIVE] ${s.tps.toFixed(1)} TPS`;
    el.status.style.color = s.waiting ? 'var(--muted)' : 'var(--warning)';
    container.appendChild(el.root);
  }
  for (const [id, el] of [...chats.entries()]) {
    if (!seen.has(id)) {
      el.root.remove();
      chats.delete(id);
    }
  }
}






// ------------------------------------------------------------------ //
// iterations + analytics chart
// ------------------------------------------------------------------ //
function updateIterationLimit() {
  const val = document.getElementById('iter-limit').value;
  iterationLimit = val === 'all' ? 'all' : parseInt(val, 10);
  renderIterations();
}
// Sortable headers: click toggles asc/desc; default = newest generation first.
let iterSortKey = 'iteration';
let iterSortDir = 'desc';
const TEXT_COLUMNS = new Set(['outcome', 'prompt_snippet', 'detail']);
function sortIterations(key) {
  if (iterSortKey === key) {
    iterSortDir = iterSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    // First click on a column: ascending (spreadsheet convention); for
    // lower-better fitness this puts the best score on top.
    iterSortKey = key;
    iterSortDir = 'asc';
  }
  renderIterations();
}

function iterSortValue(item, key) {
  const v = item[key];
  if (v === null || v === undefined || v === '') return null;
  if (TEXT_COLUMNS.has(key)) return String(v).toLowerCase();
  return Number(v);
}
function renderIterations() {
  const tbody = document.getElementById('iteration-tbody');
  if (!tbody) return;
  const search = (document.getElementById('iter-search')?.value || '').toLowerCase();
  const outcomeFilter = document.getElementById('iter-outcome-filter')?.value || '';
  const typeSpec = scoreTypes[activeScoreKey] || {};
  let scored = iterationsData.map(item => ({
    ...item,
    score: scoreOf(typeSpec, item.metrics || {}),
  }));

  let filtered = scored.filter(item => {
    if (outcomeFilter && item.outcome !== outcomeFilter) return false;
    if (search && !((item.outcome || '').toLowerCase().includes(search) || (item.detail || '').toLowerCase().includes(search) || (item.prompt_snippet || '').toLowerCase().includes(search))) return false;
    const minT = parseFloat(document.getElementById('iter-min-time')?.value);
    const maxT = parseFloat(document.getElementById('iter-max-time')?.value);
    if (!isNaN(minT) && (Number(item.gen_time) || 0) < minT) return false;
    if (!isNaN(maxT) && (Number(item.gen_time) || 0) > maxT) return false;
    return true;
  });
  // Sort by the clicked header (default: newest generation first).
  filtered.sort((a, b) => {
    const va = iterSortValue(a, iterSortKey);
    const vb = iterSortValue(b, iterSortKey);
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    let cmp;
    if (TEXT_COLUMNS.has(iterSortKey)) cmp = va < vb ? -1 : va > vb ? 1 : 0;
    else cmp = va - vb;
    return iterSortDir === 'asc' ? cmp : -cmp;
  });
  const sliced = iterationLimit === 'all' ? filtered : filtered.slice(0, iterationLimit);
  // Header arrow indicators.
  tbody.innerHTML = '';
  if (!sliced.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="6" style="text-align:center;color:var(--muted);padding:18px;">No iterations recorded yet — press play and generations appear here.</td>';
    tbody.appendChild(tr);
  }
  sliced.forEach(item => {
    const tr = document.createElement('tr');
    const outcomeClass = item.outcome === 'NEW_BEST' ? 'iter-ok' : (item.outcome || '').includes('fail') || (item.outcome || '').includes('error') ? 'iter-err' : 'iter-warn';
    tr.innerHTML = `
      <td>${item.iteration}</td>
      <td class="${outcomeClass}">${escapeHtml(item.outcome)}</td>
      <td>${fmtScore(item.score)}</td>
      <td>${item.gen_time ? Number(item.gen_time).toFixed(2) : '--'}</td>
      <td class="iter-prompt" title="${escapeHtml(item.prompt_snippet)}">${escapeHtml(item.prompt_snippet)}</td>
      <td class="iter-detail" title="${escapeHtml(item.detail)}">${escapeHtml(item.detail)}</td>
    `;
    tbody.appendChild(tr);
  });
  document.querySelectorAll('.iteration-table th.sortable').forEach(th => {
    const arrow = th.querySelector('.sort-arrow');
    if (!arrow) return;
    arrow.textContent = th.dataset.sort === iterSortKey ? (iterSortDir === 'asc' ? '▲' : '▼') : '';
  });

  drawScoreChart(scored.filter(i => i.score !== null));
}
function drawScoreChart(scored) {
  const canvas = document.getElementById('score-chart');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = 160 * dpr;
  canvas.style.height = '160px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = 160;
  ctx.clearRect(0, 0, W, H);
  const pts = scored
    .slice()
    .sort((a, b) => a.iteration - b.iteration)
    .map(i => ({ x: i.iteration, y: i.score }));
  if (pts.length < 1) {
    ctx.fillStyle = '#555';
    ctx.font = '12px monospace';
    ctx.fillText('no scored generations yet', 12, H / 2);
    return;
  }
  const pad = 12;
  const x0 = pad, x1 = W - pad, y0 = pad, y1 = H - pad;
  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  const xmin = Math.min(...xs), xmax = Math.max(...xs, xmin + 1);
  const ymin = Math.min(...ys), ymax = Math.max(...ys, ymin + 1e-9);
  const sx = (x) => x0 + ((x - xmin) / (xmax - xmin)) * (x1 - x0);
  const sy = (y) => y1 - ((y - ymin) / (ymax - ymin)) * (y1 - y0);
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  for (let g = 0; g <= 4; g++) {
    const gy = y0 + (g / 4) * (y1 - y0);
    ctx.beginPath(); ctx.moveTo(x0, gy); ctx.lineTo(x1, gy); ctx.stroke();
  }
  ctx.strokeStyle = 'rgba(0,232,124,0.85)';
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  pts.forEach((p, i) => {
    const px = sx(p.x), py = sy(p.y);
    if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  });
  ctx.stroke();
  // best point marker
  const bestIdx = pts.reduce((bi, p, i, arr) => (activeScoreDirection === 'lower' ? p.y < arr[bi].y : p.y > arr[bi].y) ? i : bi, 0);
  const bp = pts[bestIdx];
  ctx.fillStyle = '#00e87c';
  ctx.beginPath();
  ctx.arc(sx(bp.x), sy(bp.y), 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = '#888';
  ctx.font = '10px monospace';
  ctx.fillText(`score ${fmtScore(ymax)}`, x0 + 4, y0 + 10);
  ctx.fillText(`gen ${xmin}`, x0, y1 - 2);
  ctx.fillText(`gen ${xmax}`, x1 - 40, y1 - 2);
}
function renderBestFromRows() {
  const typeSpec = scoreTypes[activeScoreKey] || {};
  let best = null;
  iterationsData.forEach(item => {
    const s = scoreOf(typeSpec, item.metrics || {});
    if (s === null) return;
    if (best === null) best = { score: s, item };
    else if (activeScoreDirection === 'lower' ? s < best.score : s > best.score) best = { score: s, item };
  });
  const fitEl = document.getElementById('kpi-fitness');
  if (!best) { fitEl.textContent = '--'; return; }
  fitEl.textContent = fmtScore(best.score);
  document.getElementById('kpi-fitness-delta').textContent = activeScoreKey || '';
  // metric chips: raw values of the best row under this score
  const card = document.getElementById('metrics-card');
  card.querySelectorAll('.metric-chip').forEach(c => c.remove());
  for (const [key, spec] of Object.entries(metricSchema)) {
    const val = best.item.metrics ? best.item.metrics[key] : undefined;
    const chip = document.createElement('div');
    chip.className = 'kpi-pill metric-chip';
    const dir = spec.direction === 'lower' ? '▼' : '▲';
    chip.innerHTML = `<div><div class="kpi-label">${escapeHtml(spec.label || key)} ${dir}</div><div class="kpi-value">${val !== undefined && val !== null && val !== '' ? Number(val).toFixed(4) : '--'} <span style="font-size:10px;color:var(--muted);">${escapeHtml(spec.unit || '')}</span></div></div><div class="kpi-delta">w=${spec.weight}</div>`;
    card.appendChild(chip);
  }
}
async function pollIterations() {
  try {
    const data = await api('/api/iterations');
    const all = data.iterations || [];
    const newItems = all.filter(item => !knownIterationIds.has(item.iteration));
    if (newItems.length > 0) {
      newItems.forEach(item => knownIterationIds.add(item.iteration));
      iterationsData = [...iterationsData, ...newItems];
    }
    renderIterations();
    renderBestFromRows();
  } catch (e) { console.warn('Iteration poll failed', e); }
}
function startIterationPolling() {
  if (iterationPollTimer) return;
  pollIterations();
  iterationPollTimer = setInterval(pollIterations, 2000);
}

// ------------------------------------------------------------------ //
// LLM servers (dashboard mini + servers view)
// ------------------------------------------------------------------ //


async function loadActive() {
  let snap = null;
  try {
    const s = await api('/api/active');
    if (!s.no_engine) snap = s; // no_engine = clean "no engine" signal (200, not an error)
  } catch (e) { /* no engine running — same path as below */ }
  if (snap) {
    activeProjectId = snap.project_id;
    setProjectTabVisible(true);
    metricSchema = snap.metrics_schema || {};
    populateScoreTypes(snap);
    showWelcome(false);
    document.getElementById('dash-project-title').textContent = snap.project_name;
    document.getElementById('dash-breadcrumbs').textContent = `PROJECT · ${snap.project_id.toUpperCase()}`;
    if (settingsTab === 'servers' && currentView === 'config') { renderServers(snap.llm); loadModelStats(); }
    if (iterationsData.length) { renderIterations(); renderBestFromRows(); }
    return;
  }
  metricSchema = {};
  showWelcome(true);
  document.getElementById('dash-project-title').textContent = 'No active project';
  document.getElementById('dash-breadcrumbs').textContent = 'PROJECT';
  setProjectTabVisible(!!settingsProjectId);
  if (settingsTab === 'project' && !settingsProjectId) switchSettingsTab('general');
  // No engine running (fresh install): still show the server registry
  // from config.json so Settings → LLM Servers works pre-launch.
  if (settingsTab === 'servers' && currentView === 'config') {
    try {
      const c = await api('/api/config');
      renderServers(c.llm || {});
    } catch (e2) { console.warn('servers fallback failed', e2); }
  }
}

function renderServers(llm) {
  const tbody = document.getElementById('servers-tbody');
  tbody.innerHTML = '';
  const servers = llm.servers || [];

  if (!servers.length) { tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;color:var(--muted);">No servers.</td></tr>'; return; }
  servers.forEach(s => {
    const active = (llm.active_ids || []).includes(s.id);
    const tr = document.createElement('tr');
    tr.id = `server-row-${s.id}`;
    tr.innerHTML = `
      <td><input type="checkbox" ${active ? 'checked' : ''} onchange="toggleServerActive('${s.id}', this.checked)"></td>
      <td class="llm-label-cell" data-label="${escapeHtml(s.label || '')}"><b>${escapeHtml(s.label || s.id)}</b>${s.label && s.label !== s.id ? `<div class="iter-prompt" style="font-size:10px;color:var(--muted);">${escapeHtml(s.id)}</div>` : ''}</td><td>${escapeHtml(s.type)}</td><td class="iter-prompt" style="max-width:260px;" title="${escapeHtml(s.url)}">${escapeHtml(s.url)}</td>
      <td>${escapeHtml(s.model || '')}</td><td>${escapeHtml(s.tier || 'small')}</td><td>${escapeHtml(s.priority ?? 1)}</td><td>${s.context_window ? s.context_window : '?'}</td><td>${s.inflight ?? 0}/${s.max_concurrent ?? '—'}</td>
      <td class="${s.banned ? 'iter-err' : s.busy ? 'iter-warn' : s.online === false ? 'iter-err' : 'iter-ok'}">${s.banned ? 'BANNED' : s.busy ? 'busy' : s.online === false ? 'offline' : 'ok'}</td>
      <td>${(s.stats ? `${s.stats.requests || 0} req · ${s.stats.failures || 0} fail${s.stats.avg_seconds ? ' · ' + Number(s.stats.avg_seconds).toFixed(0) + 's' : ''}` : '—')}</td>
      <td><button class="btn btn-sm" title="Rename this endpoint" onclick="startRenameServer('${s.id}')">✎</button> <button class="btn btn-sm" title="Probe the endpoint" onclick="healthCheck('${s.id}')">health</button> <button class="btn btn-sm" title="Remove this endpoint" style="border-color:var(--danger);color:var(--danger);" onclick="removeServer('${s.id}')">✕</button></td>`;
    tbody.appendChild(tr);
  });
}
async function loadModelStats() {
  const tbody = document.getElementById('modelstats-tbody');
  if (!tbody) return;
  try {
    const r = await api('/api/llm/modelstats');
    const rows = (r && r.rows) || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);">No stats yet — models earn them by working.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.sort((a, b) => (b.oneshots - a.oneshots) || a.server_id.localeCompare(b.server_id)).map(x =>
      `<tr><td>${esc(x.skill)}</td><td>${esc(x.label || x.server_id)}</td><td>${esc(x.tier)}</td>` +
      `<td>${x.attempts}</td><td>${x.oneshots}</td><td>${x.wins}</td>` +
      `<td>${x.oneshot_rate == null ? '—' : (x.oneshot_rate * 100).toFixed(1) + '%'}</td>` +
      `<td>${x.win_rate == null ? '—' : (x.win_rate * 100).toFixed(1) + '%'}</td>` +
      `<td>$${x.cost_usd.toFixed(4)}</td></tr>`).join('');
  } catch (e) { console.warn('modelstats failed', e); }
}
function startRenameServer(id) {
  // Inline edit in the table — no browser prompt() dialog.
  const row = document.getElementById(`server-row-${id}`);
  const cell = row && row.querySelector('.llm-label-cell');
  if (!cell) return;
  const cur = cell.dataset.label || '';
  cell.innerHTML = `<input type="text" class="inline-rename" value="${escapeHtml(cur)}" placeholder="label or empty for address">`;
  const input = cell.querySelector('input');
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const label = save ? input.value.trim() : cur;
    try {
      await api('/api/servers/label', { method: 'POST', body: JSON.stringify({ id, label }) });
      toast(save ? 'Server renamed.' : 'Rename cancelled.');
      loadActive();
    } catch (e) { systemAlert('Rename failed: ' + e.message); }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}
async function toggleServerActive(id, on) {
  try {
    // Active ids live in config.json regardless of engine state — works
    // before any project launches (first run).
    const c = await api('/api/config');
    const ids = ((c.llm || {}).active_ids || []).filter(x => x !== id);
    if (on) ids.push(id);
    await api('/api/servers/active', { method: 'POST', body: JSON.stringify({ ids }) });
    loadActive();
  } catch (e) { systemAlert(e.message); }
}
function openAddServerModal() { document.getElementById('add-server-modal').style.display = 'flex'; }
async function addServer() {
  const tier = document.getElementById('as-tier').value;
  const spec = {
    id: document.getElementById('as-id').value.trim(),
    label: document.getElementById('as-label').value.trim(),
    type: document.getElementById('as-type').value,
    url: document.getElementById('as-url').value.trim(),
    model: document.getElementById('as-model').value.trim(),
    api_key: document.getElementById('as-apikey').value.trim(),
    max_concurrent: parseInt(document.getElementById('as-conc').value || '1'),
    timeout: parseFloat(document.getElementById('as-timeout').value || '1200'),
    tier: ['tiny', 'small', 'large'].includes(tier) ? tier : 'small',
    priority: parseInt(document.getElementById('as-priority').value || '1'),
    context_window: parseInt(document.getElementById('as-context').value || '0'),
    params: {},
  };
  try { spec.params = JSON.parse(document.getElementById('as-params').value || '{}'); } catch (e) {}
  if (spec.type === 'openai') { spec.base_url = spec.url; spec.url = ''; }
  if (!spec.id) return systemAlert('ID required.');
  try {
    await api('/api/servers/add', { method: 'POST', body: JSON.stringify(spec) });
    closeModal('add-server-modal'); loadActive();
  } catch (e) { systemAlert('Add failed: ' + e.message); }
}
async function removeServer(id) {
  try { await api(`/api/servers/remove/${id}`, { method: 'POST' }); loadActive(); }
  catch (e) { systemAlert('Remove failed: ' + e.message); }
}
async function healthCheck(id) {
  try {
    const r = await api(`/api/servers/health/${id}`, { method: 'POST' });
    systemAlert(r.ok ? `health OK: ${r.reply}` : `health FAIL: ${r.error}`);
  } catch (e) { systemAlert('Health check failed: ' + e.message); }
}

// ------------------------------------------------------------------ //
// projects
// ------------------------------------------------------------------ //
async function loadProjects() {
  try {
    const [r, active] = await Promise.all([
      api('/api/projects'),
      api('/api/active').catch(() => ({})),
    ]);
    activeProjectId = r.active_id;
    renderProjects(r.projects, active);
  } catch (e) { console.error(e); }
}
function renderProjects(projects, active = {}) {
  const engines = {};
  (active.engines || []).forEach(eng => { if (eng && eng.project_id != null) engines[eng.project_id] = eng; });
  const c = document.getElementById('projects-list');
  c.innerHTML = '';
  if (!projects.length) { c.innerHTML = '<div class="empty-state"><span>No projects yet — create one.</span></div>'; return; }
  projects.forEach(p => {
    const activeProj = p.id === activeProjectId;
    const mkeys = Object.keys(p.metrics || {}).join(', ');
    const eng = engines[p.id];
    let engineChip = '';
    let stopBtn = '';
    if (eng) {
      const gen = eng.generation != null ? eng.generation : '?';
      const running = eng.engine_state === 'running';
      const paused = eng.engine_state === 'paused' || eng.paused === true;
      if (running) {
        engineChip = `<span class="engine-chip running">● running gen=${escapeHtml(String(gen))}</span>`;
      } else if (paused) {
        engineChip = `<span class="engine-chip paused">⏸ paused gen=${escapeHtml(String(gen))}</span>`;
      } else {
        engineChip = `<span class="engine-chip stopped">■ stopped</span>`;
      }
      if (running || paused) {
        stopBtn = `<button class="btn" style="border-color: var(--danger); color: var(--danger);" onclick="stopEngine('${p.id}')">Stop engine</button>`;
      }
    }
    const el = document.createElement('div');
    el.className = `project-card ${activeProj ? 'active' : ''}`;
    el.innerHTML = `
      <div class="project-name">${escapeHtml(p.name)}${activeProj ? '<span class="chip chip-accent">ACTIVE</span>' : ''}${engineChip}</div>
      <div class="project-desc">${escapeHtml(p.description || '')}</div>
      <div class="project-meta">metrics: ${escapeHtml(mkeys || 'none')}</div>
      <div class="action-row" style="margin-top:12px;">
        <button class="btn btn-primary" onclick="switchProject('${p.id}')">Run</button>
        <button class="btn" onclick="editProjectSpec('${p.id}')">Edit Spec</button>
        ${stopBtn}
        <button class="btn" style="border-color: var(--danger); color: var(--danger); margin-left:auto;" data-id="${p.id}" data-name="${escapeHtml(p.name)}" onclick="openDeleteProjectModal(this)">Delete</button>
      </div>`;
    c.appendChild(el);
  });
}

async function switchProject(id) {
  try {
    const r = await api('/api/engine/switch', { method: 'POST', body: JSON.stringify({ project_id: id }) });
    if (r.ok) {
      activeProjectId = id;
      activeSpec = null;
      settingsProjectId = null;
      setProjectTabLabel();
      knownIterationIds.clear();
      iterationsData = [];
      switchView('dashboard');
    } else systemAlert('Switch failed: ' + r.error);
  } catch (e) { systemAlert('Switch failed: ' + e.message); }
}
async function stopEngine(id) {
  try {
    await api('/api/engine/stop', { method: 'POST', body: JSON.stringify({ project_id: id }) });
    toast('Engine stopped');
    loadProjects();
  } catch (e) {
    console.error('Stop engine failed', e);
    systemAlert('Stop engine failed: ' + e.message);
  }
}

// ---- project deletion (type-the-name confirmation) ----
let deleteProjectId = null;
function openDeleteProjectModal(el) {
  deleteProjectId = el.dataset.id;
  document.getElementById('dp-target-name').textContent = `Delete "${el.dataset.name}"?`;
  const input = document.getElementById('dp-confirm-input');
  input.value = '';
  input.dataset.expected = el.dataset.name;
  document.getElementById('dp-confirm-btn').disabled = true;
  document.getElementById('delete-project-modal').style.display = 'flex';
  input.focus();
}
function dpInputChanged(el) {
  document.getElementById('dp-confirm-btn').disabled = el.value.trim() !== el.dataset.expected;
}
async function confirmDeleteProject() {
  if (!deleteProjectId) return;
  try {
    await api(`/api/projects/${deleteProjectId}`, { method: 'DELETE' });
    closeModal('delete-project-modal');
    toast('Project deleted.');
    loadProjects();
  } catch (e) {
    closeModal('delete-project-modal');
    systemAlert('Delete failed: ' + e.message);
  }
}
function openNewProjectModal() {
  document.getElementById('new-project-modal').style.display = 'flex';
}

function openCreateProjectModal() {
  document.getElementById('create-project-modal').style.display = 'flex';
  if (!npCards.length) {
    npCards = [{ key: '', label: '', unit: 'ms', direction: 'lower', weight: 1, live: true }];
  }
  renderCardDesigner('np');
}
function openSuggestProjectModal() {
  document.getElementById('suggest-project-modal').style.display = 'flex';
  document.getElementById('sp-goal').value = '';
  document.getElementById('sp-code').value = '';
  document.getElementById('sp-status').style.display = 'none';
  document.getElementById('sp-view-live').style.display = 'none';
  spProgramFile = null;
  spDataFile = null;
  ['sp-program-chip', 'sp-data-chip'].forEach(id => {
    const el = document.getElementById(id);
    el.style.display = 'none';
  });
  ['sp-file-program', 'sp-file-data'].forEach(id => { document.getElementById(id).value = ''; });
  loadSuggestServers();
}
async function loadSuggestServers() {
  const sel = document.getElementById('sp-server');
  sel.innerHTML = '';
  try {
    const c = await api('/api/config');
    const servers = (c.llm && c.llm.servers) || [];
    const activeIds = new Set(c.llm && c.llm.active_ids || []);
    const enabled = servers.filter(s => s.enabled);
    if (!enabled.length) {
      const opt = document.createElement('option');
      opt.textContent = 'No enabled AI servers — add one in Settings';
      opt.value = '';
      sel.appendChild(opt);
      return;
    }
    enabled.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.label || s.id}${s.model ? ` — ${s.model}` : ''}`;
      if (activeIds.has(s.id)) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!sel.selectedIndex && sel.options.length) sel.options[0].selected = true;
  } catch (e) { console.error('loadSuggestServers', e); }
}
function attachFile(inputId, storeVar, chipId) {
  const input = document.getElementById(inputId);
  const file = input.files && input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    if (storeVar === 'spProgramFile') {
      const content = String(reader.result || '');
      spProgramFile = { name: file.name, content };
      document.getElementById('sp-code').value = content;
      const chip = document.getElementById(chipId);
      chip.textContent = `✓ ${file.name} (${content.split('\n').length} lines, ${content.length} chars)`;
      chip.style.display = 'inline-flex';
    } else {
      // Data files may be ANY format — carry them as base64, never as text.
      const b64 = String(reader.result || '').split(',')[1] || '';
      spDataFile = { name: file.name, content_b64: b64, size: file.size };
      const chip = document.getElementById(chipId);
      chip.textContent = `✓ ${file.name} (${formatBytes(file.size)})`;
      chip.style.display = 'inline-flex';
    }
  };
  if (storeVar === 'spProgramFile') reader.readAsText(file);
  else reader.readAsDataURL(file);
}
async function createProject() {
  const cardSpec = cardsToSpec(collectDesignerCards('np'));
  const spec = {
    name: document.getElementById('np-name').value.trim(),
    description: document.getElementById('np-desc').value.trim(),
    language: document.getElementById('np-lang').value.trim() || 'c',
    artifact_name: document.getElementById('np-artifact').value.trim() || 'program',
    data: { baseline_source: document.getElementById('np-baseline').value.trim() || null },
    prompts: { goal: document.getElementById('np-goal').value.trim() },
    metrics: cardSpec.metrics,
    telemetry: cardSpec.telemetry,
  };
  const id = document.getElementById('np-id').value.trim().toLowerCase();
  if (!id) return systemAlert('ID required.');
  if (!Object.keys(cardSpec.metrics).length) return systemAlert('Add at least one metric card.');
  try {
    const r = await api('/api/projects', { method: 'POST', body: JSON.stringify({ id, spec }) });
    if (r.ok) {
      closeModal('create-project-modal');
      loadProjects();
      obAfterProjectCreated(id, spec.name || id);
      // Manual setup promises pipeline steps + metric cards "field by
      // field" — continue straight into the spec editor (outside the
      // onboarding wizard, which drives its own next step).
      if (!obOpen) editProjectSpec(id);
    }
    else systemAlert('Create failed: ' + r.error);
  } catch (e) { systemAlert('Create failed: ' + e.message); }
}

// ------------------------------------------------------------------ //
// metric cards designer — the creation step that makes each project's
// dashboard cards custom. One card per metric: key, label, unit,
// direction, weight + "live" (shown live in the worker telemetry cards).
// ------------------------------------------------------------------ //
let npCards = [];
let srCards = [];

function designerState(prefix) { return prefix === 'np' ? npCards : srCards; }

function designerCardHtml(prefix, i, c) {
  return `
  <div class="metric-editor" data-i="${i}">
    <div class="step-title">Card ${i + 1} <button class="btn btn-sm" style="border-color:var(--danger);color:var(--danger);" onclick="removeDesignerCard('${prefix}', ${i})">✕</button></div>
    <div class="form-row"><label>metric key (printed by score harness)</label><input data-c="key" value="${escapeHtml(c.key || '')}" placeholder="time_ms"></div>
    <div class="form-row"><label>card label</label><input data-c="label" value="${escapeHtml(c.label || '')}" placeholder="Time per round"></div>
    <div class="form-row"><label>unit</label><input data-c="unit" value="${escapeHtml(c.unit || '')}" placeholder="ms / bytes / %"></div>
    <div class="form-row"><label>direction</label><select data-c="direction">
      <option value="lower" ${c.direction === 'lower' ? 'selected' : ''}>lower-better ▼</option>
      <option value="higher" ${c.direction === 'higher' ? 'selected' : ''}>higher-better ▲</option></select></div>
    <div class="form-row"><label>weight</label><input data-c="weight" type="number" step="0.1" value="${c.weight ?? 1}"></div>
    <div class="form-row"><label>live card in worker telemetry</label><input data-c="live" type="checkbox" ${c.live !== false ? 'checked' : ''}></div>
  </div>`;
}

function renderCardDesigner(prefix) {
  const cards = designerState(prefix);
  const c = document.getElementById(prefix + '-cards');
  if (!c) return;
  c.innerHTML = cards.length
    ? cards.map((card, i) => designerCardHtml(prefix, i, card)).join('')
    : '<div class="empty-state"><span>No metric cards yet — add one (each card = one scored number of the project).</span></div>';
}

function addDesignerCard(prefix) {
  const state = designerState(prefix);
  // Persist in-progress DOM edits before re-rendering.
  const collected = collectDesignerCards(prefix);
  if (collected.length) state.splice(0, state.length, ...collected);
  state.push({ key: '', label: '', unit: '', direction: 'lower', weight: 1, live: true });
  renderCardDesigner(prefix);
}

function removeDesignerCard(prefix, i) {
  const state = designerState(prefix);
  const collected = collectDesignerCards(prefix);
  if (collected.length) state.splice(0, state.length, ...collected);
  state.splice(i, 1);
  renderCardDesigner(prefix);
}

function collectDesignerCards(prefix) {
  const c = document.getElementById(prefix + '-cards');
  const cards = [];
  c.querySelectorAll('.metric-editor').forEach(el => {
    const card = {};
    el.querySelectorAll('[data-c]').forEach(inp => {
      const k = inp.dataset.c;
      if (k === 'weight') card.weight = parseFloat(inp.value || '1');
      else if (k === 'live') card.live = inp.checked;
      else card[k] = inp.value.trim();
    });
    cards.push(card);
  });
  return cards;
}

function cardsToSpec(cards) {
  const metrics = {};
  const live = [];
  for (const c of cards) {
    if (!c.key) continue;
    metrics[c.key] = { label: c.label || c.key, unit: c.unit || '', direction: c.direction || 'lower', weight: c.weight != null ? c.weight : 1 };
    if (c.live !== false) live.push(c.key);
  }
  return {
    metrics,
    telemetry: { enabled: true, progress_token: 'KAISEN_PROGRESS', live_fields: live },
  };
}
let suggestPollTimer = null;
let suggestStartTime = 0;
function showSuggestProgress() {
  document.getElementById('suggest-progress-modal').style.display = 'flex';
}
function closeSuggestProgress() {
  document.getElementById('suggest-progress-modal').style.display = 'none';
}
let suggestStepsCache = [];
async function pollSuggestStatus() {
  let st = null;
  try { st = await api('/api/suggest/status'); } catch (e) { return; }
  const stage = document.getElementById('sgp-stage');
  const meta = document.getElementById('sgp-meta');
  const body = document.getElementById('suggest-progress-body');
  const elapsed = st.elapsed != null ? `${st.elapsed}s` : '';
  const liveTokens = st.tokens ? ` · ${st.tokens.length} tok streaming` : '';
  stage.textContent = (st.raw_label || (st.stage || 'working')) + ' · ' + elapsed + liveTokens;
  suggestStepsCache = st.steps || [];
  const stepsEl = document.getElementById('suggest-steps');
  if (stepsEl) {
    stepsEl.innerHTML = suggestStepsCache.map((s, i) => {
      const ss = s.state || 'pending';
      const cls = ss === 'running' ? 'sstep running' : ss === 'done' ? 'sstep done' : ss === 'failed' ? 'sstep failed' : 'sstep';
      return `<div class="${cls}" data-i="${i}" role="button" tabindex="0" onclick="showSuggestStep(${i})">
        <span class="sstep-dot"></span>
        <span class="sstep-label">${escapeHtml(s.label || s.id)}</span>
        ${s.attempt ? `<span class="sstep-attempt">try ${s.attempt}</span>` : ''}
      </div>`;
    }).join('') || '<div class="sstep"><span class="sstep-dot"></span><span class="sstep-label muted">preparing…</span></div>';
  }
  body.scrollTop = body.scrollHeight;
  const servers = (st.server_ids || []).join(', ') || 'auto';
  meta.textContent = st.running ? `round ${st.round || 1}/${st.max_rounds || 8} · ${servers} · ${elapsed}` : `${st.stage} · ${elapsed}`;
  const statusEl = document.getElementById('sp-status');
  if (st.running) {
    statusEl.style.display = 'block';
    statusEl.textContent = `🤖 AI working — ${st.stage ? st.stage.replace(/_/g, ' ') : 'step by step'} · ${elapsed}`;
    document.getElementById('sp-view-live').style.display = 'inline-flex';
  }
}
function showSuggestStep(i) {
  const s = suggestStepsCache[i];
  if (!s) return;
  const raw = document.getElementById('sgp-raw');
  raw.style.display = 'block';
  raw.textContent = s.output || s.error || '(no output yet)';
  raw.scrollTop = raw.scrollHeight;
}
function spGoalLanguage() {
  // Auto-fill the language select from the goal's words (only when the
  // user hasn't picked one explicitly).
  const sel = document.getElementById('sp-lang');
  const goal = (document.getElementById('sp-goal').value || '').toLowerCase();
  if (!sel || sel.value || !goal) return;
  const phrases = [
    ['cuda', 'cuda'], ['c++', 'cpp'], ['c++17', 'cpp'], ['c++20', 'cpp'],
    ['typescript', 'typescript'], ['javascript', 'javascript'], ['java', 'java'],
    ['python', 'python'], ['golang', 'go'], [' go ', 'go'], ['rust', 'rust'],
    ['c#', 'csharp'], ['csharp', 'csharp'], ['kotlin', 'kotlin'], ['swift', 'swift'],
    ['php', 'php'], ['ruby', 'ruby'], ['zig', 'zig'], ['scala', 'scala'],
    ['dart', 'dart'], ['haskell', 'haskell'], ['lua', 'lua'], ['perl', 'perl'],
    ['shell script', 'shell'], ['bash', 'shell'], [' in c ', 'c'], ['c program', 'c'],
  ];
  for (const [phrase, lang] of phrases) {
    if (goal.includes(phrase)) {
      sel.value = lang;
      return;
    }
  }
}


async function suggestProject() {
  const goal = document.getElementById('sp-goal').value.trim();
  const code = document.getElementById('sp-code').value.trim();
  if (!goal) return systemAlert('Describe the goal in words — no program needed, the AI writes it from scratch.');
  const sel = document.getElementById('sp-server');
  const serverId = sel && sel.value;
  if (!serverId) return systemAlert('No AI server selected — add one in Settings.');
  const btn = document.querySelector('#suggest-project-modal .btn-primary');
  btn.disabled = true;
  btn.textContent = 'Suggesting…';
  showSuggestProgress();
  suggestStartTime = Date.now();
  suggestPollTimer = setInterval(pollSuggestStatus, 600);
  try {
    const body = { goal, code, server_ids: [serverId] };
    if (spDataFile) body.data_file = spDataFile;
    const langEl = document.getElementById('sp-lang');
    if (langEl && langEl.value) body.language = langEl.value;
    if (spProgramFile) body.code_file = spProgramFile.name;
    const r = await api('/api/projects/suggest', { method: 'POST', body: JSON.stringify(body) });
    clearInterval(suggestPollTimer);
    suggestPollTimer = null;
    await pollSuggestStatus();
    document.getElementById('sp-status').style.display = 'none';
    document.getElementById('sp-view-live').style.display = 'none';
    if (r.ok) {
      document.getElementById('sr-spec').value = JSON.stringify(r.suggested_spec, null, 2);
      document.getElementById('sr-id').value = (goal.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40)) || 'project';
      seedDesignerFromSpec('sr', r.suggested_spec);
      const notes = r.validation || {};
      const notesEl = document.getElementById('sr-notes');
      notesEl.innerHTML = `<span class="sr-notes-ok">✓ validated in ${notes.rounds || 1} round${(notes.rounds || 1) > 1 ? 's' : ''} (guardrails + lint + smoke run)</span>`;
      if (notes.notes && notes.notes.length) {
        notesEl.innerHTML += `<div class="sr-notes-detail">${escapeHtml(notes.notes.join(' · '))}</div>`;
      }
      closeModal('suggest-project-modal');
      closeSuggestProgress();
      document.getElementById('suggest-result-modal').style.display = 'flex';
    } else systemAlert('Suggest failed: ' + (r.error || 'unknown error'));
  } catch (e) {
    clearInterval(suggestPollTimer);
    suggestPollTimer = null;
    await pollSuggestStatus();
    document.getElementById('sp-status').style.display = 'none';
    document.getElementById('sp-view-live').style.display = 'none';
    systemAlert('Suggest failed: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Suggest Pipeline';
  }
}
function seedDesignerFromSpec(prefix, spec) {
  const metrics = spec.metrics || {};
  const live = new Set(((spec.telemetry || {}).live_fields) || []);
  const cards = Object.entries(metrics).map(([key, m]) => ({
    key,
    label: m.label || '',
    unit: m.unit || '',
    direction: m.direction || 'lower',
    weight: m.weight != null ? m.weight : 1,
    live: !live.size || live.has(key),
  }));
  if (prefix === 'np') npCards = cards;
  else srCards = cards;
  renderCardDesigner(prefix);
}

async function createProjectFromSpec() {
  let spec;
  try { spec = JSON.parse(document.getElementById('sr-spec').value); }
  catch (e) { return systemAlert('Invalid JSON: ' + e.message); }
  // The metric cards designer edits the suggested metrics — overlay them.
  const cardSpec = cardsToSpec(collectDesignerCards('sr'));
  spec.metrics = cardSpec.metrics;
  spec.telemetry = cardSpec.telemetry;
  if (pipeSuggestProjectId) {
    // Apply mode: overwrite the existing project's pipeline with the
    // reviewed suggestion (harness files ride inside the spec).
    try {
      const r = await api(`/api/projects/${pipeSuggestProjectId}/spec`, { method: 'PUT', body: JSON.stringify({ spec }) });
      if (r.ok) {
        closeModal('suggest-result-modal');
        toast('Pipeline applied — the canvas now shows the AI-designed nodes.');
        pipeSuggestProjectId = null;
        document.getElementById('sr-create-btn').textContent = 'Create Project';
        document.getElementById('sr-id-row').style.display = '';
        activeSpec = r.spec;
        renderProjectConfig();
      } else systemAlert('Apply failed: ' + (r.error || 'unknown error'));
    } catch (e) { systemAlert('Apply failed: ' + e.message); }
    return;
  }
  const id = document.getElementById('sr-id').value.trim().toLowerCase();
  if (!id) return systemAlert('ID required.');
  if (!Object.keys(cardSpec.metrics).length) return systemAlert('Add at least one metric card.');
  try {
    const r = await api('/api/projects', { method: 'POST', body: JSON.stringify({ id, spec }) });
    if (r.ok) { closeModal('suggest-result-modal'); loadProjects(); obAfterProjectCreated(id, spec.name || id); }
    else systemAlert('Create failed: ' + r.error);
  } catch (e) { systemAlert('Create failed: ' + e.message); }
}
let settingsProjectId = null;  // project whose spec the settings tab shows (override)
function setProjectTabLabel() {
  const tab = document.querySelector('#view-config .tab[data-tab="project"]');
  if (tab) tab.textContent = settingsProjectId ? `Project · ${settingsProjectId}` : 'Active Project';
}
async function editProjectSpec(id) {
  try {
    const r = await api(`/api/projects/${id}/spec`);
    activeSpec = r.spec;
    settingsProjectId = id;
    setProjectTabVisible(true);
    setProjectTabLabel();
    openSettingsBar('project');
  } catch (e) { systemAlert('Load spec failed: ' + e.message); }
}


// ------------------------------------------------------------------ //
// onboarding — first-run guided setup (connect model → project → launch)
// ------------------------------------------------------------------ //
let obKind = null;          // 'local' | 'api'
let obServer = null;        // {id, label} after a successful connect
let obProject = null;       // {id, name} after project creation
let obOpen = false;
let obExistingServers = [];
let obTesting = false;

function showOnboarding() {
  obOpen = true;
  document.getElementById('onboarding-modal').style.display = 'flex';
  obGoStep(1);
  refreshObExisting();
}
function closeOnboarding() {
  obOpen = false;
  document.getElementById('onboarding-modal').style.display = 'none';
}
async function refreshObExisting() {
  const el = document.getElementById('ob-existing');
  try {
    const c = await api('/api/config');
    const enabled = ((c.llm || {}).servers || []).filter(s => s.enabled !== false);
    obExistingServers = enabled;
    if (!enabled.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
    el.style.display = 'flex';
    el.innerHTML = `
      <span class="ob-existing-label">✓ ${enabled.length} endpoint${enabled.length > 1 ? 's' : ''} already configured — you can skip this step</span>
      <button class="btn btn-sm btn-primary" onclick="obGoStep(2)">Use existing →</button>`;
  } catch (e) { el.style.display = 'none'; }
}
function obGoStep(n) {
  document.querySelectorAll('.ob-step-body').forEach(b => b.style.display = 'none');
  const body = document.querySelector(`.ob-step-body[data-body="${n}"]`);
  if (body) body.style.display = 'block';
  document.querySelectorAll('.ob-step').forEach(s => {
    const sn = parseInt(s.dataset.step, 10);
    s.classList.toggle('active', sn === n);
    s.classList.toggle('done', sn < n);
  });
  if (n === 3) renderObSummary();
}
function obPickKind(kind) {
  obKind = kind;
  document.querySelectorAll('.ob-kind').forEach(k => k.classList.remove('selected'));
  document.getElementById(kind === 'local' ? 'ob-kind-local' : 'ob-kind-api').classList.add('selected');
  const isLocal = kind === 'local';
  document.getElementById('ob-server-form').style.display = 'block';
  document.getElementById('ob-url-label').textContent = isLocal ? 'Endpoint URL (…/completion)' : 'Base URL';
  const urlEl = document.getElementById('ob-url');
  urlEl.value = isLocal ? 'http://127.0.0.1:8502/completion' : '';
  urlEl.placeholder = isLocal ? 'http://127.0.0.1:8502/completion' : 'https://api.openai.com/v1';
  document.getElementById('ob-model-row').style.display = isLocal ? 'none' : 'flex';
  document.getElementById('ob-key-row').style.display = isLocal ? 'none' : 'flex';
  document.getElementById('ob-test').style.display = 'none';
  document.getElementById('ob-test').innerHTML = '';
  document.getElementById('ob-label').value = '';
  document.getElementById('ob-model').value = '';
  document.getElementById('ob-key').value = '';
  const idEl = document.getElementById('ob-id');
  idEl.dataset.touched = '';
  obAutoId();
  document.getElementById('ob-label').focus();
}
function obAutoId() {
  const idEl = document.getElementById('ob-id');
  if (!idEl || idEl.dataset.touched) return;
  const label = (document.getElementById('ob-label').value || '').trim();
  const model = (document.getElementById('ob-model').value || '').trim();
  const base = label || model || (obKind === 'local' ? 'local-model' : 'api-model');
  let id = base.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'my-model';
  const taken = new Set(obExistingServers.map(s => s.id));
  const orig = id;
  let n = 2;
  while (taken.has(id)) id = orig + '-' + (n++);
  idEl.value = id;
}
async function obAddServer() {
  if (obTesting) return;
  const isLocal = obKind === 'local';
  const spec = {
    id: document.getElementById('ob-id').value.trim().toLowerCase(),
    label: document.getElementById('ob-label').value.trim(),
    type: isLocal ? 'llama' : 'openai',
    url: '',
    base_url: '',
    model: '',
    api_key: '',
    max_concurrent: parseInt(document.getElementById('ob-conc').value || '2'),
    timeout: 1200,
    params: { temperature: 0.6 },
  };
  if (!spec.id) return systemAlert('ID required.');
  if (isLocal) {
    spec.url = document.getElementById('ob-url').value.trim();
    if (!spec.url) return systemAlert('Endpoint URL required.');
  } else {
    spec.base_url = document.getElementById('ob-url').value.trim();
    spec.model = document.getElementById('ob-model').value.trim();
    spec.api_key = document.getElementById('ob-key').value.trim();
    if (!spec.base_url) return systemAlert('Base URL required.');
    if (!spec.model) return systemAlert('Model name required.');
  }
  const testEl = document.getElementById('ob-test');
  const btn = document.getElementById('ob-test-btn');
  obTesting = true;
  btn.disabled = true;
  btn.textContent = 'Testing…';
  testEl.style.display = 'block';
  testEl.className = 'ob-test working';
  testEl.innerHTML = '<span class="ob-spin"></span>Saving & probing the endpoint…';
  try {
    await api('/api/servers/add', { method: 'POST', body: JSON.stringify(spec) });
    const r = await api(`/api/servers/health/${encodeURIComponent(spec.id)}`, { method: 'POST' });
    if (r.ok) {
      obServer = { id: spec.id, label: spec.label || spec.id };
      testEl.className = 'ob-test ok';
      testEl.innerHTML = `✓ Connected — model replied: “${escapeHtml(r.reply || 'ok')}”`;
      toast('Model connected.');
      setTimeout(() => obGoStep(2), 900);
    } else {
      obServer = { id: spec.id, label: spec.label || spec.id };
      testEl.className = 'ob-test bad';
      testEl.innerHTML = `✕ Saved, but the endpoint didn't answer: ${escapeHtml(r.error || 'unknown error')}<div class="ob-test-actions"><button class="btn btn-sm" onclick="obRetest()">Retest</button><button class="btn btn-sm btn-primary" onclick="obGoStep(2)">Continue anyway</button></div>`;
    }
  } catch (e) {
    testEl.className = 'ob-test bad';
    testEl.innerHTML = `✕ ${escapeHtml(e.message)}<div class="ob-test-actions"><button class="btn btn-sm" onclick="obAddServer()">Retry</button></div>`;
  } finally {
    obTesting = false;
    btn.disabled = false;
    btn.textContent = 'Test & Add';
  }
}
async function obRetest() {
  if (!obServer || obTesting) return;
  obTesting = true;
  const testEl = document.getElementById('ob-test');
  testEl.className = 'ob-test working';
  testEl.innerHTML = '<span class="ob-spin"></span>Probing again…';
  try {
    const r = await api(`/api/servers/health/${encodeURIComponent(obServer.id)}`, { method: 'POST' });
    if (r.ok) {
      testEl.className = 'ob-test ok';
      testEl.innerHTML = `✓ Connected — model replied: “${escapeHtml(r.reply || 'ok')}”`;
      setTimeout(() => obGoStep(2), 700);
    } else {
      testEl.className = 'ob-test bad';
      testEl.innerHTML = `✕ Still unreachable: ${escapeHtml(r.error || 'unknown error')}<div class="ob-test-actions"><button class="btn btn-sm" onclick="obRetest()">Retest</button><button class="btn btn-sm btn-primary" onclick="obGoStep(2)">Continue anyway</button></div>`;
    }
  } catch (e) {
    testEl.className = 'ob-test bad';
    testEl.innerHTML = `✕ ${escapeHtml(e.message)}<div class="ob-test-actions"><button class="btn btn-sm" onclick="obRetest()">Retest</button></div>`;
  } finally {
    obTesting = false;
  }
}
async function obDemoProject(btn) {
  if (!btn) return;
  btn.disabled = true;
  btn.style.opacity = 0.6;
  try {
    const r = await api('/api/onboarding/demo', { method: 'POST' });
    if (r.ok) {
      obProject = r.project || { id: 'demo-prime', name: 'Prime Counter — demo' };
      toast('Demo project created.');
      obGoStep(3);
    } else systemAlert('Demo create failed: ' + (r.error || 'unknown error'));
  } catch (e) { systemAlert('Demo create failed: ' + e.message); }
  btn.disabled = false;
  btn.style.opacity = 1;
}
function obAfterProjectCreated(id, name) {
  if (!obOpen) return;
  obProject = { id, name };
  obGoStep(3);
}
function renderObSummary() {
  const rows = [];
  if (obServer) {
    rows.push(`<div class="ob-sum-row">✓ Model — ${escapeHtml(obServer.label)}${obServer.id !== obServer.label ? ` <span class="muted">(${escapeHtml(obServer.id)})</span>` : ''}</div>`);
  } else {
    rows.push('<div class="ob-sum-row muted">No model connected — add one anytime in Settings → LLM Servers.</div>');
  }
  if (obProject) {
    rows.push(`<div class="ob-sum-row">✓ Project — ${escapeHtml(obProject.name)}</div>`);
  } else {
    rows.push('<div class="ob-sum-row muted">No project — create one anytime from the Projects view.</div>');
  }
  document.getElementById('ob-summary').innerHTML = rows.join('');
  document.getElementById('ob-launch-btn').style.display = obProject ? 'inline-flex' : 'none';
}
async function obLaunch() {
  if (!obProject) return obFinish();
  try {
    await api('/api/engine/switch', { method: 'POST', body: JSON.stringify({ project_id: obProject.id }) });
    try { await api('/api/engine/pause', { method: 'POST', body: JSON.stringify({ paused: false }) }); } catch (e) {}
  } catch (e) {
    systemAlert('Launch failed: ' + e.message);
    return;
  }
  await obComplete();
  closeOnboarding();
  activeProjectId = obProject.id;
  activeSpec = null;
  switchView('dashboard');
  toast('Engine started — watch the live generations.');
}
async function obFinish() {
  await obComplete();
  closeOnboarding();
  switchView('projects');
  loadProjects();
}
async function obComplete() {
  try { await api('/api/onboarding/complete', { method: 'POST' }); }
  catch (e) { console.warn('onboarding complete failed', e); }
}
async function maybeShowOnboarding() {
  try {
    const [cfg, pl] = await Promise.all([api('/api/config'), api('/api/projects')]);
    if (cfg.onboarding && cfg.onboarding.done) return;
    if (pl.projects && pl.projects.length) return;
    showOnboarding();
  } catch (e) { /* first paint must never be blocked by setup checks */ }
}

// ------------------------------------------------------------------ //
// config
// ------------------------------------------------------------------ //
async function loadConfig() {
  try {
    const c = await api('/api/config');
    document.getElementById('cfg-host').value = c.server.host;
    document.getElementById('cfg-port').value = c.server.port;
    document.getElementById('cfg-wcount').value = c.workers.default_count;
    document.getElementById('cfg-wmax').value = c.workers.max_count;
    document.getElementById('cfg-wqueue').value = c.workers.queue_size;
    document.getElementById('cfg-llm-timeout').value = c.llm.read_timeout;
    document.getElementById('cfg-llm-connect').value = c.llm.connect_timeout;
    document.getElementById('cfg-llm-retries').value = c.llm.max_retries;
    document.getElementById('cfg-autofix').checked = !!(c.autofix && c.autofix.build_enabled);
    document.getElementById('cfg-tg-token').value = c.telegram.token || '';
    document.getElementById('cfg-tg-chat').value = c.telegram.chat_id || '';
    const s = c.safety;
    document.getElementById('cfg-safety').innerHTML = `
      <div class="safety-row ${s.global_off ? 'bad' : 'ok'}">
        ${s.global_off ? '⚠ GLOBAL SAFETY OFF — guardrails disabled (config.json + KAISEN_SAFETY_OFF=1).' : 'Guardrails ACTIVE.'}
        <span style="font-size:11px;opacity:0.8;">${s.hard_deny_rules} hard rules · launchers: ${(s.allowed_launchers || []).join(', ')}</span>
      </div>
      <div style="font-size:12px;color:var(--muted);">Global off cannot be toggled from the GUI: edit config.json and set KAISEN_SAFETY_OFF=1.</div>`;
  } catch (e) { console.error(e); }
}
async function loadProjectConfig() {
  if (activeSpec) { renderProjectConfig(); return; }
  try {
    let pid = settingsProjectId;
    if (!pid) {
      const snap = await api('/api/active');
      pid = snap.project_id;
    }
    const r = await api(`/api/projects/${pid}/spec`);
    activeSpec = r.spec;
    renderProjectConfig();
  } catch (e) {
    setProjectTabVisible(!!settingsProjectId);
    if (settingsTab === 'project' && !settingsProjectId) switchSettingsTab('general');
  }
}
// ---- auto-fix build errors (compiler suggestions) ----
async function autofixDefaultChanged(enabled) {
  try {
    await api('/api/autofix', { method: 'POST', body: JSON.stringify({ enabled }) });
    openAutofixApplyModal(enabled);
  } catch (e) { systemAlert('Autofix default failed: ' + e.message); }
}
async function openAutofixApplyModal(prefillDefault) {
  try {
    const d = await api('/api/autofix');
    const def = prefillDefault !== undefined ? prefillDefault : d.default;
    const list = document.getElementById('autofix-project-list');
    const pids = Object.keys(d.projects || {});
    if (!pids.length) {
      list.innerHTML = '<div style="color:var(--muted);">No projects yet.</div>';
    } else {
      list.innerHTML = pids.map(pid => {
        const cur = d.projects[pid];
        const checked = prefillDefault !== undefined ? def : (cur !== false);
        const custom = (typeof cur === 'string') ? cur : '';
        return `
          <div style="display:flex;align-items:center;gap:10px;border:1px solid var(--border2);border-radius:8px;padding:8px 10px;">
            <input type="checkbox" id="af-${pid}" ${checked ? 'checked' : ''} style="flex:0 0 auto;">
            <span style="font-family:monospace;font-size:12px;flex:0 0 140px;">${escapeHtml(pid)}</span>
            <input id="afc-${pid}" placeholder="custom fixer path (optional)" value="${escapeHtml(custom)}" style="flex:1;">
          </div>`;
      }).join('');
    }
    document.getElementById('autofix-modal').style.display = 'flex';
  } catch (e) { systemAlert('Autofix load failed: ' + e.message); }
}
async function applyAutofixProjects() {
  const payload = {};
  document.querySelectorAll('#autofix-project-list > div').forEach(row => {
    const cb = row.querySelector('input[type="checkbox"]');
    const custom = row.querySelector('input:not([type="checkbox"])');
    if (!cb || !cb.id || !custom) return;
    const pid = cb.id.replace('af-', '');
    payload[pid] = custom.value.trim() || cb.checked;
  });
  try {
    await api('/api/autofix/apply', { method: 'POST', body: JSON.stringify({ projects: payload }) });
    closeModal('autofix-modal');
    systemAlert('Autofix settings applied.');
  } catch (e) { systemAlert('Apply failed: ' + e.message); }
}

async function saveConfig() {  const body = {
    server: { host: document.getElementById('cfg-host').value, port: parseInt(document.getElementById('cfg-port').value || '8080') },
    workers: {
      default_count: parseInt(document.getElementById('cfg-wcount').value || '4'),
      max_count: parseInt(document.getElementById('cfg-wmax').value || '32'),
      queue_size: parseInt(document.getElementById('cfg-wqueue').value || '8'),
    },
    llm: {
      read_timeout: parseFloat(document.getElementById('cfg-llm-timeout').value || '1200'),
      connect_timeout: parseFloat(document.getElementById('cfg-llm-connect').value || '15'),
      max_retries: parseInt(document.getElementById('cfg-llm-retries').value || '3'),
    },
    telegram: {
      token: document.getElementById('cfg-tg-token').value,
      chat_id: document.getElementById('cfg-tg-chat').value,
      enabled: !!(document.getElementById('cfg-tg-token').value && document.getElementById('cfg-tg-chat').value),
    },
  };
  try {
    await api('/api/config', { method: 'PUT', body: JSON.stringify(body) });
    systemAlert('Config saved.');
  } catch (e) { systemAlert('Save failed: ' + e.message); }
}

// One Apply Changes button, two targets: on the General tab it saves the
// framework config; on the Active Project tab it saves the project spec.
async function applyConfig() {
  const projectTabActive = document.getElementById('cfg-project').style.display !== 'none';
  if (projectTabActive) await saveProjectConfig();
  else await saveConfig();
}
async function saveProjectConfig() {
  if (!activeSpec) return systemAlert('No active project — activate one first.');
  collectProjectConfig();
  try {
    const r = await api(`/api/projects/${activeSpec.id}/spec`, {
      method: 'PUT',
      body: JSON.stringify({ spec: activeSpec }),
    });
    if (r.ok) systemAlert('Project spec saved.');
    else systemAlert('Spec save failed: ' + (r.error || 'unknown error'));
  } catch (e) { systemAlert('Spec save failed: ' + e.message); }
}

// ---- project spec editor ----
function renderProjectConfig() {
  if (!activeSpec) return;
  const s = activeSpec;
  document.getElementById('pj-id').value = s.id;
  document.getElementById('pj-name').value = s.name || '';
  document.getElementById('pj-desc').value = s.description || '';
  document.getElementById('pj-lang').value = s.language || 'c';
  document.getElementById('pj-artifact').value = s.artifact_name || 'program';
  document.getElementById('pj-hysteresis').value = (s.select && s.select.hysteresis) || 1.0001;
  document.getElementById('pj-guardrails').checked = !(s.guardrails && s.guardrails.enabled === false);
  document.getElementById('pj-goal').value = (s.prompts && s.prompts.goal) || '';
  document.getElementById('pj-user-instructions').value = (s.prompts && s.prompts.user_instructions) || '';
  document.getElementById('pj-variant-mode').value = (s.prompts && s.prompts.variant_mode) || 'random';
  document.getElementById('pj-variant-n').value = (s.prompts && s.prompts.variant_n) || 1;
  document.getElementById('pj-pinned').value = ((s.prompts && s.prompts.pinned_blocks) || []).join(', ');
  document.getElementById('pj-baseline').value = (s.data && s.data.baseline_source) || '';
  document.getElementById('pj-deepwork').checked = !!(s.skills && s.skills.deepwork && s.skills.deepwork.enabled);
  document.getElementById('pj-lessons').checked = !!(s.skills && s.skills.lessons && s.skills.lessons.enabled);
  const af = s.skills && s.skills.autofix_build;
  document.getElementById('pj-autofix').checked = af !== false;
  document.getElementById('pj-autofix-custom').value = (typeof af === 'string') ? af : '';
  const eng = s.engine || {};
  document.getElementById('pj-workers').value = eng.workers || '';
  document.getElementById('pj-multi').value = eng.multi || '';
  renderPipeCanvas(s);
  renderMetricsEditor(s);
}

// ------------------------------------------------------------------ //
// pipeline canvas — node-editor-style connected nodes
// ------------------------------------------------------------------ //
const LANGS = [
  ['c', 'C'], ['cpp', 'C++'], ['cuda', 'CUDA'], ['python', 'Python'],
  ['java', 'Java'], ['javascript', 'JavaScript'], ['typescript', 'TypeScript'],
  ['csharp', 'C#'], ['go', 'Go'], ['rust', 'Rust'], ['kotlin', 'Kotlin'],
  ['swift', 'Swift'], ['php', 'PHP'], ['ruby', 'Ruby'], ['r', 'R'],
  ['zig', 'Zig'], ['scala', 'Scala'], ['dart', 'Dart'], ['haskell', 'Haskell'],
  ['lua', 'Lua'], ['perl', 'Perl'], ['shell', 'Shell'],
];
function fillLangSelect(sel, withAuto) {
  if (!sel) return;
  sel.innerHTML = '';
  if (withAuto) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = 'auto (from file name)';
    sel.appendChild(o);
  }
  LANGS.forEach(([v, l]) => {
    const o = document.createElement('option');
    o.value = v;
    o.textContent = l;
    sel.appendChild(o);
  });
}

let pipeNodes = [];
let pipeCollapsed = {};
const PIPE_BADGES = {
  build: ['⛏ BUILD', 'badge-build'],
  verify: ['✓ VERIFY', 'badge-verify'],
  score: ['⚡ SCORE', 'badge-score'],
};

function pipeFromSpec(s) {
  const nodes = [];
  const st = (s.steps || {});
  if (st.build && Object.keys(st.build).length) nodes.push({ stage: 'build', step: st.build });
  (st.verify || []).forEach(x => nodes.push({ stage: 'verify', step: x }));
  (st.score || []).forEach(x => nodes.push({ stage: 'score', step: x }));
  return nodes;
}
function pipeStepMode(step) {
  // The mode is what the USER chose, not what's typed: a freshly
  // created inline step has empty code and must still render the
  // script editor (mode-by-existence, not mode-by-content).
  if (step.inline) return step.inline.lang === 'shell' ? 'inline-shell' : 'inline-python';
  return 'file';
}
function pipeNodeHtml(node, i) {
  const collapsed = pipeCollapsed[i] ? ' collapsed' : '';
  const isBuild = node.stage === 'build';
  const [badge, badgeCls] = PIPE_BADGES[node.stage];
  const mode = pipeStepMode(node.step);
  const mk = Object.keys(activeSpec.metrics || {});
  const parseRows = node.stage === 'score' ? mk.map(key => {
    const rule = (node.step.parse || []).find(r => r.pattern && r.pattern.includes('?P<' + key + '>'));
    return `<div class="parse-row"><span class="metric-key">${escapeHtml(key)}</span><input data-f="parse" data-key="${escapeHtml(key)}" placeholder="e.g. time_ms=(?P&lt;time_ms&gt;[\\d.]+)" value="${escapeHtml(rule ? rule.pattern : '')}"></div>`;
  }).join('') : '';
  const move = isBuild ? '' : `<button class="btn btn-sm" title="Move up" onclick="pipeMove(${i}, -1)">↑</button><button class="btn btn-sm" title="Move down" onclick="pipeMove(${i}, 1)">↓</button>`;
  const rm = isBuild ? '' : `<button class="btn btn-sm" title="Remove node" style="border-color:var(--danger);color:var(--danger);" onclick="pipeRemove(${i})">✕</button>`;
  const modeLabel = mode.startsWith('inline') ? ('inline ' + (mode === 'inline-shell' ? 'shell' : 'python')) : (node.step.program || '—');
  return `
  <div class="pnode${collapsed}" data-i="${i}" data-stage="${node.stage}">
    <div class="pnode-head" role="button" tabindex="0" onclick="pipeToggle(${i})">
      <span class="pnode-badge ${badgeCls}">${badge}</span>
      <span class="pnode-title">${escapeHtml(modeLabel)}</span>
      <span class="pnode-actions" onclick="event.stopPropagation()">${move}${rm}</span>
      <span class="pnode-caret">▾</span>
    </div>
    <div class="pnode-body">
      <div class="pnode-mode">
        <button class="btn btn-sm ${mode === 'inline-python' ? 'pnode-mode-active' : ''}" onclick="pipeSetMode(${i}, 'inline-python')">python script</button>
        <button class="btn btn-sm ${mode === 'inline-shell' ? 'pnode-mode-active' : ''}" onclick="pipeSetMode(${i}, 'inline-shell')">shell script</button>
        <button class="btn btn-sm ${mode === 'file' ? 'pnode-mode-active' : ''}" onclick="pipeSetMode(${i}, 'file')">harness file</button>
      </div>
      <textarea class="pnode-code" data-f="code" spellcheck="false" placeholder="${mode === 'inline-shell' ? '#! bash script — argv: $1 = {candidate}, $2 = {artifact}' : '#! python3 script — argv: {candidate} {artifact} {project_dir} {workdir}'}" style="display:${mode.startsWith('inline') ? 'block' : 'none'};">${mode.startsWith('inline') ? escapeHtml(node.step.inline ? node.step.inline.code : '') : ''}</textarea>
      <div class="form-row" style="display:${mode === 'file' ? 'flex' : 'none'};"><label>program path</label><input data-f="program" placeholder="harness/${node.stage}.py" value="${escapeHtml(node.step.program || '')}"></div>
      <div class="form-row"><label>args (comma list)</label><input data-f="args" placeholder="{candidate}, {artifact}" value="${escapeHtml((node.step.args || []).join(', '))}"></div>
      <div class="form-row"><label>timeout (s)</label><input data-f="timeout" type="number" class="pnode-num" value="${node.step.timeout || 60}"></div>
      <div class="form-row"><label>memory MB (optional)</label><input data-f="memory_limit_mb" type="number" class="pnode-num" value="${node.step.memory_limit_mb || ''}"></div>
      ${node.stage === 'score' ? `<div class="form-row" style="align-items:flex-start;"><label>parse rules<br><span style="font-size:10px;color:var(--muted);text-transform:none;letter-spacing:0;">one regex per metric — named group = key</span></label><div style="flex:1;display:flex;flex-direction:column;gap:6px;">${parseRows || '<span class="muted" style="font-size:12px;">no metrics declared yet — add one in the Metrics panel</span>'}</div></div>` : ''}
    </div>
  </div>`;
}
function pipeStageIndex(i) {
  let k = 0;
  for (let j = 0; j < i; j++) if (pipeNodes[j].stage === pipeNodes[i].stage) k++;
  return k;
}
function pipeRemove(i) {
  const node = pipeNodes[i];
  if (!node || node.stage === 'build') return;
  activeSpec.steps[node.stage].splice(pipeStageIndex(i), 1);
  renderPipeCanvas(activeSpec);
}
function pipeMove(i, d) {
  const j = i + d;
  if (j < 0 || j >= pipeNodes.length) return;
  const a = pipeNodes[i], b = pipeNodes[j];
  if (a.stage === 'build' || b.stage === 'build' || a.stage !== b.stage) return;
  const arr = activeSpec.steps[a.stage];
  const ki = pipeStageIndex(i), kj = pipeStageIndex(j);
  const t = arr[ki];
  arr[ki] = arr[kj];
  arr[kj] = t;
  renderPipeCanvas(activeSpec);
}
function addPipeNode(stage) {
  pipeCollapsed = {};
  const step = stage === 'verify'
    ? { inline: { lang: 'python', code: '#!/usr/bin/env python3\nimport subprocess, sys\n\nartifact = sys.argv[1]\n# run the artifact, prove correctness, exit non-zero on failure\nprint("OK")' }, args: ['{artifact}'], timeout: 60 }
    : { inline: { lang: 'python', code: '#!/usr/bin/env python3\nimport subprocess, sys, time\n\nartifact = sys.argv[1]\n# measure the goal metric, print key=value lines + KAISEN_PROGRESS updates\nprint("time_ms=1.0")' }, args: ['{artifact}'], timeout: 60, parse: [] };
  activeSpec.steps = activeSpec.steps || { build: {}, verify: [], score: [] };
  activeSpec.steps[stage] = activeSpec.steps[stage] || [];
  activeSpec.steps[stage].push(step);
  renderPipeCanvas(activeSpec);
}
function pipeConnector() {
  return '<div class="pnode-connector"><div class="pline"></div><div class="parrow">▼</div></div>';
}
function renderPipeCanvas(s) {
  pipeNodes = pipeFromSpec(s);
  const c = document.getElementById('pj-canvas');
  if (!c) return;
  if (!pipeNodes.length) {
    c.innerHTML = '<div class="empty-state"><span>No pipeline nodes yet — click "✨ AI build my pipeline" or add nodes below.</span></div>';
    return;
  }
  c.innerHTML = pipeNodes.map((n, i) => pipeNodeHtml(n, i) + (i < pipeNodes.length - 1 ? pipeConnector() : '')).join('');
  c.querySelectorAll('[data-f]').forEach(el => {
    el.addEventListener('input', () => pipeCollectNode(el.closest('.pnode')));
  });
}
function pipeCollectNode(nodeEl) {
  if (!nodeEl) return;
  const i = parseInt(nodeEl.dataset.i, 10);
  const node = pipeNodes[i];
  if (!node) return;
  // Mutate the step IN PLACE: pipeFromSpec shares these objects with
  // activeSpec.steps, so replacing node.step here would detach the edits
  // — the next re-render rebuilds pipeNodes from activeSpec and silently
  // discards everything typed since the last Apply.
  const step = node.step;
  const mode = pipeStepMode(step);
  nodeEl.querySelectorAll('[data-f]').forEach(inp => {
    // Hidden inputs (the inactive mode's textarea / program path) still
    // hold values — collecting them pollutes the step with both `inline`
    // and `program` at once. Only visible fields belong to the step.
    if (!inp.offsetParent) return;
    const f = inp.dataset.f;
    if (f === 'code') {
      // In inline mode the code may be empty mid-edit — keep the step
      // inline so the editor stays visible (mode-by-existence above).
      if (mode.startsWith('inline')) {
        step.inline = { lang: mode === 'inline-shell' ? 'shell' : 'python', code: inp.value };
      } else if (inp.value.trim()) {
        step.inline = { lang: 'python', code: inp.value };
      }
    } else if (f === 'program') {
      if (inp.value.trim()) step.program = inp.value.trim();
    } else if (f === 'args') {
      step.args = inp.value.split(',').map(x => x.trim()).filter(Boolean);
    } else if (f === 'timeout') {
      step.timeout = parseFloat(inp.value || '60');
    } else if (f === 'memory_limit_mb') {
      if (inp.value) step.memory_limit_mb = parseFloat(inp.value);
      else delete step.memory_limit_mb;
    } else if (f === 'parse') {
      step.parse = step.parse || [];
      const key = inp.dataset.key;
      const pat = inp.value.trim();
      // Idempotent per key: every keystroke fires an input event, so
      // replace (not append) — otherwise each keystroke leaves a partial
      // regex rule behind and the spec fills with garbage patterns.
      step.parse = step.parse.filter(r => !(r.pattern && r.pattern.includes('?P<' + key + '>')));
      if (pat) step.parse.push({ type: 'regex', pattern: pat });
    }
  });
}
function pipeSetMode(i, m) {
  const el = document.querySelector('.pnode[data-i="' + i + '"]');
  if (el) pipeCollectNode(el);
  const node = pipeNodes[i];
  if (m === 'file') {
    delete node.step.inline;
  } else {
    node.step.inline = { lang: m === 'inline-shell' ? 'shell' : 'python', code: node.step.inline ? node.step.inline.code : '' };
    delete node.step.program;
  }
  renderPipeCanvas(activeSpec);
}
function pipeToggle(i) {
  const el = document.querySelector('.pnode[data-i="' + i + '"]');
  if (el) pipeCollectNode(el);
  pipeCollapsed[i] = !pipeCollapsed[i];
  renderPipeCanvas(activeSpec);
}


// ------------------------------------------------------------------ //
// pipeline test + AI-built pipeline (apply to this project)
// ------------------------------------------------------------------ //
let pipeSuggestProjectId = null;
async function smokeTestPipeline() {
  if (!activeSpec) return systemAlert('No active project.');
  collectProjectConfig();
  try {
    await api(`/api/projects/${activeSpec.id}/spec`, { method: 'PUT', body: JSON.stringify({ spec: activeSpec }) });
  } catch (e) { systemAlert('Save failed: ' + e.message); return; }
  document.getElementById('smoke-meta').textContent = 'running…';
  document.getElementById('smoke-content').innerHTML = '<div class="console-line" style="color:var(--warning);">Running build → verify → score on the baseline (temp copy)…</div>';
  document.getElementById('smoke-modal').style.display = 'flex';
  let r;
  try { r = await api(`/api/projects/${activeSpec.id}/smoke`, { method: 'POST' }); }
  catch (e) { r = { ok: false, error: e.message }; }
  const el = document.getElementById('smoke-content');
  if (r.ok) {
    document.getElementById('smoke-meta').textContent = '✓ PASSED';
    el.innerHTML = `<div class="console-line" style="color:var(--accent);">✓ pipeline passed — metrics: ${escapeHtml(JSON.stringify(r.metrics || {}))}</div>`;
  } else {
    document.getElementById('smoke-meta').textContent = '✕ FAILED at ' + (r.stage || '?');
    const parts = [];
    if (r.reason) parts.push(`<div class="console-line" style="color:var(--danger);">${escapeHtml(r.reason)}</div>`);
    if (r.stderr_tail) parts.push(`<div class="console-block">${escapeHtml(r.stderr_tail)}</div>`);
    if (r.stdout_tail) parts.push(`<div class="console-block">${escapeHtml(r.stdout_tail)}</div>`);
    el.innerHTML = parts.join('') || `<div class="console-line" style="color:var(--danger);">${escapeHtml(r.error || 'unknown error')}</div>`;
  }
}
function closeSmokeModal() { document.getElementById('smoke-modal').style.display = 'none'; }
async function aiSuggestPipeline() {
  if (!activeSpec) return systemAlert('No active project.');
  collectProjectConfig();
  try {
    await api(`/api/projects/${activeSpec.id}/spec`, { method: 'PUT', body: JSON.stringify({ spec: activeSpec }) });
  } catch (e) { systemAlert('Save failed: ' + e.message); return; }
  showSuggestProgress();
  suggestStartTime = Date.now();
  suggestPollTimer = setInterval(pollSuggestStatus, 600);
  let r;
  try {
    r = await api(`/api/projects/${activeSpec.id}/pipeline-suggest`, { method: 'POST' });
  } catch (e) { r = { ok: false, error: e.message }; }
  clearInterval(suggestPollTimer);
  suggestPollTimer = null;
  await pollSuggestStatus();
  closeSuggestProgress();
  if (r.ok) {
    pipeSuggestProjectId = activeSpec.id;
    document.getElementById('sr-spec').value = JSON.stringify(r.suggested_spec, null, 2);
    document.getElementById('sr-id-row').style.display = 'none';
    document.getElementById('sr-create-btn').textContent = '✓ Apply to my project';
    const notes = r.validation || {};
    document.getElementById('sr-notes').innerHTML = `<span class="sr-notes-ok">✓ validated in ${notes.rounds || 1} round${(notes.rounds || 1) > 1 ? 's' : ''} (guardrails + lint + smoke run) — review the JSON, then apply. The canvas updates to the new nodes.</span>`;
    seedDesignerFromSpec('sr', r.suggested_spec);
    document.getElementById('suggest-result-modal').style.display = 'flex';
    toast('AI pipeline ready — review the nodes.');
  } else {
    systemAlert('AI suggest failed: ' + (r.error || 'unknown error'));
  }
}

// ------------------------------------------------------------------ //
// swarm — parallel agents over the active servers
// ------------------------------------------------------------------ //
let swarmJobId = null;
let swarmPollTimer = null;
let swarmLastJob = null;

async function openSwarmModal() {
  document.getElementById('swarm-modal').style.display = 'flex';
  const req = document.getElementById('swarm-request');
  const spec = activeSpec;
  if (spec && spec.prompts && spec.prompts.goal) req.value = spec.prompts.goal;
  else if (spec && spec.description) req.value = spec.description;
  document.getElementById('swarm-live').style.display = 'none';
  document.getElementById('swarm-results').style.display = 'none';
  swarmJobId = null;
  swarmLastJob = null;
  try {
    const pl = await api('/api/projects');
    const sel = document.getElementById('swarm-project');
    sel.innerHTML = '';
    (pl.projects || []).forEach(p => {
      const o = document.createElement('option');
      o.value = p.id;
      o.textContent = p.name + ' (' + p.id + ')';
      if (p.id === activeProjectId) o.selected = true;
      sel.appendChild(o);
    });
    document.getElementById('swarm-project-row').style.display = (pl.projects || []).length > 1 ? 'flex' : 'none';
  } catch (e) {}
}
function closeSwarmModal() {
  document.getElementById('swarm-modal').style.display = 'none';
  if (swarmPollTimer) { clearInterval(swarmPollTimer); swarmPollTimer = null; }
}
async function startSwarm() {
  const kind = document.getElementById('swarm-kind').value;
  const body = {
    kind,
    request: document.getElementById('swarm-request').value.trim() || 'Improve the program',
    n: parseInt(document.getElementById('swarm-n').value || '3'),
    max_concurrent: parseInt(document.getElementById('swarm-conc').value || '3'),
  };
  if (kind !== 'answer') {
    body.project_id = activeProjectId || document.getElementById('swarm-project').value;
    if (!body.project_id) return systemAlert('Pick a project first (or start the engine on one).');
  }
  try {
    const r = await api('/api/swarm/start', { method: 'POST', body: JSON.stringify(body) });
    swarmJobId = r.job_id;
    document.getElementById('swarm-live').style.display = 'block';
    document.getElementById('swarm-results').style.display = 'block';
    clearInterval(swarmPollTimer);
    swarmPollTimer = setInterval(pollSwarm, 1200);
    pollSwarm();
  } catch (e) { systemAlert('Swarm start failed: ' + e.message); }
}
async function pollSwarm() {
  if (!swarmJobId) return;
  let j;
  try { j = (await api(`/api/swarm/${swarmJobId}`)).job; } catch (e) { return; }
  swarmLastJob = j;
  renderSwarm(j);
  if (['done', 'failed', 'cancelled'].includes(j.state)) {
    clearInterval(swarmPollTimer);
    swarmPollTimer = null;
  }
}
function renderSwarm(job) {
  document.getElementById('swarm-meta').textContent = `${job.kind} · ${job.state} · ${escapeHtml(job.request.slice(0, 60))}`;
  const log = document.getElementById('swarm-log');
  const lines = (job.events || []).filter(e => ['phase', 'error', 'results', 'finished'].includes(e.type)).slice(-7);
  log.innerHTML = lines.map(e => {
    const txt = e.type === 'phase' ? `${e.data.phase}: ${e.data.message || ''}`
      : e.type === 'error' ? `✕ ${e.data.message}`
      : e.type === 'results' ? `✓ ${e.data.count} result(s)`
      : `state: ${e.data.state}`;
    return `<div class="console-line">${escapeHtml(txt)}</div>`;
  }).join('') || '<div class="console-line muted">starting…</div>';
  const tasksEl = document.getElementById('swarm-tasks');
  tasksEl.innerHTML = (job.tasks || []).map((t, i) => {
    const st = t.state || 'waiting';
    const dot = st === 'streaming' ? 'sw-dot streaming' : st === 'done' ? 'sw-dot done' : st === 'failed' ? 'sw-dot failed' : 'sw-dot';
    const preview = escapeHtml((t.preview || t.task || t.error || '')).slice(0, 90);
    return `<div class="sw-task"><span class="${dot}"></span><span class="sw-task-state">${escapeHtml(st)}</span><span class="sw-task-text">${preview}</span></div>`;
  }).join('') || '<div class="console-line muted">no tasks yet</div>';
  const resEl = document.getElementById('swarm-results-body');
  const rs = job.results || [];
  if (job.kind === 'code_forge') {
    resEl.innerHTML = rs.length ? rs.map((d, i) => `
      <div class="sw-result ${d.ok ? 'ok' : 'bad'}">
        <div class="sw-result-head">#${d.rank || i + 1} ${d.ok ? '✓' : '✕'} ${escapeHtml(JSON.stringify(d.metrics || {}))}${d.reason ? ` <span class="muted">${escapeHtml(d.reason)}</span>` : ''}</div>
        ${d.ok ? `<details><summary>code</summary><pre class="sw-code">${escapeHtml(d.code || '')}</pre></details>
        <button class="btn btn-sm btn-primary" onclick="applySwarmResult(${i})">Use this</button>` : ''}
      </div>`).join('') : '<div class="console-line muted">waiting for results…</div>';
  } else if (job.kind === 'pipeline') {
    resEl.innerHTML = rs.length ? rs.map((d, i) => `
      <div class="sw-result ${d.ok ? 'ok' : 'bad'}">
        <div class="sw-result-head">#${i + 1} ${d.ok ? '✓ validated' : '✕ ' + escapeHtml(d.error || 'failed')} ${escapeHtml((d.notes || []).join(' · '))}</div>
        ${d.ok ? `<button class="btn btn-sm btn-primary" onclick="reviewSwarmSpec(${i})">Review &amp; apply</button>` : ''}
      </div>`).join('') : '<div class="console-line muted">waiting for results…</div>';
  } else {
    const ans = rs[0] && rs[0].answer;
    resEl.innerHTML = ans ? `<div class="console-block">${escapeHtml(ans)}</div>` : '<div class="console-line muted">waiting for the synthesizer…</div>';
  }
}
async function applySwarmResult(i) {
  if (!swarmLastJob) return;
  const d = swarmLastJob.results[i];
  if (!d) return;
  try {
    const r = await api('/api/queue/custom_code', { method: 'POST', body: JSON.stringify({ code: d.code, source: 'swarm' }) });
    if (r.ok) toast(`Swarm draft #${d.rank || i + 1} queued as generation ${r.generation}.`);
    else systemAlert('Queue failed: ' + (r.error || 'unknown'));
  } catch (e) { systemAlert('Queue failed: ' + e.message); }
}
async function reviewSwarmSpec(i) {
  if (!swarmLastJob) return;
  const d = swarmLastJob.results[i];
  if (!d || !d.spec) return;
  pipeSuggestProjectId = swarmLastJob.project_id || activeProjectId;
  document.getElementById('sr-spec').value = JSON.stringify(d.spec, null, 2);
  document.getElementById('sr-id-row').style.display = 'none';
  document.getElementById('sr-create-btn').textContent = '✓ Apply to my project';
  document.getElementById('sr-notes').innerHTML = '<span class="sr-notes-ok">✓ swarm design — validated (guardrails + lint + smoke run). Review the JSON, then apply.</span>';
  seedDesignerFromSpec('sr', d.spec);
  document.getElementById('suggest-result-modal').style.display = 'flex';
}



// ------------------------------------------------------------------ //
// live config — natural language reconfiguration (Ctrl+K palette)
// ------------------------------------------------------------------ //
const ACCENT_PRESETS = ['#00e87c', '#00bbf9', '#ff6b35', '#f15bb5', '#8ac926', '#6a4c93', '#ffca3a', '#e8e8e8'];
let lastConfigSnapshot = null;

function openPalette() {
  document.getElementById('palette-modal').style.display = 'flex';
  document.getElementById('palette-result').style.display = 'none';
  const inp = document.getElementById('palette-input');
  inp.value = '';
  inp.focus();
}
function closePalette() { document.getElementById('palette-modal').style.display = 'none'; }
async function submitPalette() {
  const inp = document.getElementById('palette-input');
  const request = inp.value.trim();
  if (!request) return;
  const res = document.getElementById('palette-result');
  res.style.display = 'block';
  res.innerHTML = '<div class="console-line" style="color:var(--warning);">Thinking…</div>';
  try {
    const r = await api('/api/config-agent', { method: 'POST', body: JSON.stringify({ request }) });
    if (r.ok) {
      lastConfigSnapshot = r.action;
      const reply = r.reply || 'done';
      res.innerHTML = `<div class="console-line" style="color:var(--accent);">${escapeHtml(reply)}</div>
        ${r.action === 'run_smoke' && r.result ? `<div class="console-line">${escapeHtml(JSON.stringify(r.result))}</div>` : ''}
        <div class="palette-actions">
          ${r.action !== 'answer' ? '<button class="btn btn-sm" onclick="undoLastChange()">↺ Undo</button>' : ''}
          <button class="btn btn-sm" onclick="closePalette()">Done</button>
        </div>`;
      if (r.css_vars) applyCssVars(r.css_vars);
      if (r.action === 'set_pref') { loadPrefs(); }
      if (r.action === 'edit_project' || r.action === 'add_metric') { activeSpec = null; if (currentView === 'config') openSettingsBar(settingsTab || 'general'); loadProjects(); }
    } else {
      res.innerHTML = `<div class="console-line" style="color:var(--danger);">✕ ${escapeHtml(r.error || 'failed')}</div><div class="palette-actions"><button class="btn btn-sm" onclick="closePalette()">Close</button></div>`;
    }
  } catch (e) {
    res.innerHTML = `<div class="console-line" style="color:var(--danger);">✕ ${escapeHtml(e.message)}</div>`;
  }
}
async function undoLastChange() {
  try {
    const list = await api('/api/snapshots' + (activeProjectId ? `?project_id=${activeProjectId}` : ''));
    const snaps = list.snapshots || [];
    if (!snaps.length) return systemAlert('No snapshot to revert to.');
    const r = await api('/api/snapshots/restore', { method: 'POST', body: JSON.stringify({ id: snaps[0].id, project_id: activeProjectId }) });
    if (r.ok) { toast('Reverted to the previous snapshot.'); loadPrefs(); activeSpec = null; loadProjects(); }
    else systemAlert('Revert failed: ' + (r.error || 'unknown'));
  } catch (e) { systemAlert('Revert failed: ' + e.message); }
}

// ------------------------------------------------------------------ //
// UI prefs — theme application
// ------------------------------------------------------------------ //
function applyCssVars(vars) {
  Object.entries(vars || {}).forEach(([k, v]) => document.documentElement.style.setProperty(k, v));
}
async function loadPrefs() {
  try {
    const r = await api('/api/prefs');
    applyCssVars(r.css_vars);
    const density = r.prefs && r.prefs.theme && r.prefs.theme.density;
    document.body.classList.remove('density-compact', 'density-spacious');
    if (density === 'compact') document.body.classList.add('density-compact');
    if (density === 'spacious') document.body.classList.add('density-spacious');
    const sel = document.getElementById('pref-density');
    if (sel) sel.value = density || 'comfortable';
    const presets = document.getElementById('accent-presets');
    if (presets) {
      const cur = (r.prefs.theme && r.prefs.theme.accent) || '#00e87c';
      presets.innerHTML = ACCENT_PRESETS.map(c => `<button class="accent-dot ${c === cur ? 'active' : ''}" style="background:${c};" title="${c}" onclick="setPref('theme.accent', '${c}')"></button>`).join('');
    }
  } catch (e) { console.warn('prefs load failed', e); }
}
async function setPref(path, value) {
  try {
    const r = await api('/api/prefs', { method: 'PUT', body: JSON.stringify({ path, value }) });
    if (r.ok) { applyCssVars(r.css_vars); toast('Preference applied (snapshot taken).'); loadPrefs(); }
    else systemAlert(r.error || 'Pref rejected.');
  } catch (e) { systemAlert('Pref failed: ' + e.message); }
}
async function resetPrefs() {
  try {
    const r = await api('/api/prefs/reset', { method: 'POST' });
    if (r.ok) { applyCssVars(r.css_vars); toast('Back to the KAISEN standard.'); loadPrefs(); }
  } catch (e) { systemAlert('Reset failed: ' + e.message); }
}

// ------------------------------------------------------------------ //
// snapshots UI
// ------------------------------------------------------------------ //
async function takeSnapshot() {
  try {
    const r = await api('/api/snapshots', { method: 'POST', body: JSON.stringify({ project_id: activeProjectId || undefined, reason: 'manual' }) });
    if (r.ok) { toast('Snapshot taken.'); loadSnapshots(); }
  } catch (e) { systemAlert('Snapshot failed: ' + e.message); }
}
async function loadSnapshots() {
  const el = document.getElementById('snapshots-list');
  if (!el) return;
  try {
    const r = await api('/api/snapshots' + (activeProjectId ? `?project_id=${activeProjectId}` : ''));
    const snaps = (r.snapshots || []).slice(0, 12);
    el.innerHTML = snaps.length ? snaps.map(s => `
      <div class="snap-row">
        <span class="snap-time">${escapeHtml(new Date(s.created * 1000).toLocaleString())}</span>
        <span class="snap-reason">${escapeHtml(s.reason || s.kind)}</span>
        <button class="btn btn-sm" onclick="restoreSnapshot('${s.id}')">Restore</button>
      </div>`).join('') : '<div class="muted" style="font-size:12px;">No snapshots yet — they are taken automatically before agent/config changes.</div>';
  } catch (e) { el.innerHTML = '<div class="muted" style="font-size:12px;">Snapshot list unavailable.</div>'; }
}
async function restoreSnapshot(id) {
  try {
    const r = await api('/api/snapshots/restore', { method: 'POST', body: JSON.stringify({ id, project_id: activeProjectId || undefined }) });
    if (r.ok) { toast('Snapshot restored.'); loadSnapshots(); activeSpec = null; loadProjects(); }
    else systemAlert('Restore failed: ' + (r.error || 'unknown'));
  } catch (e) { systemAlert('Restore failed: ' + e.message); }
}

// ------------------------------------------------------------------ //
// project agent console
// ------------------------------------------------------------------ //
let agentPollTimer = null;
let agentSeenTurns = 0;

function openAgentModal() {
  if (!activeSpec) return systemAlert('Open a project first (Settings → Active Project).');
  document.getElementById('agent-modal').style.display = 'flex';
  const m = document.getElementById('agent-mission');
  m.value = '';
  document.getElementById('agent-transcript').innerHTML = '<div class="console-line muted">The agent reads the project, runs the pipeline, and edits the spec with validation. Every change is snapshotted.</div>';
  agentSeenTurns = 0;
}
function closeAgentModal() {
  document.getElementById('agent-modal').style.display = 'none';
  clearInterval(agentPollTimer);
  agentPollTimer = null;
}
async function startAgent() {
  if (!activeSpec) return;
  const mission = document.getElementById('agent-mission').value.trim();
  try {
    const r = await api(`/api/projects/${activeSpec.id}/agent/start`, { method: 'POST', body: JSON.stringify({ mission }) });
    if (!r.ok) return systemAlert(r.error || 'Agent start failed.');
    agentSeenTurns = 0;
    clearInterval(agentPollTimer);
    agentPollTimer = setInterval(pollAgent, 1000);
    pollAgent();
  } catch (e) { systemAlert('Agent start failed: ' + e.message); }
}
async function pollAgent() {
  let st;
  try { st = await api('/api/agent/status'); } catch (e) { return; }
  const meta = document.getElementById('agent-meta');
  const el = document.getElementById('agent-transcript');
  meta.textContent = st.running ? `working… ${st.elapsed}s · ${(st.tokens || '').length} tok` : `done · ${st.elapsed}s`;
  const turns = st.turns || [];
  if (turns.length > agentSeenTurns) {
    const fresh = turns.slice(agentSeenTurns);
    const rows = fresh.map(t => {
      const argsS = JSON.stringify(t.args || {}).slice(0, 160);
      return `<div class="console-line"><span class="agent-tool">${escapeHtml(t.tool)}</span> ${escapeHtml(argsS)}</div>`;
    }).join('');
    el.innerHTML += rows;
    el.scrollTop = el.scrollHeight;
    agentSeenTurns = turns.length;
  }
  if (!st.running && st.summary) {
    el.innerHTML += `<div class="console-line" style="color:var(--accent);margin-top:8px;">✓ ${escapeHtml(st.summary)}</div>`;
    clearInterval(agentPollTimer);
    agentPollTimer = null;
    activeSpec = null;
    loadProjects();
    toast('Agent finished — spec reloaded.');
  }
}
async function cancelAgent() {
  try { await api('/api/agent/cancel', { method: 'POST' }); toast('Agent stopping…'); } catch (e) {}
}



function renderMetricsEditor(s) {
  const c = document.getElementById('pj-metrics');
  c.innerHTML = '';
  const tele = s.telemetry || {};
  const liveFields = tele.live_fields || [];
  for (const [key, spec] of Object.entries(s.metrics || {})) {
    const div = document.createElement('div');
    div.className = 'metric-editor';
    const isLive = !liveFields.length || liveFields.includes(key);
    div.innerHTML = `
      <div class="step-title">${escapeHtml(key)} <button class="btn btn-sm" style="border-color:var(--danger);color:var(--danger);" onclick="removeMetric('${key}')">✕</button></div>
      <div class="form-row"><label>label</label><input data-k="label" value="${escapeHtml(spec.label || key)}"></div>
      <div class="form-row"><label>unit</label><input data-k="unit" value="${escapeHtml(spec.unit || '')}"></div>
      <div class="form-row"><label>direction</label><select data-k="direction">
        <option value="higher" ${spec.direction === 'higher' ? 'selected' : ''}>higher-better</option>
        <option value="lower" ${spec.direction === 'lower' ? 'selected' : ''}>lower-better</option></select></div>
      <div class="form-row"><label>weight</label><input data-k="weight" type="number" step="0.1" value="${spec.weight != null ? spec.weight : 1}"></div>
      <div class="form-row"><label>live card in worker telemetry</label><input data-k="live" type="checkbox" ${isLive ? 'checked' : ''}></div>`;
    div.dataset.key = key;
    c.appendChild(div);
  }
}
function addMetricRow() {
  const s = activeSpec;
  s.metrics = s.metrics || {};
  let n = 1; while (s.metrics['metric' + n]) n++;
  s.metrics['metric' + n] = { label: 'Metric ' + n, unit: '', direction: 'higher', weight: 1.0 };
  renderMetricsEditor(s);
}
function removeMetric(key) {
  delete activeSpec.metrics[key];
  renderMetricsEditor(activeSpec);
}
function collectProjectConfig() {
  const s = activeSpec;
  s.name = document.getElementById('pj-name').value;
  s.description = document.getElementById('pj-desc').value;
  s.language = document.getElementById('pj-lang').value;
  s.artifact_name = document.getElementById('pj-artifact').value;
  s.select = s.select || {}; s.select.hysteresis = parseFloat(document.getElementById('pj-hysteresis').value || '1.0001');
  s.guardrails = s.guardrails || {}; s.guardrails.enabled = document.getElementById('pj-guardrails').checked;
  s.prompts = s.prompts || {};
  s.prompts.goal = document.getElementById('pj-goal').value;
  s.prompts.user_instructions = document.getElementById('pj-user-instructions').value;
  s.prompts.variant_mode = document.getElementById('pj-variant-mode').value;
  s.prompts.variant_n = parseInt(document.getElementById('pj-variant-n').value || '1');
  s.prompts.pinned_blocks = document.getElementById('pj-pinned').value.split(',').map(x => x.trim()).filter(Boolean);
  s.data = s.data || {}; s.data.baseline_source = document.getElementById('pj-baseline').value || null;
  s.skills = s.skills || {};
  s.skills.deepwork = s.skills.deepwork || {}; s.skills.deepwork.enabled = document.getElementById('pj-deepwork').checked;
  s.skills.lessons = s.skills.lessons || {}; s.skills.lessons.enabled = document.getElementById('pj-lessons').checked;
  const afCustom = document.getElementById('pj-autofix-custom').value.trim();
  s.skills.autofix_build = afCustom || document.getElementById('pj-autofix').checked;
  s.engine = s.engine || {};
  const wv = parseInt(document.getElementById('pj-workers').value || '');
  const mv = parseInt(document.getElementById('pj-multi').value || '');
  s.engine.workers = wv > 0 ? wv : null;
  s.engine.multi = mv > 0 ? mv : null;
  const steps = { build: {}, verify: [], score: [] };
  pipeNodes.forEach(n => {
    if (n.stage === 'build') steps.build = n.step;
    else steps[n.stage].push(n.step);
  });
  s.steps = steps;
  const metrics = {};
  const liveFields = [];
  document.querySelectorAll('#pj-metrics .metric-editor').forEach(el => {
    const key = el.dataset.key;
    const spec = { label: key, unit: '', direction: 'higher', weight: 1 };
    el.querySelectorAll('[data-k]').forEach(inp => {
      const k = inp.dataset.k;
      if (k === 'weight') spec.weight = parseFloat(inp.value || '1');
      else if (k === 'live') { if (inp.checked) liveFields.push(key); }
      else spec[k] = inp.value;
    });
    metrics[key] = spec;
  });
  s.metrics = metrics;
  s.telemetry = { enabled: true, progress_token: 'KAISEN_PROGRESS', live_fields: liveFields };
}

// ------------------------------------------------------------------ //
// notes — legacy operational notes: color filters, drag reorder,
// master-detail pane, inline editing, timestamped comments
// ------------------------------------------------------------------ //
let activeNoteId = null;
const activeColorFilters = new Set();
let colorFilterOrder = ['', ...NOTE_COLORS];
let dragSrcEl = null;
let colorDragSrcEl = null;

async function loadNotes() {
  try {
    const data = await api('/api/notes');
    notesData = data.notes || [];
    if (data.color_order && data.color_order.length > 0) {
      colorFilterOrder = data.color_order;
    } else {
      colorFilterOrder = ['', ...NOTE_COLORS];
    }
    renderColorFilters();
    filterNotes('');
  } catch (e) { console.error('Failed to load notes', e); }
}
function renderColorFilters() {
  const container = document.getElementById('color-filters-container');
  if (!container) return;
  container.innerHTML = '';
  const counts = {};
  colorFilterOrder.forEach(c => counts[c] = 0);
  notesData.forEach(note => {
    const matchesArchive = showArchivedNotes ? note.archived : !note.archived;
    if (matchesArchive) {
      const c = note.color || '';
      counts[c] = (counts[c] || 0) + 1;
    }
  });
  colorFilterOrder.forEach(color => {
    const chip = document.createElement('div');
    const count = counts[color] || 0;
    const hasCount = count > 0;
    chip.className = `color-filter-chip ${activeColorFilters.has(color) ? 'selected' : ''} ${color === '' ? 'no-color' : ''}`;
    chip.dataset.color = color;
    chip.draggable = true;
    chip.setAttribute('role', 'button');
    chip.tabIndex = 0;
    chip.setAttribute('aria-label', `Filter notes: ${color || 'no color'} (${count})`);
    chip.title = `Filter: ${color || 'no color'} — drag to reorder filters`;
    if (hasCount) {
      chip.style.width = 'auto';
      chip.style.height = 'auto';
      chip.style.padding = '6px 12px';
      chip.style.borderRadius = '999px';
      chip.style.display = 'inline-flex';
      chip.style.alignItems = 'center';
      chip.style.justifyContent = 'center';
      chip.style.gap = '0';
      chip.style.fontSize = '12px';
      chip.style.border = color === '' ? '1px solid var(--border2)' : 'none';
      chip.style.backgroundColor = color !== '' ? color : 'transparent';
      if (color !== '') chip.style.boxShadow = `0 2px 6px ${color}55`;
      const countSpan = document.createElement('span');
      countSpan.className = 'color-filter-count';
      countSpan.textContent = count;
      chip.appendChild(countSpan);
    } else {
      chip.style.width = '24px';
      chip.style.height = '24px';
      chip.style.padding = '0';
      chip.style.borderRadius = '50%';
      chip.style.justifyContent = 'center';

      if (color !== '') {
        chip.style.backgroundColor = color;
        chip.style.opacity = '0.4';
        chip.style.border = 'none';
      } else {
        chip.style.backgroundColor = 'transparent';
        chip.style.border = '1px dashed var(--border2)';
        chip.textContent = '';
      }
    }
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      if (activeColorFilters.has(color)) activeColorFilters.delete(color);
      else activeColorFilters.add(color);
      renderColorFilters();
      filterNotes(document.getElementById('notes-search').value);
    });
    chip.addEventListener('dragstart', (e) => {
      colorDragSrcEl = chip;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', color);
      setTimeout(() => chip.classList.add('dragging'), 0);
    });
    chip.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      if (chip !== colorDragSrcEl) chip.classList.add('drag-over');
    });
    chip.addEventListener('dragleave', () => chip.classList.remove('drag-over'));
    chip.addEventListener('drop', (e) => {
      e.stopPropagation();
      if (colorDragSrcEl && chip !== colorDragSrcEl) {
        const draggedColor = colorDragSrcEl.dataset.color;
        const targetColor = chip.dataset.color;
        const dragIdx = colorFilterOrder.indexOf(draggedColor);
        const targetIdx = colorFilterOrder.indexOf(targetColor);
        if (dragIdx > -1 && targetIdx > -1) {
          colorFilterOrder.splice(dragIdx, 1);
          colorFilterOrder.splice(targetIdx, 0, draggedColor);
          api('/api/notes/colors', { method: 'POST', body: JSON.stringify({ color_order: colorFilterOrder }) }).catch(console.error);
          renderColorFilters();
          filterNotes(document.getElementById('notes-search').value);
        }
      }
      chip.classList.remove('drag-over');
    });
    chip.addEventListener('dragend', () => {
      colorDragSrcEl = null;
      document.querySelectorAll('.color-filter-chip').forEach(c => {
        c.classList.remove('dragging', 'drag-over');
      });
    });
    container.appendChild(chip);
  });
}
function filterNotes(query) {
  const container = document.getElementById('notes-list');
  container.innerHTML = '';
  let filtered = notesData.filter(n => {
    const matchesSearch = (n.title || '').toLowerCase().includes(query.toLowerCase()) || (n.text || '').toLowerCase().includes(query.toLowerCase());
    const matchesArchive = showArchivedNotes ? n.archived : !n.archived;
    return matchesSearch && matchesArchive;
  });
  if (activeColorFilters.size > 0) {
    filtered = filtered.filter(n => activeColorFilters.has(n.color || ''));
  }
  const originalIndices = {};
  notesData.forEach((n, idx) => originalIndices[n.id] = idx);
  filtered.sort((a, b) => {
    const colorA = a.color || '';
    const colorB = b.color || '';
    const idxA = colorFilterOrder.indexOf(colorA);
    const idxB = colorFilterOrder.indexOf(colorB);
    if (idxA === -1 && idxB === -1) return originalIndices[a.id] - originalIndices[b.id];
    if (idxA === -1) return 1;
    if (idxB === -1) return -1;
    if (idxA !== idxB) return idxA - idxB;
    return originalIndices[a.id] - originalIndices[b.id];
  });
  if (filtered.length === 0) {
    container.innerHTML = `<div class="empty-state"><div class="pulse"></div><span>${showArchivedNotes ? 'No archived notes.' : 'No operational notes recorded.'}</span></div>`;
    return;
  }
  filtered.forEach(note => {
    const el = document.createElement('div');
    el.className = `note-item ${note.archived ? 'note-archived' : ''}`;
    el.dataset.id = note.id;
    el.draggable = true;
    el.setAttribute('role', 'button');
    el.tabIndex = 0;
    el.setAttribute('aria-label', `Open note: ${note.title || 'Untitled'}`);
    if (note.color) {
      el.style.backgroundColor = note.color + '33';
      el.style.borderColor = note.color + '66';
    }
    const commentCount = note.comments?.length || 0;
    el.innerHTML = `
      <div class="note-item-header">
        <span class="note-item-title">${escapeHtml(note.title || 'Untitled')}</span>
        <div style="display:flex; align-items:center; gap:8px;">
          ${commentCount > 0 ? `<span class="note-comment-bubble">💬 ${commentCount}</span>` : ''}
          <span class="note-item-time">${formatTimestamp(note.updated_at)}</span>
        </div>
      </div>
      <div class="note-item-preview">${escapeHtml(note.text || '')}</div>
    `;
    el.addEventListener('click', (e) => { if (dragSrcEl) return; openNoteDetail(note.id); });
    el.addEventListener('dragstart', handleDragStart);
    el.addEventListener('dragover', handleDragOver);
    el.addEventListener('dragenter', handleDragEnter);
    el.addEventListener('dragleave', handleDragLeave);
    el.addEventListener('drop', handleDrop);
    el.addEventListener('dragend', handleDragEnd);
    container.appendChild(el);
  });
}
function handleDragStart(e) {
  dragSrcEl = this;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', this.dataset.id);
  this.classList.add('dragging');
  setTimeout(() => this.style.opacity = '0.5', 0);
}
function handleDragOver(e) {
  if (e.preventDefault) e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  return false;
}
function handleDragEnter(e) { if (this !== dragSrcEl) { this.classList.add('drag-over'); } }
function handleDragLeave(e) { this.classList.remove('drag-over'); }
function handleDrop(e) {
  if (e.stopPropagation) e.stopPropagation();
  if (dragSrcEl !== this) {
    const draggedId = dragSrcEl.dataset.id;
    const targetId = this.dataset.id;
    const draggedIdx = notesData.findIndex(n => n.id === draggedId);
    const targetIdx = notesData.findIndex(n => n.id === targetId);
    if (draggedIdx > -1 && targetIdx > -1) {
      const [moved] = notesData.splice(draggedIdx, 1);
      notesData.splice(targetIdx, 0, moved);
      api('/api/notes/reorder', { method: 'POST', body: JSON.stringify({ order: notesData.map(n => n.id) }) }).catch(console.error);
      filterNotes(document.getElementById('notes-search').value);
    }
  }
  return false;
}
function handleDragEnd(e) {
  dragSrcEl = null;
  document.querySelectorAll('.note-item').forEach(item => { item.classList.remove('dragging', 'drag-over'); item.style.opacity = ''; });
}
function openNoteDetail(id) {
  activeNoteId = id;
  const note = notesData.find(n => n.id === id);
  if (!note) return;
  document.getElementById('notes-list').style.display = 'none';
  document.getElementById('note-detail').style.display = 'flex';
  document.querySelector('.fab').style.display = 'none';
  document.querySelector('#view-notes .main-header').style.display = 'none';
  document.getElementById('notes-search').style.display = 'none';
  document.getElementById('note-detail-title-display').textContent = note.title || 'Untitled';
  document.getElementById('note-detail-title-editor').value = note.title || '';
  document.getElementById('note-detail-display').textContent = note.text || '';
  document.getElementById('note-detail-editor').value = note.text || '';
  document.getElementById('note-detail-last-edited').textContent = `Last edited: ${formatTimestamp(note.updated_at)}`;
  renderColorPicker(note.color);
  renderComments(note.comments || []);
  document.getElementById('note-detail-display').style.display = 'block';
  document.getElementById('note-detail-editor').style.display = 'none';
  document.getElementById('note-detail-title-display').style.display = 'block';
  document.getElementById('note-detail-title-editor').style.display = 'none';
  document.getElementById('note-edit-btn').style.display = 'inline-flex';
  document.getElementById('note-save-btn').style.display = 'none';
  document.getElementById('note-cancel-edit-btn').style.display = 'none';
  document.getElementById('note-color-picker').style.display = 'flex';
  document.getElementById('note-archive-btn').textContent = note.archived ? 'Restore' : 'Archive';
}
function closeNoteDetail() {
  activeNoteId = null;
  document.getElementById('notes-list').style.display = 'flex';
  document.getElementById('note-detail').style.display = 'none';
  document.querySelector('.fab').style.display = 'flex';
  document.querySelector('#view-notes .main-header').style.display = '';
  document.getElementById('notes-search').style.display = '';
  filterNotes(document.getElementById('notes-search').value);
}
function renderColorPicker(activeColor) {
  const picker = document.getElementById('note-color-picker');
  picker.innerHTML = '';
  const defBtn = document.createElement('button');
  defBtn.className = `color-btn default ${!activeColor ? 'active' : ''}`;
  defBtn.setAttribute('aria-label', 'No color');
  defBtn.title = 'No color';
  defBtn.onclick = () => changeNoteColor('');
  picker.appendChild(defBtn);
  NOTE_COLORS.forEach(color => {
    const btn = document.createElement('button');
    btn.className = `color-btn ${color === activeColor ? 'active' : ''}`;
    btn.style.backgroundColor = color;
    btn.setAttribute('aria-label', `Color ${color}`);
    btn.title = color;
    btn.onclick = () => changeNoteColor(color);
    picker.appendChild(btn);
  });
}
async function changeNoteColor(color) {
  if (!activeNoteId) return;
  const note = notesData.find(n => n.id === activeNoteId);
  if (!note) return;
  note.color = color;
  renderColorPicker(color);
  try { await api(`/api/notes/${activeNoteId}`, { method: 'PUT', body: JSON.stringify({ color }) }); }
  catch (e) { console.error('Failed to update color', e); }
}
function renderComments(comments) {
  const container = document.getElementById('note-comments-list');
  container.innerHTML = '';
  if (!comments || comments.length === 0) {
    container.innerHTML = '<div class="note-comment-empty">No comments yet.</div>';
    return;
  }
  comments.forEach(comment => {
    const div = document.createElement('div');
    div.className = 'note-comment';
    div.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
        <span class="note-comment-time">${formatTimestamp(comment.timestamp)}</span>
        <button class="btn btn-sm btn-danger" style="padding:2px 6px; font-size:10px;" title="Delete Comment">✕</button>
      </div>
      <div class="note-comment-text">${escapeHtml(comment.text)}</div>
    `;
    const deleteBtn = div.querySelector('.btn-danger');
    deleteBtn.onclick = (e) => {
      e.stopPropagation();
      systemConfirm('Purge this comment record?', () => deleteComment(activeNoteId, comment.text, comment.timestamp));
    };
    container.appendChild(div);
  });
  container.scrollTop = container.scrollHeight;
}
async function addComment() {
  if (!activeNoteId) return;
  const input = document.getElementById('note-comment-input');
  const text = input.value.trim();
  if (!text) return;
  try {
    const updatedNote = await api(`/api/notes/${activeNoteId}/comment`, {
      method: 'POST',
      body: JSON.stringify({ text })
    });
    const note = notesData.find(n => n.id === activeNoteId);
    if (note) {
      note.comments = updatedNote.comments || [];
      note.updated_at = updatedNote.updated_at;
      renderComments(note.comments);
    }
    input.value = '';
  } catch (e) { console.error('Failed to add comment', e); }
}
async function deleteComment(noteId, text, timestamp) {
  if (!noteId) return;
  try {
    const data = await api(`/api/notes/${noteId}/comment/delete`, {
      method: 'POST',
      body: JSON.stringify({ text, timestamp })
    });
    if (data.ok) {
      const note = notesData.find(n => n.id === noteId);
      if (note) {
        note.comments = note.comments.filter(c => !(c.text === text && c.timestamp === timestamp));
        renderComments(note.comments);
      }
    } else {
      systemAlert('Deletion failed: ' + (data.error || 'Unknown error'));
    }
  } catch (e) { console.error('Comment deletion transmission failed', e); }
}
function toggleEditNote() {
  document.getElementById('note-detail-display').style.display = 'none';
  document.getElementById('note-detail-editor').style.display = 'block';
  document.getElementById('note-detail-title-display').style.display = 'none';
  document.getElementById('note-detail-title-editor').style.display = 'block';
  document.getElementById('note-edit-btn').style.display = 'none';
  document.getElementById('note-save-btn').style.display = 'inline-flex';
  document.getElementById('note-cancel-edit-btn').style.display = 'inline-flex';
  document.getElementById('note-color-picker').style.display = 'none';
  document.getElementById('note-detail-editor').focus();
}
function cancelEditNote() {
  const note = notesData.find(n => n.id === activeNoteId);
  document.getElementById('note-detail-editor').value = note ? note.text || '' : '';
  document.getElementById('note-detail-title-editor').value = note ? note.title || '' : '';
  document.getElementById('note-detail-display').style.display = 'block';
  document.getElementById('note-detail-editor').style.display = 'none';
  document.getElementById('note-detail-title-display').style.display = 'block';
  document.getElementById('note-detail-title-editor').style.display = 'none';
  document.getElementById('note-edit-btn').style.display = 'inline-flex';
  document.getElementById('note-save-btn').style.display = 'none';
  document.getElementById('note-cancel-edit-btn').style.display = 'none';
  document.getElementById('note-color-picker').style.display = 'flex';
}
async function saveNoteDetail() {
  if (!activeNoteId) return;
  const newTitle = document.getElementById('note-detail-title-editor').value;
  const newText = document.getElementById('note-detail-editor').value;
  const note = notesData.find(n => n.id === activeNoteId);
  if (!note) return;
  const similarityData = await api('/api/notes/check-similarity', {
    method: 'POST',
    body: JSON.stringify({ text: newText, exclude_id: activeNoteId })
  });
  if (similarityData.similar_notes && similarityData.similar_notes.length > 0) {
    const similarTitles = similarityData.similar_notes.map(n => n.title || 'Untitled').join(', ');
    const confirmed = await new Promise(resolve => {
      systemConfirm(`Warning: This note is highly similar to existing note(s): ${similarTitles}. Save anyway?`, resolve);
    });
    if (!confirmed) return;
  }
  note.title = newTitle;
  note.text = newText;
  document.getElementById('note-detail-title-display').textContent = newTitle || 'Untitled';
  document.getElementById('note-detail-display').textContent = newText;
  try {
    await api(`/api/notes/${activeNoteId}`, { method: 'PUT', body: JSON.stringify({ title: newTitle, text: newText }) });
    cancelEditNote();
  } catch (e) { console.error('Failed to save note', e); }
}
async function deleteNote() {
  if (!activeNoteId) return;
  systemConfirm('Permanently delete this operational note?', async () => {
    try {
      await api(`/api/notes/${activeNoteId}`, { method: 'DELETE' });
      notesData = notesData.filter(n => n.id !== activeNoteId);
      closeNoteDetail();
    } catch (e) { console.error('Failed to delete note', e); }
  });
}
async function toggleArchiveNote() {
  if (!activeNoteId) return;
  const note = notesData.find(n => n.id === activeNoteId);
  if (!note) return;
  const newArchiveStatus = !note.archived;
  try {
    await api(`/api/notes/${activeNoteId}/archive`, {
      method: 'POST',
      body: JSON.stringify({ archived: newArchiveStatus })
    });
    note.archived = newArchiveStatus;
    note.updated_at = Date.now() / 1000;
    document.getElementById('note-archive-btn').textContent = newArchiveStatus ? 'Restore' : 'Archive';
    filterNotes(document.getElementById('notes-search').value);
  } catch (e) { console.error('Failed to archive note', e); }
}
function openNoteModal() {
  document.getElementById('modal-title').textContent = 'New Note';
  document.getElementById('modal-note-title').value = '';
  document.getElementById('modal-textarea').value = '';
  renderModalColorPicker('');
  document.getElementById('note-modal').style.display = 'flex';
}
function closeNoteModal() { document.getElementById('note-modal').style.display = 'none'; }
function renderModalColorPicker(activeColor) {
  const picker = document.getElementById('modal-color-picker');
  picker.innerHTML = '';
  const defBtn = document.createElement('button');
  defBtn.className = `color-btn default ${!activeColor ? 'active' : ''}`;
  defBtn.setAttribute('aria-label', 'No color');
  defBtn.title = 'No color';
  defBtn.onclick = () => {
    picker.querySelectorAll('.color-btn').forEach(b => b.classList.remove('active'));
    defBtn.classList.add('active');
    document.getElementById('modal-note-color').value = '';
  };
  picker.appendChild(defBtn);
  NOTE_COLORS.forEach(color => {
    const btn = document.createElement('button');
    btn.className = `color-btn ${color === activeColor ? 'active' : ''}`;
    btn.style.backgroundColor = color;
    btn.setAttribute('aria-label', `Color ${color}`);
    btn.title = color;
    btn.onclick = () => {
      picker.querySelectorAll('.color-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('modal-note-color').value = color;
    };
    picker.appendChild(btn);
  });
}
async function saveNoteFromModal() {
  const title = document.getElementById('modal-note-title').value.trim();
  const text = document.getElementById('modal-textarea').value.trim();
  const color = document.getElementById('modal-note-color').value;
  const similarityData = await api('/api/notes/check-similarity', {
    method: 'POST',
    body: JSON.stringify({ text })
  });
  if (similarityData.similar_notes && similarityData.similar_notes.length > 0) {
    const similarTitles = similarityData.similar_notes.map(n => n.title || 'Untitled').join(', ');
    const confirmed = await new Promise(resolve => {
      systemConfirm(`Warning: This note is highly similar to existing note(s): ${similarTitles}. Create anyway?`, resolve);
    });
    if (!confirmed) return;
  }
  try {
    await api('/api/notes', { method: 'POST', body: JSON.stringify({ title, text, color }) });
    closeNoteModal();
    loadNotes();
  } catch (e) { console.error('Failed to create note', e); }
}

// ------------------------------------------------------------------ //
// custom code / misc
// ------------------------------------------------------------------ //
function openCustomCodeModal() {
  document.getElementById('custom-code-modal').style.display = 'flex';
  document.getElementById('custom-code-input').value = '';
  document.getElementById('custom-code-input').focus();
}
function closeCustomCodeModal() { document.getElementById('custom-code-modal').style.display = 'none'; }
async function submitCustomCode() {
  const code = document.getElementById('custom-code-input').value.trim();
  if (!code) return systemAlert('Code payload cannot be empty.');
  const btn = document.querySelector('#custom-code-modal .btn-primary');
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Transmitting...';
  try {
    const body = { code };
    const langEl = document.getElementById('cc-lang');
    if (langEl && langEl.value) body.language = langEl.value;
    const data = await api('/api/queue/custom_code', { method: 'POST', body: JSON.stringify(body) });
    if (data.ok) { systemAlert(`Code queued successfully. Generation: ${data.generation}`); closeCustomCodeModal(); }
    else systemAlert(`Queue failed: ${data.error}`);
  } catch (e) { systemAlert('Network error during transmission.'); }
  finally { btn.disabled = false; btn.textContent = origText; }
}

// ------------------------------------------------------------------ //
// global interaction: Esc closes, overlay click closes, keyboard acts
// ------------------------------------------------------------------ //
function topOpenModal() {
  return [...document.querySelectorAll('.modal-overlay')]
    .reverse().find(m => m.style.display === 'flex');
}
function dismissModal(m) {
  if (!m) return;
  m.style.display = 'none';
  if (m.id === 'live-output-modal') closeLiveModal();
  if (m.id === 'logs-modal') closeLogsModal();
  if (m.id === 'onboarding-modal') closeOnboarding();
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const m = topOpenModal();
    if (m) { dismissModal(m); return; }
    if (currentView === 'config') { closeSettingsBar(); return; }
    const expand = document.getElementById('status-expand');
    if (expand && expand.style.display === 'flex') toggleStatusExpand();
  }
});
document.addEventListener('click', (e) => {
  if (e.target && e.target.classList && e.target.classList.contains('modal-overlay')) {
    dismissModal(e.target);
  }
});
// Enter/Space activates any keyboard-focusable role=button (nav, tabs,
// LLM rows, color filters, worker tabs).
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const el = e.target;
  if (!el || !el.getAttribute) return;
  const role = el.getAttribute('role');
  if (role === 'button' || role === 'tab' || el.classList.contains('tab') || el.classList.contains('llm-row') || el.classList.contains('color-btn') || el.classList.contains('color-filter-chip') || el.classList.contains('note-item')) {
    e.preventDefault();
    el.click();
  }
});
// ------------------------------------------------------------------ //
// toast — lightweight non-blocking feedback
// ------------------------------------------------------------------ //
function toast(message) {
  let t = document.getElementById('toast-host');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast-host';
    t.setAttribute('aria-live', 'polite');
    document.body.appendChild(t);
  }
  const el = document.createElement('div');
  el.className = 'toast';
  el.textContent = message;
  t.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// ------------------------------------------------------------------ //
// logs (status pill)
// ------------------------------------------------------------------ //
function openLogsModal() {
  document.getElementById('logs-modal').style.display = 'flex';
  if (logsPollInterval) clearInterval(logsPollInterval);
  logsPollInterval = setInterval(fetchLogsModal, 1000);
  fetchLogsModal();
}
function closeLogsModal() {
  document.getElementById('logs-modal').style.display = 'none';
  if (logsPollInterval) clearInterval(logsPollInterval);
}
async function fetchLogsModal() {
  try {
    const data = await api('/api/debug/logs');
    const container = document.getElementById('logs-modal-content');
    const logs = data.logs || [];
    if (!logs.length) container.innerHTML = '<div class="console-line" style="color:var(--muted);">[SYSTEM] No logs captured yet.</div>';
    else container.innerHTML = logs.map(l => `<div class="console-line">${escapeHtml(l)}</div>`).join('');
  } catch (e) { console.warn('Logs poll failed', e); }
}

// ------------------------------------------------------------------ //
// init
// ------------------------------------------------------------------ //

document.getElementById('btn-logs').onclick = (e) => { e.stopPropagation(); openLogsModal(); };
document.getElementById('sp-file-program').addEventListener('change', () => attachFile('sp-file-program', 'spProgramFile', 'sp-program-chip'));
document.getElementById('sp-file-data').addEventListener('change', () => attachFile('sp-file-data', 'spDataFile', 'sp-data-chip'));
fillLangSelect(document.getElementById('pj-lang'));
fillLangSelect(document.getElementById('np-lang'));
fillLangSelect(document.getElementById('sp-lang'), true);
fillLangSelect(document.getElementById('cc-lang'));

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    openPalette();
  }
});

loadPrefs();

setInterval(fetchState, 1000);
setInterval(updateStatusPill, 1000);
setInterval(loadActive, 3000);

loadProjects();
loadActive();
startIterationPolling();
updateStatusPill();
maybeShowOnboarding();
