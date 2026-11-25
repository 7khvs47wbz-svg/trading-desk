const $ = (s) => document.querySelector(s);
const fmt = (n, d = 2) => (n == null || isNaN(n) ? "—" : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
const money = (n) => (n == null ? "—" : (n < 0 ? "−$" : "$") + fmt(Math.abs(n)));
const icon = (id, label) => `<svg class="i" ${label ? `role="img" aria-label="${label}"` : 'aria-hidden="true"'}><use href="#i-${id}"/></svg>`;
// Gains/losses carry a sign and an arrow, not just colour.
const signed = (n, text) => (n == null || isNaN(n) ? "—" : n === 0 ? text : `${icon(n > 0 ? "up" : "down", n > 0 ? "up" : "down")}${n > 0 ? "+" : ""}${text}`);
const pnl = (n) => signed(n, n < 0 ? money(n) : money(n));
const pct = (n) => signed(n, fmt(n) + "%");
const cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const charts = {};
const skeleton = (label) => `<div class="skeleton" aria-hidden="true"><div></div><div></div><div></div></div><div class="loading-label" role="status">${esc(label)}</div>`;

async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET"), headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch("/api" + path, opts);
  let j = {};
  try { j = await r.json(); } catch (e) { throw new Error(r.status >= 500 ? "The server is busy or waking up. Try again in a few seconds." : r.statusText); }
  if (r.status === 401 && j.auth) { showAuth(); throw new Error("Please sign in again."); }
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, err) { const t = $("#toast"); t.textContent = msg; t.className = err ? "err" : ""; t.setAttribute("role", err ? "alert" : "status"); t.style.display = "block"; clearTimeout(t._h); t._h = setTimeout(() => (t.style.display = "none"), err ? 9000 : 5000); }
async function busy(btn, fn) {
  btn = btn.closest("button") || btn;
  btn.disabled = true; btn.setAttribute("aria-busy", "true");
  try { await fn(); } catch (e) { toast(e.message, true); } finally { btn.disabled = false; btn.removeAttribute("aria-busy"); }
}
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function scoreColor(s) { return s >= 60 ? "var(--gain)" : s >= 40 ? "var(--warn)" : "var(--loss)"; }
function badge(text) {
  const good = ["NEW_ENTRY_ALLOWED", "GO", "TRADING_ALLOWED", "BROAD"], bad = ["CASH_PRIORITY", "NO_GO", "HALTED", "COOLDOWN"];
  const kind = good.includes(text) ? "good" : bad.includes(text) ? "bad" : "warn";
  return `<span class="badge b-${kind}">${icon(kind === "good" ? "check" : kind === "bad" ? "ban" : "alert")}${esc(text)}</span>`;
}
function notice(kind, html) { return `<div class="notice ${kind}" role="${kind === "bad" ? "alert" : "status"}">${icon(kind === "good" ? "check" : kind === "bad" ? "ban" : "alert")}<div>${html}</div></div>`; }
function card(k, v, s = "", c = "") { return `<div class="card"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${s}</div></div>`; }
function table(cols, rows, empty = "Nothing yet.") {
  return `<table><thead><tr>${cols.map((c) => `<th scope="col" class="${c.num ? "num" : ""}">${c.h || `<span class="sr-only">Actions</span>`}</th>`).join("")}</tr></thead><tbody>${
    rows.map((r) => `<tr>${cols.map((c) => `<td class="${c.num ? "num" : ""} ${c.k === "symbol" ? "sym" : ""} ${c.cls ? c.cls(r) : ""}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join("")}</tr>`).join("") ||
    `<tr><td colspan="${cols.length}" class="empty">${empty}</td></tr>`}</tbody></table>`;
}

// ---------------------------------------------------------------- charts (theme-aware)
const chartCfgs = {};
function drawChart(id, cfg) {
  chartCfgs[id] = cfg;
  if (charts[id]) charts[id].destroy();
  const text = css("--muted"), grid = css("--border-soft");
  cfg.options = cfg.options || {};
  cfg.options.maintainAspectRatio = false;
  cfg.options.animation = matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 250 };
  cfg.options.interaction = { mode: "index", intersect: false };
  if (cfg.type !== "doughnut") {
    cfg.options.scales = cfg.options.scales || {};
    for (const ax of ["x", "y"]) cfg.options.scales[ax] = { ...(cfg.options.scales[ax] || {}), ticks: { color: text, maxTicksLimit: ax === "x" ? 6 : 5, font: { family: "Fira Code", size: 11 } }, grid: { color: grid } };
  }
  cfg.options.plugins = { ...(cfg.options.plugins || {}), tooltip: { bodyFont: { family: "Fira Code" } } };
  if (cfg.options.plugins.legend?.display !== false) cfg.options.plugins.legend = { ...(cfg.options.plugins.legend || {}), labels: { color: css("--text"), font: { family: "Fira Sans" } } };
  charts[id] = new Chart($("#" + id), cfg);
}
const redrawCharts = () => Object.entries(chartCfgs).forEach(([id, cfg]) => document.getElementById(id) && drawChart(id, cfg));

// ---------------------------------------------------------------- theme
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch (e) { /* private mode */ }
  const b = $("#theme-toggle");
  b.innerHTML = icon(t === "light" ? "moon" : "sun");
  b.setAttribute("aria-label", `Switch to ${t === "light" ? "dark" : "light"} theme`);
  redrawCharts();
}
$("#theme-toggle").onclick = () => setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
setTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");

// ---------------------------------------------------------------- tabs
const loaders = {};
const tabButtons = [...document.querySelectorAll("#tabs button")];
tabButtons.forEach((b, i) => {
  b.onclick = () => {
    tabButtons.forEach((x) => { x.setAttribute("aria-selected", x === b); x.tabIndex = x === b ? 0 : -1; });
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === b.dataset.tab));
    if (location.hash.slice(1) !== b.dataset.tab) history.replaceState(null, "", "#" + b.dataset.tab);
    loaders[b.dataset.tab]?.();
  };
  b.onkeydown = (e) => {
    const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (d) { const n = tabButtons[(i + d + tabButtons.length) % tabButtons.length]; n.focus(); n.click(); }
  };
});
window.addEventListener("hashchange", () => {
  const b = document.querySelector(`[data-tab="${location.hash.slice(1)}"]`);
  if (b && b.getAttribute("aria-selected") !== "true") b.click();
});

// ---------------------------------------------------------------- cockpit
function components(comps) {
  return Object.entries(comps || {}).map(([k, c]) => `<div class="comp" title="${esc(c.signal)}"><span>${k.replace(/_/g, " ")}</span><b>${c.data_available === false ? "—" : Math.round(c.score)}</b><div class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(c.score)}" aria-label="${k.replace(/_/g, " ")}"><div style="width:${c.score}%;background:${scoreColor(c.score)}"></div></div></div>`).join("");
}
function analyzerPanel(a) {
  if (!a) return `<p class="muted">No data yet. Use “Refresh from public data”.</p>`;
  const c = a.composite;
  return `<div class="row between wrap"><div><span class="score" style="color:${scoreColor(c.composite_score)}">${fmt(c.composite_score, 1)}</span> <span class="muted">/ 100 · ${esc(c.zone_detail || c.zone)}</span></div><div class="muted small">Data as of ${esc(a.data_provenance?.as_of)}</div></div>
    <p>${esc(c.guidance)}</p>${components(a.components)}<ul class="actions">${(c.actions || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`;
}
async function loadCockpit(refresh) {
  if (refresh || !$("#posture").children.length) $("#posture").innerHTML = `<div class="card wide">${skeleton("Running market-breadth-analyzer, uptrend-analyzer and exposure-coach…")}</div>`;
  const [r, cb] = await Promise.all([api("/regime" + (refresh ? "?refresh=1" : "")), api("/circuit")]);
  const e = r.exposure || {};
  $("#posture").innerHTML =
    card("Posture", badge(e.recommendation || "—"), `Confidence ${esc(e.confidence)}`) +
    card("Exposure ceiling", `${e.exposure_ceiling_pct ?? "—"}%`, "Max equity allocation for new risk") +
    card("Participation", esc(e.participation || "—"), `Bias: ${esc(e.bias || "—")}`) +
    card("Circuit breaker", badge(cb.recommendation), `Today ${money(cb.pnl_today)} · ${cb.losing_streak} losses in a row`);
  if (e.rationale) $("#posture").insertAdjacentHTML("beforeend", `<div class="card wide"><div class="k">Exposure Coach rationale</div><div style="margin-top:4px">${esc(e.rationale)}</div><div class="s">Not yet included (need FMP or other skills): ${esc((e.inputs_missing || []).join(", "))}</div></div>`);
  $("#breadth").innerHTML = analyzerPanel(r.breadth);
  $("#uptrend").innerHTML = analyzerPanel(r.uptrend);
  const h = (r.breadth?.trend_summary?.entries) || r.breadth_history || [];
  drawChart("breadth-chart", { type: "line", data: { labels: h.map((x) => x.data_date), datasets: [{ label: "Breadth score", data: h.map((x) => x.composite_score), borderColor: css("--info"), backgroundColor: css("--info"), pointRadius: h.length > 30 ? 0 : 3, tension: .3 }] }, options: { scales: { y: { min: 0, max: 100 } }, plugins: { legend: { display: false } } } });
  $("#circuit").innerHTML = `<p>${badge(cb.recommendation)}</p><div class="scroll">` + table(
    [{ h: "Window", k: "w" }, { h: "Realized P&L", num: 1, f: (x) => pnl(x.v), cls: (x) => cls(x.v) }, { h: "Halt at", num: 1, k: "l" }],
    [{ w: "Today", v: cb.pnl_today, l: `−${cb.limits.daily}%` }, { w: "This week", v: cb.pnl_week, l: `−${cb.limits.weekly}%` }, { w: "This month", v: cb.pnl_month, l: `−${cb.limits.monthly}%` }]) +
    `</div><p class="muted small">Losing streak ${cb.losing_streak}; a 24h cooldown starts after ${cb.limits.streak_n}. Computed from closed trades in your journal.</p>` +
    (cb.triggered_rules.length ? notice("bad", `${cb.triggered_rules.map((x) => esc(x.rule.replace(/_/g, " "))).join(", ")} active`) : "");
  if (r.errors?.length) toast(r.errors.join("\n"), true);
}
loaders.cockpit = () => loadCockpit().catch((e) => toast(e.message, true));
$("#regime-refresh").onclick = (ev) => busy(ev.target, () => loadCockpit(true));

