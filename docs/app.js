// ===== i18n =====
const i18n = {
  en: {
    title: "Polymarket Bot",
    capital: "Total Capital",
    todayPnl: "Today's P&L",
    totalPnl: "Total P&L",
    simulatedTodayPnl: "Simulated P&L Today",
    simulatedTotalPnl: "Simulated Total P&L",
    exposure: "Exposure",
    openPositions: "Open Positions",
    recentTrades: "Recent Trades",
    strategies: "Strategies",
    recentPnl: "Recent P&L",
    recentPnlSim: "Recent Simulated P&L",
    noPositions: "No open positions",
    noTrades: "No closed trades yet",
    lastUpdated: "Updated",
    market: "Market",
    side: "Side",
    entry: "Entry",
    current: "Current",
    size: "Size",
    pnl: "P&L",
    unrealisedPnlSim: "Unrealised P&L (Est.)",
    exit: "Exit",
    time: "Time",
    simulationNote: "DRY RUN uses simulated mark-to-mid P&L. There are no real fills, slippage, or execution guarantees.",
    statusReason: "Reason",
    statusResume: "Resume",
    mode: { LIVE: "LIVE", DRY_RUN: "DRY RUN" },
    botStatus: { RUNNING: "Running", STOPPED: "Stopped", PAUSED: "Paused" },
    strategyStatus: { ACTIVE: "Active", PAUSED: "Paused", DISABLED: "Disabled" },
  },
  pt: {
    title: "Polymarket Bot",
    capital: "Capital Total",
    todayPnl: "P&L Hoje",
    totalPnl: "P&L Total",
    simulatedTodayPnl: "P&L Simulado Hoje",
    simulatedTotalPnl: "P&L Simulado Total",
    exposure: "Exposicao",
    openPositions: "Posicoes Abertas",
    recentTrades: "Trades Recentes",
    strategies: "Estrategias",
    recentPnl: "P&L Recente",
    recentPnlSim: "P&L Simulado Recente",
    noPositions: "Sem posicoes abertas",
    noTrades: "Sem trades fechados",
    lastUpdated: "Actualizado",
    market: "Mercado",
    side: "Lado",
    entry: "Entrada",
    current: "Actual",
    size: "Tamanho",
    pnl: "P&L",
    unrealisedPnlSim: "P&L Nao Realizado (Estim.)",
    exit: "Saida",
    time: "Hora",
    simulationNote: "O DRY RUN usa P&L simulado por mark-to-mid. Nao ha fills reais, slippage real nem garantia de execucao.",
    statusReason: "Motivo",
    statusResume: "Retoma",
    mode: { LIVE: "REAL", DRY_RUN: "SIMULACAO" },
    botStatus: { RUNNING: "A correr", STOPPED: "Parado", PAUSED: "Em pausa" },
    strategyStatus: { ACTIVE: "Activo", PAUSED: "Em pausa", DISABLED: "Desactivado" },
  },
};

let currentLang = localStorage.getItem("lang") || "en";
let statusData = null;

function t(key) { return i18n[currentLang][key] || key; }

function setLang(lang) {
  currentLang = lang;
  localStorage.setItem("lang", lang);
  document.querySelectorAll(".lang-toggle button").forEach(b =>
    b.classList.toggle("active", b.dataset.lang === lang)
  );
  render();
}

// ===== Fetch =====
async function fetchStatus() {
  const paths = ["data/runtime_status.json", "data/status.json"];
  for (const path of paths) {
    try {
      const r = await fetch(path + "?t=" + Date.now());
      if (!r.ok) continue;
      statusData = await r.json();
      render();
      return;
    } catch (e) { /* try next path */ }
  }
}