// ---------------------------------------------------------------- screener
const yes = (v) => (v ? `<span class="pos">${icon("check", "yes")}</span>` : `<span class="sr-only">no</span>`);
const screenCols = {
  vcp: [{ h: "Symbol", k: "symbol" }, { h: "Score", k: "score", num: 1 }, { h: "Price", num: 1, f: (r) => fmt(r.price) }, { h: "RS", k: "rs", num: 1 }, { h: "Pivot", num: 1, f: (r) => fmt(r.pivot) }, { h: "To pivot", num: 1, f: (r) => fmt(r.dist_to_pivot_pct) + "%" }, { h: "Pullbacks %", f: (r) => `<span class="num">${r.contractions.join(" → ")}</span> ${yes(r.contracting)}` }, { h: "Vol dry-up", f: (r) => yes(r.volume_dry_up) }, { h: "Stop", num: 1, f: (r) => fmt(r.suggested_stop) }],
  momentum: [{ h: "Symbol", k: "symbol" }, { h: "Days ago", k: "days_ago", num: 1 }, { h: "Burst", num: 1, f: (r) => pct(r.change_pct), cls: () => "pos" }, { h: "Vol ×", num: 1, k: "vol_ratio" }, { h: "Price", num: 1, f: (r) => fmt(r.price) }, { h: "RS", k: "rs", num: 1 }, { h: "Stop (burst low)", num: 1, f: (r) => fmt(r.suggested_stop) }],
  canslim: [{ h: "Symbol", k: "symbol" }, { h: "Name", k: "name" }, { h: "Score", k: "score", num: 1 }, { h: "Price", num: 1, f: (r) => fmt(r.price) }, { h: "RS", k: "rs", num: 1 }, { h: "EPS growth", num: 1, f: (r) => fmt(r.eps_growth_pct, 1) + "%" }, { h: "Rev growth", num: 1, f: (r) => fmt(r.rev_growth_pct, 1) + "%" }, { h: "Passed", f: (r) => `<span class="num" title="${esc(Object.keys(r.checks).join(" · "))}">${Object.entries(r.checks).filter(([, v]) => v).map(([k]) => k[0]).join(" ")}</span>` }],
  value: [{ h: "Symbol", k: "symbol" }, { h: "Name", k: "name" }, { h: "Sector", k: "sector" }, { h: "Price", num: 1, f: (r) => fmt(r.price) }, { h: "Yield", num: 1, f: (r) => fmt(r.yield_pct) + "%" }, { h: "P/E", num: 1, f: (r) => fmt(r.pe, 1) }, { h: "P/B", num: 1, f: (r) => fmt(r.pb, 1) }, { h: "Payout", num: 1, f: (r) => fmt(r.payout_pct, 0) + "%" }, { h: "Score", num: 1, k: "score" }],
};
$("#screen-run").onclick = (ev) => busy(ev.target, async () => {
  const kind = $("#screen-kind").value;
  $("#screen-note").textContent = "";
  $("#screen-results").innerHTML = `<div class="panel">${skeleton(kind === "value" || kind === "canslim" ? "Fetching fundamentals for each ticker; this can take a minute…" : "Downloading price history and screening…")}</div>`;
  try {
    const r = await api("/screen", { kind, universe: $("#screen-universe").value });
    if (kind === "fmp_vcp") { $("#screen-results").innerHTML = `<pre class="panel scroll">${esc(JSON.stringify(r.results, null, 2)).slice(0, 20000)}</pre>`; return; }
    const cols = [...screenCols[kind], { h: "", f: (x) => `<button class="small" onclick="tradeFrom('${esc(x.symbol)}', ${Number(x.suggested_stop) || "null"})" aria-label="Paper trade ${esc(x.symbol)}">Paper trade</button>` }];
    $("#screen-note").textContent = `${r.results.length} matches out of ${r.universe_size} tickers.`;
    $("#screen-results").innerHTML = `<div class="panel scroll">${table(cols, r.results, "No tickers passed this screen. Try another screen or a wider universe.")}</div>`;
  } catch (e) { $("#screen-results").innerHTML = ""; throw e; }
});
$("#screen-bt").onclick = (ev) => busy(ev.target, async () => {
  const kind = $("#screen-kind").value;
  $("#bt-result").innerHTML = `<div class="panel">${skeleton("Re-running the screen on every 10th trading day of the past year, using only the data available on each day…")}</div>`;
  try {
    const b = await api("/backtest", { kind, universe: $("#screen-universe").value });
    if (!b.supported) { $("#bt-result").innerHTML = notice("warn", `<b>No backtest for this screen.</b> ${esc(b.reason || "")}`); return; }
    if (!b.picks) { $("#bt-result").innerHTML = notice("warn", "The screen picked nothing in the past year on this universe, so there is nothing to measure."); return; }
    const edge = b.avg_excess_pct > 0 && b.beat_spy_pct > 50;
    $("#bt-result").innerHTML = `<div class="panel"><h3>Backtest: ${esc($("#screen-kind").selectedOptions[0].text.split(" (")[0])}</h3>
      <p class="muted small">${b.picks} picks on ${b.periods} dates from ${esc(b.from)} to ${esc(b.to)}. Each pick is held ${b.holding_days} trading days and compared with SPY over the same days. Top 10 picks per date; no costs or slippage.</p>
      <div class="cards">${card("Picks that rose", fmt(b.win_rate_pct, 1) + "%", "after " + b.holding_days + " days")}${card("Beat SPY", fmt(b.beat_spy_pct, 1) + "%", "of picks")}${card("Average return", pct(b.avg_return_pct), "median " + fmt(b.median_return_pct) + "%", cls(b.avg_return_pct))}${card("Average vs SPY", pct(b.avg_excess_pct), "", cls(b.avg_excess_pct))}${card("Range", fmt(b.worst_pct, 1) + "% to " + fmt(b.best_pct, 1) + "%", "worst to best pick")}</div>
      ${notice(edge ? "good" : "warn", edge ? "This screen has beaten SPY on average over this period. Past results don't guarantee future ones." : "This screen has <b>not</b> reliably beaten SPY over this period. Treat its picks as ideas to research, not signals.")}
      <details><summary>Most recent picks</summary><div class="scroll">${table([{ h: "Date", k: "date" }, { h: "Symbol", k: "symbol" }, { h: "20-day return", num: 1, f: (r) => pct(r.ret * 100), cls: (r) => cls(r.ret) }, { h: "vs SPY", num: 1, f: (r) => pct(r.excess * 100), cls: (r) => cls(r.excess) }], b.recent)}</div></details></div>`;
  } catch (e) { $("#bt-result").innerHTML = ""; throw e; }
});
$("#screen-kind").onchange = () => { $("#bt-result").innerHTML = ""; };
window.tradeFrom = (sym, stop) => {
  $("#pt-symbol").value = sym; $("#pt-stop").value = stop || ""; $("#pt-setup").value = $("#screen-kind").selectedOptions[0].text.split(" (")[0];
  document.querySelector('[data-tab="paper"]').click();
  $("#pt-thesis").focus();
};

// ---------------------------------------------------------------- paper trading
function renderPaper(p) {
  const open = p.positions.reduce((a, x) => a + x.pnl, 0);
  $("#paper-summary").innerHTML = card("Paper equity", money(p.equity), `Started at ${money(p.start)}`) +
    card("Return", pct(p.return_pct), "Since last reset", cls(p.return_pct)) + card("Cash", money(p.cash), `${fmt(p.cash / p.equity * 100, 0)}% of equity`) +
    card("Open positions", p.positions.length, `<span class="${cls(open)}">${pnl(open)}</span> open P&L`);
  $("#pt-positions").innerHTML = table([{ h: "Symbol", k: "symbol" }, { h: "Shares", num: 1, f: (r) => fmt(r.shares, 0) }, { h: "Entry", num: 1, f: (r) => fmt(r.entry) }, { h: "Last", num: 1, f: (r) => fmt(r.price) }, { h: "Stop", num: 1, f: (r) => fmt(r.stop) }, { h: "P&L", num: 1, f: (r) => `${pnl(r.pnl)} <span class="small">(${fmt(r.pnl_pct)}%)</span>`, cls: (r) => cls(r.pnl) }, { h: "Gate", f: (r) => badge(r.gate_decision) + (r.overridden ? " <span class='muted small'>overridden</span>" : "") }, { h: "", f: (r) => `<button class="small" onclick="sellPos(${r.id}, '${esc(r.symbol)}')">Close</button>` }], p.positions, "No open positions. Size and check a trade on the left.");
  $("#pt-orders").innerHTML = table([{ h: "Time", f: (r) => `<span class="num">${r.at.slice(0, 16).replace("T", " ")}</span>` }, { h: "Side", f: (r) => `<span class="badge ${r.side === "BUY" ? "b-good" : "b-warn"}">${r.side}</span>` }, { h: "Symbol", k: "symbol" }, { h: "Shares", num: 1, f: (r) => fmt(r.shares, 0) }, { h: "Price", num: 1, f: (r) => fmt(r.price) }, { h: "Note", k: "note" }], p.orders.slice(0, 15), "No orders yet.");
}
loaders.paper = () => api("/paper").then(renderPaper).catch((e) => toast(e.message, true));
$("#pt-preview").onclick = (ev) => busy(ev.target, async () => {
  const sym = $("#pt-symbol").value.trim();
  if (!sym) { $("#pt-symbol").focus(); throw new Error("Enter a symbol first."); }
  $("#pt-result").innerHTML = skeleton("Running position-sizer and pre-trade-discipline-gate…");
  try {
    const r = await api("/paper/preview", { symbol: sym, stop: $("#pt-stop").value, thesis: $("#pt-thesis").value, has_plan: $("#pt-plan").checked });
    const g = r.gate.candidate_results?.[0] || {};
    const d = r.gate.overall_decision;
    const reasons = (g.reasons || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>All checks passed.</li>";
    const pc = r.quote.price_check || {};
    const pcNote = pc.ok === false ? notice("bad", `<b>Price check failed.</b> ${esc(pc.message)} Paper buys are blocked until the sources agree.`)
      : `<p class="price-check muted">${icon(pc.ok ? "check" : "info")} ${esc(pc.message || "")}${pc.second_source ? ` Nasdaq: ${fmt(pc.second_source)}.` : ""}</p>`;
    $("#pt-result").innerHTML = `
      <div class="cards" style="margin-top:var(--sp-4)">${card(r.quote.symbol, money(r.entry), `<span class="${cls(r.quote.change_pct)}">${pct(r.quote.change_pct)}</span> · ATR ${fmt(r.quote.atr)}`)}${card("Size", fmt(r.shares, 0) + " sh", money(r.cost) + " position")}${card("Risk", money(r.risk_dollars), `Stop ${fmt(r.stop)} · ${fmt(r.sizer.final_risk_pct)}% of equity`)}</div>
      ${pcNote}
      ${notice(d === "GO" ? "good" : d === "NO_GO" ? "bad" : "warn", `<b>Discipline gate</b> ${badge(d)}<ul class="actions">${reasons}</ul>`)}
      <div class="form"><label>Shares to buy <input id="pt-shares" type="number" value="${r.shares}" inputmode="numeric"></label>
      ${d !== "GO" ? `<label class="check full"><input type="checkbox" id="pt-override"> Override the gate (recorded in the journal)</label>` : ""}</div>
      <button class="primary" id="pt-buy">Place paper buy</button>
      <div class="chart-box sm" style="margin-top:var(--sp-4)"><canvas id="pt-chart" role="img" aria-label="${r.quote.symbol} closing price, last 6 months, with stop line"></canvas></div>`;
    drawChart("pt-chart", { type: "line", data: { labels: r.quote.chart.map((x) => x.t), datasets: [{ label: "Close", data: r.quote.chart.map((x) => x.c), borderColor: css("--info"), pointRadius: 0, borderWidth: 2 }, { label: "Stop", data: r.quote.chart.map(() => r.stop), borderColor: css("--loss"), borderDash: [5, 4], pointRadius: 0, borderWidth: 1.5 }] }, options: { plugins: { legend: { position: "bottom", labels: { boxHeight: 2 } } } } });
    $("#pt-buy").onclick = (e2) => busy(e2.target, async () => {
      const shares = $("#pt-shares").value;
      const p = await api("/paper/buy", { symbol: r.quote.symbol, shares, stop: r.stop, target: $("#pt-target").value || null, setup: $("#pt-setup").value, thesis: $("#pt-thesis").value, gate_decision: d, override: $("#pt-override")?.checked });
      renderPaper(p); $("#pt-result").innerHTML = ""; toast(`Paper bought ${shares} ${r.quote.symbol}`);
    });
  } catch (e) { $("#pt-result").innerHTML = ""; throw e; }
});
window.sellPos = async (id, sym) => { const reason = prompt(`Close ${sym}. Exit reason?`, "manual exit"); if (reason === null) return; try { renderPaper(await api(`/paper/sell/${id}`, { reason })); toast(`Closed ${sym} and logged it to the journal`); } catch (e) { toast(e.message, true); } };
$("#pt-stops").onclick = (ev) => busy(ev.target, async () => { const r = await api("/paper/check-stops", {}); renderPaper(r); toast(r.closed.length ? "Closed: " + r.closed.join(", ") : "No stops or targets hit"); });
$("#pt-reset").onclick = async () => { const c = prompt("Reset the paper account? Open positions and order history are cleared; the journal is kept.\n\nStarting cash:", "100000"); if (c) { try { renderPaper(await api("/paper/reset", { cash: c })); toast("Paper account reset"); } catch (e) { toast(e.message, true); } } };

// ---------------------------------------------------------------- portfolio
function renderPortfolio(p) {
  const gain = p.total_value - p.cost;
  $("#pf-summary").innerHTML = card("Holdings value", money(p.total_value), `Cost ${money(p.cost)}`) + card("Unrealized gain", pnl(gain), p.cost ? fmt(gain / p.cost * 100) + "%" : "", cls(gain)) +
    card("Annual dividends", money(p.annual_income), p.total_value ? fmt(p.annual_income / p.total_value * 100) + "% yield" : "") +
    card("Equity allocation", fmt(p.equity_pct, 0) + "%", p.exposure_ceiling != null ? `Exposure Coach ceiling ${p.exposure_ceiling}%` : "Open Cockpit to get the ceiling");
  $("#pf-warnings").innerHTML = p.warnings.map((w) => notice("warn", esc(w))).join("");
  $("#pf-cash").value = p.cash;
  $("#pf-table").innerHTML = table([{ h: "Symbol", k: "symbol" }, { h: "Name", f: (r) => esc((r.name || "").slice(0, 22)) }, { h: "Shares", num: 1, f: (r) => fmt(r.shares, 2) }, { h: "Price", num: 1, f: (r) => fmt(r.price) }, { h: "Day", num: 1, f: (r) => pct(r.change_pct), cls: (r) => cls(r.change_pct) }, { h: "Value", num: 1, f: (r) => money(r.value) }, { h: "Gain", num: 1, f: (r) => pnl(r.gain), cls: (r) => cls(r.gain) }, { h: "Weight", num: 1, f: (r) => fmt(r.weight_pct, 1) + "%" }, { h: "Yield", num: 1, f: (r) => fmt(r.yield_pct) + "%" }, { h: "", f: (r) => `<button class="icon" onclick="delHolding(${r.id}, '${esc(r.symbol)}')" aria-label="Remove ${esc(r.symbol)}">${icon("x")}</button>` }], p.holdings, "No holdings yet. Add one below or import a CSV.");
  const s = Object.entries(p.sectors);
  const palette = ["#60a5fa", "#22c55e", "#fbbf24", "#a78bfa", "#f472b6", "#2dd4bf", "#fb923c", "#94a3b8"];
  drawChart("pf-chart", { type: "doughnut", data: { labels: s.map((x) => x[0]), datasets: [{ data: s.map((x) => x[1]), backgroundColor: palette, borderColor: css("--card"), borderWidth: 2 }] }, options: { cutout: "62%", plugins: { legend: { position: "right" } } } });
}
loaders.portfolio = () => { $("#pf-table").innerHTML = skeleton("Loading quotes…"); api("/holdings").then(renderPortfolio).catch((e) => toast(e.message, true)); };
$("#pf-add").onclick = (ev) => busy(ev.target, async () => {
  if (!$("#pf-symbol").value.trim() || !$("#pf-shares").value) { $("#pf-symbol").focus(); throw new Error("Enter a symbol and a share count."); }
  renderPortfolio(await api("/holdings", { symbol: $("#pf-symbol").value, shares: $("#pf-shares").value, cost_basis: $("#pf-cost").value || 0 }));
  ["#pf-symbol", "#pf-shares", "#pf-cost"].forEach((s) => ($(s).value = "")); toast("Holding saved");
});
$("#pf-import").onclick = (ev) => busy(ev.target, async () => { const r = await api("/holdings/import", { csv: $("#pf-csv").value }); renderPortfolio(r); toast(`Imported ${r.imported} rows`); });
$("#pf-cash-save").onclick = (ev) => busy(ev.target, async () => { await api("/settings", { portfolio_cash: Number($("#pf-cash").value) }); loaders.portfolio(); toast("Cash saved"); });
window.delHolding = async (id, sym) => { if (!confirm(`Remove ${sym} from your holdings?`)) return; try { renderPortfolio(await api(`/holdings/${id}`, null, "DELETE")); } catch (e) { toast(e.message, true); } };

// ---------------------------------------------------------------- journal
function renderJournal(j) {
  const s = j.stats;
  $("#jr-stats").innerHTML = card("Closed trades", s.trades) + card("Win rate", s.win_rate != null ? fmt(s.win_rate * 100, 0) + "%" : "—") +
    card("Avg win / loss", `${money(s.avg_win)} / ${money(s.avg_loss)}`) + card("Profit factor", fmt(s.profit_factor)) +
    card("Expectancy / trade", pnl(s.expectancy), "", cls(s.expectancy)) + card("Net P&L", pnl(s.net_pnl), "", cls(s.net_pnl)) +
    card("Plan adherence", s.plan_adherence_pct != null ? s.plan_adherence_pct + "%" : "—", `${s.overrides} gate overrides`);
  drawChart("jr-chart", { type: "line", data: { labels: j.curve.map((x) => x.t), datasets: [{ label: "Cumulative P&L", data: j.curve.map((x) => x.pnl), borderColor: css("--gain"), pointRadius: j.curve.length > 30 ? 0 : 3, tension: .2, segment: { borderColor: (c) => (c.p1.parsed.y < 0 ? css("--loss") : css("--gain")) } }] }, options: { plugins: { legend: { display: false } } } });
  $("#jr-weekly").innerHTML = table([{ h: "Week", k: "week" }, { h: "Trades", k: "trades", num: 1 }, { h: "Win rate", num: 1, f: (r) => fmt(r.wins / r.trades * 100, 0) + "%" }, { h: "P&L", num: 1, f: (r) => pnl(r.pnl), cls: (r) => cls(r.pnl) }], j.weekly, "Close a trade to start your weekly digest.");
  $("#jr-table").innerHTML = table([{ h: "Closed", f: (r) => `<span class="num">${(r.closed_at || "").slice(0, 10)}</span>` }, { h: "Symbol", k: "symbol" }, { h: "Source", k: "source" }, { h: "Setup", k: "setup" }, { h: "Entry", num: 1, f: (r) => fmt(r.entry) }, { h: "Exit", num: 1, f: (r) => fmt(r.exit) }, { h: "P&L", num: 1, f: (r) => pnl(r.pnl), cls: (r) => cls(r.pnl) }, { h: "R", num: 1, f: (r) => fmt(r.r_multiple) }, { h: "Exit reason", k: "exit_reason" }, { h: "Gate", f: (r) => (r.gate_decision ? badge(r.gate_decision) : "") + (r.overridden ? " <span class='muted small'>overridden</span>" : "") },
    { h: "Followed plan", f: (r) => `<select aria-label="Followed plan for ${r.symbol}" onchange="patchJ(${r.id},{followed_plan:this.value===''?null:Number(this.value)})"><option value="" ${r.followed_plan == null ? "selected" : ""}>—</option><option value="1" ${r.followed_plan === 1 ? "selected" : ""}>Yes</option><option value="0" ${r.followed_plan === 0 ? "selected" : ""}>No</option></select>` },
    { h: "Lessons", f: (r) => `<input aria-label="Lessons for ${r.symbol}" value="${esc(r.lessons)}" placeholder="What did I learn?" onchange="patchJ(${r.id},{lessons:this.value})" style="min-width:220px">` },
    { h: "", f: (r) => `<button class="icon" onclick="delJ(${r.id}, '${esc(r.symbol)}')" aria-label="Delete ${esc(r.symbol)} journal entry">${icon("x")}</button>` }], j.entries, "No trades yet. Closed paper trades appear here automatically.");
}
loaders.journal = () => api("/journal").then(renderJournal).catch((e) => toast(e.message, true));
window.patchJ = async (id, body) => { try { await api(`/journal/${id}`, body, "PATCH"); toast("Saved"); } catch (e) { toast(e.message, true); } };
window.delJ = async (id, sym) => { if (!confirm(`Delete the ${sym} journal entry? This can't be undone.`)) return; try { renderJournal(await api(`/journal/${id}`, null, "DELETE")); } catch (e) { toast(e.message, true); } };
$("#jr-add").onclick = (ev) => busy(ev.target, async () => {
  for (const f of ["#jr-symbol", "#jr-shares", "#jr-entry", "#jr-exit"]) if (!$(f).value.trim()) { $(f).focus(); throw new Error("Symbol, shares, entry and exit are required."); }
  renderJournal(await api("/journal", { symbol: $("#jr-symbol").value, shares: $("#jr-shares").value, entry: $("#jr-entry").value, exit: $("#jr-exit").value, stop: $("#jr-stop").value, setup: $("#jr-setup").value, followed_plan: $("#jr-followed").value, lessons: $("#jr-lessons").value }));
  toast("Added to journal");
});

// ---------------------------------------------------------------- tools
$("#ps-run").onclick = (ev) => busy(ev.target, async () => {
  if (!$("#ps-entry").value || (!$("#ps-stop").value && !$("#ps-atr").value)) { $("#ps-entry").focus(); throw new Error("Enter an entry price and either a stop or an ATR."); }
  const r = await api("/size", { account: $("#ps-account").value, entry: $("#ps-entry").value, stop: $("#ps-stop").value, atr: $("#ps-atr").value, risk_pct: $("#ps-risk").value, max_position_pct: $("#ps-max").value, use_kelly: $("#ps-kelly").checked });
  const c = r.calculations;
  $("#ps-result").innerHTML = `<div class="cards" style="margin-top:var(--sp-4)">${card("Shares", fmt(r.final_recommended_shares, 0))}${card("Position", money(r.final_position_value))}${card("Risk", money(r.final_risk_dollars), fmt(r.final_risk_pct) + "% of account")}</div><div class="scroll">` +
    table([{ h: "Method", k: "m" }, { h: "Shares", num: 1, k: "s" }, { h: "Risk", num: 1, k: "r" }], Object.entries(c).filter(([, v]) => v).map(([k, v]) => ({ m: k.replace(/_/g, " "), s: fmt(v.shares ?? v.recommended_shares, 0), r: money(v.dollar_risk ?? v.risk_dollars) }))) +
    `</div><p class="muted small">Binding constraint: ${esc(r.binding_constraint || "none")}</p>`;
});
loaders.tools = async () => {
  const s = await api("/settings");
  $("#st-risk").value = s.risk_pct; $("#st-max").value = s.max_position_pct; $("#st-universe").value = s.universe;
  $("#acct-email").textContent = s.email;
  $("#st-info").textContent = `Price cross-check limit: ${s.max_deviation_pct}% · FMP API: ${s.fmp_enabled ? "enabled" : "not configured"}`;
};
$("#st-save").onclick = (ev) => busy(ev.target, async () => { await api("/settings", { risk_pct: Number($("#st-risk").value), max_position_pct: Number($("#st-max").value), universe: $("#st-universe").value }); toast("Settings saved"); });

// ---------------------------------------------------------------- account
$("#pw-save").onclick = (ev) => busy(ev.target, async () => {
  if ($("#pw-new").value.length < 8) { $("#pw-new").focus(); throw new Error("New password must be at least 8 characters."); }
  await api("/auth/password", { current: $("#pw-current").value, new: $("#pw-new").value });
  $("#pw-current").value = $("#pw-new").value = ""; toast("Password changed. Other devices are signed out.");
});
$("#acct-delete").onclick = (ev) => busy(ev.target, async () => {
  if (!$("#del-pw").value) { $("#del-pw").focus(); throw new Error("Enter your password to delete the account."); }
  if (!confirm("Permanently delete your account and all its data? This can't be undone.")) return;
  await api("/auth/delete", { password: $("#del-pw").value });
  $("#del-pw").value = ""; showAuth(); toast("Account deleted");
});
$("#logout").onclick = async () => { try { await api("/auth/logout", {}); } catch (e) { /* ignore */ } showAuth(); };

// ---------------------------------------------------------------- sign in
let authMode = "login";
function setAuthMode(m) {
  authMode = m;
  $("#auth-login-tab").setAttribute("aria-selected", m === "login");
  $("#auth-signup-tab").setAttribute("aria-selected", m === "signup");
  $("#auth-submit").textContent = m === "login" ? "Sign in" : "Create account";
  $("#auth-pw").autocomplete = m === "login" ? "current-password" : "new-password";
  $("#auth-pw-hint").hidden = m === "login";
  $("#auth-error").textContent = "";
}
$("#auth-login-tab").onclick = () => setAuthMode("login");
$("#auth-signup-tab").onclick = () => setAuthMode("signup");
function showAuth() {
  document.body.classList.add("signed-out");
  $("#auth").hidden = false;
  Object.values(charts).forEach((c) => c.destroy());
  for (const k in charts) delete charts[k];
  ["#posture", "#breadth", "#uptrend", "#circuit", "#screen-results", "#bt-result", "#pt-result", "#paper-summary", "#pt-positions", "#pt-orders", "#pf-summary", "#pf-table", "#pf-warnings", "#jr-stats", "#jr-weekly", "#jr-table", "#ps-result"].forEach((id) => { const el = $(id); if (el) el.innerHTML = ""; });
  setTimeout(() => $("#auth-email").focus(), 0);
}
$("#auth-form").onsubmit = (ev) => {
  ev.preventDefault();
  busy($("#auth-submit"), async () => {
    const email = $("#auth-email").value.trim(), password = $("#auth-pw").value;
    $("#auth-error").textContent = "";
    if (!email) { $("#auth-email").focus(); $("#auth-error").textContent = "Enter your email."; return; }
    if (password.length < (authMode === "signup" ? 8 : 1)) { $("#auth-pw").focus(); $("#auth-error").textContent = authMode === "signup" ? "Password must be at least 8 characters." : "Enter your password."; return; }
    try { await api(`/auth/${authMode}`, { email, password }); } catch (e) { $("#auth-error").textContent = e.message; return; }
    $("#auth-pw").value = "";
    startApp();
  });
};
function startApp() {
  document.body.classList.remove("signed-out");
  $("#auth").hidden = true;
  api("/settings").then((s) => { const o = $("#fmp-opt"); if (!s.fmp_enabled && !o.disabled) { o.disabled = true; o.textContent += " (not configured)"; } }).catch(() => {});
  (document.querySelector(`[data-tab="${location.hash.slice(1)}"]`) || tabButtons[0]).click();
}

// ---------------------------------------------------------------- boot
api("/auth/me").then((m) => (m.email ? startApp() : showAuth())).catch(() => showAuth());