// ===== Helpers =====
function escHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function pnlClass(v) { return v > 0 ? "positive" : v < 0 ? "negative" : "neutral"; }
function fmtUsd(v) { return (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2); }
function fmtPct(v) { return (v >= 0 ? "+" : "") + v.toFixed(2) + "%"; }
function sideBadge(s) {
  const cls = (s === "BUY" || s === "YES") ? "side-buy" : "side-sell";
  return `<span class="side-badge ${cls}">${escHtml(s)}</span>`;
}
function timeAgo(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return Math.floor(diff) + "s ago";
  if (diff < 3600) return Math.floor(diff / 60) + "m ago";
  if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
  return d.toLocaleDateString();
}

// ===== Mini Chart (pure canvas, no lib) =====
function drawPnlChart(trades) {
  const canvas = document.getElementById("pnl-chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  canvas.style.width = rect.width + "px";
  canvas.style.height = rect.height + "px";
  ctx.scale(dpr, dpr);

  const w = rect.width;
  const h = rect.height;
  const pad = { t: 10, r: 10, b: 24, l: 50 };
  const plotW = w - pad.l - pad.r;
  const plotH = h - pad.t - pad.b;

  ctx.clearRect(0, 0, w, h);

  if (!trades || trades.length === 0) {
    ctx.fillStyle = "#71717a";
    ctx.font = "12px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("No trade data yet", w / 2, h / 2);
    return;
  }

  const pnls = trades.slice().reverse().map(t => t.pnl_usdc || 0);
  const cumulative = [];
  let sum = 0;
  for (const p of pnls) { sum += p; cumulative.push(sum); }

  const min = Math.min(0, ...cumulative);
  const max = Math.max(0, ...cumulative);
  const range = max - min || 1;

  const xStep = plotW / Math.max(cumulative.length - 1, 1);
  const toY = v => pad.t + plotH - ((v - min) / range) * plotH;
  const toX = i => pad.l + i * xStep;

  ctx.strokeStyle = "#1f1f23";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (plotH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
  }

  const zeroY = toY(0);
  ctx.strokeStyle = "#27272a";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(pad.l, zeroY);
  ctx.lineTo(w - pad.r, zeroY);
  ctx.stroke();
  ctx.setLineDash([]);

  const lastVal = cumulative[cumulative.length - 1];
  const isPositive = lastVal >= 0;
  const color = isPositive ? "52, 211, 153" : "248, 113, 113";

  const gradient = ctx.createLinearGradient(0, pad.t, 0, pad.t + plotH);
  gradient.addColorStop(0, `rgba(${color}, ${isPositive ? 0.15 : 0.05})`);
  gradient.addColorStop(1, `rgba(${color}, 0.0)`);

  ctx.beginPath();
  ctx.moveTo(toX(0), zeroY);
  for (let i = 0; i < cumulative.length; i++) {
    ctx.lineTo(toX(i), toY(cumulative[i]));
  }
  ctx.lineTo(toX(cumulative.length - 1), zeroY);
  ctx.closePath();
  ctx.fillStyle = gradient;
  ctx.fill();

  ctx.beginPath();
  ctx.moveTo(toX(0), toY(cumulative[0]));
  for (let i = 1; i < cumulative.length; i++) {
    ctx.lineTo(toX(i), toY(cumulative[i]));
  }
  ctx.strokeStyle = `rgba(${color}, 0.9)`;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.stroke();

  const lastX = toX(cumulative.length - 1);
  const lastY = toY(lastVal);
  ctx.beginPath();
  ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
  ctx.fillStyle = `rgba(${color}, 1)`;
  ctx.fill();
  ctx.beginPath();
  ctx.arc(lastX, lastY, 7, 0, Math.PI * 2);
  ctx.strokeStyle = `rgba(${color}, 0.3)`;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.fillStyle = "#71717a";
  ctx.font = "10px 'JetBrains Mono', monospace";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const val = min + (range / 4) * (4 - i);
    const y = pad.t + (plotH / 4) * i;
    ctx.fillText("$" + val.toFixed(1), pad.l - 8, y + 3);
  }

  ctx.textAlign = "center";
  const labelInterval = Math.max(1, Math.floor(cumulative.length / 5));
  for (let i = 0; i < cumulative.length; i += labelInterval) {
    ctx.fillText("#" + (i + 1), toX(i), h - 4);
  }
}

// ===== Render =====
function render() {
  if (!statusData) return;
  const d = statusData;
  const l = i18n[currentLang];

  document.getElementById("page-title").textContent = t("title");

  const statusBadge = document.getElementById("status-badge");
  const statusText = l.botStatus[d.status] || d.status;
  statusBadge.innerHTML = `<span class="pulse"></span>${statusText}`;
  statusBadge.className = "badge badge-" + d.status.toLowerCase();

  const modeBadge = document.getElementById("mode-badge");
  modeBadge.textContent = l.mode[d.mode] || d.mode;
  modeBadge.className = "badge " + (d.mode === "LIVE" ? "badge-live" : "badge-dry-run");

  const updated = new Date(d.updated_at);
  const timeStr = isNaN(updated.getTime()) ? "--" : updated.toLocaleTimeString();
  document.getElementById("last-updated").textContent =
    t("lastUpdated") + " " + timeStr;

  const simulationNote = document.getElementById("simulation-note");
  if (d.is_simulated) {
    simulationNote.style.display = "block";
    simulationNote.textContent = t("simulationNote");
  } else {
    simulationNote.style.display = "none";
    simulationNote.textContent = "";
  }

  const statusNote = document.getElementById("status-note");
  const stopReason = d.stop_reason || "";
  const resumeAt = d.resume_at || "";
  if (d.status === "PAUSED" || d.status === "STOPPED") {
    const parts = [];
    if (stopReason) parts.push(`${t("statusReason")}: ${stopReason}`);
    if (resumeAt) {
      const resumeDate = new Date(resumeAt);
      const resumeText = isNaN(resumeDate.getTime()) ? resumeAt : resumeDate.toLocaleString();
      parts.push(`${t("statusResume")}: ${resumeText}`);
    }
    statusNote.style.display = "block";
    statusNote.className = "status-note " + d.status.toLowerCase();
    statusNote.textContent = parts.join(" • ") || statusText;
  } else {
    statusNote.style.display = "none";
    statusNote.className = "status-note";
    statusNote.textContent = "";
  }

  document.getElementById("capital-label").textContent = t("capital");
  document.getElementById("capital-value").textContent = "$" + d.capital_usdc.toFixed(2);

  document.getElementById("today-pnl-label").textContent =
    d.is_simulated ? t("simulatedTodayPnl") : t("todayPnl");
  const todayEl = document.getElementById("today-pnl-value");
  todayEl.textContent = fmtUsd(d.pnl_today_usdc);
  todayEl.className = "metric-value " + pnlClass(d.pnl_today_usdc);
  document.getElementById("today-pnl-pct").textContent = fmtPct(d.pnl_today_pct);
  document.getElementById("today-pnl-pct").className = "metric-sub " + pnlClass(d.pnl_today_pct);

  document.getElementById("total-pnl-label").textContent =
    d.is_simulated ? t("simulatedTotalPnl") : t("totalPnl");
  const totalEl = document.getElementById("total-pnl-value");
  totalEl.textContent = fmtUsd(d.pnl_total_usdc);
  totalEl.className = "metric-value " + pnlClass(d.pnl_total_usdc);

  const positions = d.open_positions || [];
  const trades = d.recent_trades || [];

  document.getElementById("exposure-label").textContent = t("exposure");
  const totalExposure = positions.reduce((s, p) => s + p.size_usdc, 0);
  document.getElementById("exposure-value").textContent = "$" + totalExposure.toFixed(2);
  const expPct = d.capital_usdc > 0 ? (totalExposure / d.capital_usdc) * 100 : 0;
  document.getElementById("exposure-pct").textContent = expPct.toFixed(1) + "% of capital";

  document.getElementById("strategies-title").textContent = t("strategies");
  const stratList = document.getElementById("strategies-list");
  const strats = Object.entries(d.strategies || {});
  stratList.innerHTML = strats.map(([name, status]) => {
    const cls = "strategy-" + status.toLowerCase();
    const label = l.strategyStatus[status] || status;
    const displayName = name.replace(/_/g, " ");
    return `<div class="strategy-card ${cls}">
      <div class="strategy-top">
        <span class="strategy-name">${escHtml(displayName)}</span>
        <span class="strategy-dot"></span>
      </div>
      <span class="strategy-status">${label}</span>
    </div>`;
  }).join("");

  document.getElementById("chart-title").textContent =
    d.is_simulated ? t("recentPnlSim") : t("recentPnl");
  drawPnlChart(trades);

  document.getElementById("positions-title").textContent = t("openPositions");
  document.getElementById("positions-count").textContent = positions.length;
  document.getElementById("th-market").textContent = t("market");
  document.getElementById("th-side").textContent = t("side");
  document.getElementById("th-entry").textContent = t("entry");
  document.getElementById("th-current").textContent = t("current");
  document.getElementById("th-size").textContent = t("size");
  document.getElementById("th-pnl").textContent =
    d.is_simulated ? t("unrealisedPnlSim") : t("pnl");

  const posTable = document.getElementById("positions-table");
  const posEmpty = document.getElementById("positions-empty");
  const posBody = document.getElementById("positions-body");

  if (positions.length === 0) {
    posTable.style.display = "none";
    posEmpty.style.display = "block";
    document.getElementById("positions-empty-text").textContent = t("noPositions");
  } else {
    posTable.style.display = "table";
    posEmpty.style.display = "none";
    posBody.innerHTML = positions.map(p => `<tr>
      <td>${escHtml(p.market)}</td>
      <td>${sideBadge(p.side)}</td>
      <td class="mono">${p.entry_price.toFixed(3)}</td>
      <td class="mono">${p.current_price.toFixed(3)}</td>
      <td class="mono">$${p.size_usdc.toFixed(2)}</td>
      <td class="pnl-cell ${pnlClass(p.unrealised_pnl_usdc)}">${fmtUsd(p.unrealised_pnl_usdc)}</td>
    </tr>`).join("");
  }

  document.getElementById("trades-title").textContent = t("recentTrades");
  document.getElementById("trades-count").textContent = trades.length;
  document.getElementById("th-t-market").textContent = t("market");
  document.getElementById("th-t-side").textContent = t("side");
  document.getElementById("th-t-entry").textContent = t("entry");
  document.getElementById("th-t-exit").textContent = t("exit");
  document.getElementById("th-t-pnl").textContent = t("pnl");
  document.getElementById("th-t-time").textContent = t("time");

  const trTable = document.getElementById("trades-table");
  const trEmpty = document.getElementById("trades-empty");
  const trBody = document.getElementById("trades-body");

  if (trades.length === 0) {
    trTable.style.display = "none";
    trEmpty.style.display = "block";
    document.getElementById("trades-empty-text").textContent = t("noTrades");
  } else {
    trTable.style.display = "table";
    trEmpty.style.display = "none";
    trBody.innerHTML = trades.map(tr => `<tr>
      <td>${escHtml(tr.market)}</td>
      <td>${sideBadge(tr.side)}</td>
      <td class="mono">${tr.entry_price.toFixed(3)}</td>
      <td class="mono">${tr.exit_price.toFixed(3)}</td>
      <td class="pnl-cell ${pnlClass(tr.pnl_usdc)}">${fmtUsd(tr.pnl_usdc)}</td>
      <td class="mono" style="color:var(--text-tertiary)">${timeAgo(tr.closed_at)}</td>
    </tr>`).join("");
  }
}

// ===== Init =====
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".lang-toggle button").forEach(b => {
    b.addEventListener("click", () => setLang(b.dataset.lang));
    b.classList.toggle("active", b.dataset.lang === currentLang);
  });
  fetchStatus();
  setInterval(fetchStatus, 15000);
  window.addEventListener("resize", () => { if (statusData) drawPnlChart(statusData.recent_trades); });
});
