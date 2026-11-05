// CBB model dashboard. Reads data.json (written each morning by cbb_model.py).
const WIN = 1, LOSS = -1.1;                      // -110 pricing: risk 1.1 to win 1
const TIERS = ["STRONG", "MEDIUM", "LEAN"];

// ---------- pure logic (also used by tests) ----------
function iso(d) { return `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`; }

function filterGames(games, f) {
  const t = (f.team || "").toLowerCase();
  return games.filter(g => {
    const d = iso(g.date);
    if (f.from && d < f.from) return false;
    if (f.to && d > f.to) return false;
    if (f.status === "Upcoming" && g.final) return false;
    if (f.status === "Final" && !g.final) return false;
    if (t && !(g.home.toLowerCase().includes(t) || g.away.toLowerCase().includes(t))) return false;
    if (f.conf && g.hc !== f.conf && g.ac !== f.conf) return false;
    if (f.flagged && !g.st && !g.tt) return false;
    if (f.tier && g.st !== f.tier && g.tt !== f.tier) return false;
    return true;
  });
}

function tierRecord(games) {
  const rows = [];
  for (const [kind, tk, rk] of [["Spread", "st", "sr"], ["Total", "tt", "tr"]]) {
    for (const tier of TIERS) {
      const r = games.filter(g => g[tk] === tier && g[rk]);
      const w = r.filter(g => g[rk] === "W").length, l = r.filter(g => g[rk] === "L").length;
      const p = r.length - w - l;
      rows.push({ kind, tier, w, l, p, pct: w + l ? 100 * w / (w + l) : null,
                  units: w * WIN + l * LOSS });
    }
  }
  return rows;
}

function accuracy(games, sameGames) {
  let f = games.filter(g => g.final && g.hp != null);
  if (sameGames) f = f.filter(g => g.ps != null && g.tvs != null && g.vs != null);
  return [["Our model", "ps", "pt"], ["Torvik", "tvs", "tvt"], ["Vegas", "vs", "vt"]].map(([label, s, t]) => {
    const sp = f.filter(g => g[s] != null).map(g => Math.abs(g.hp - g.ap + g[s]));
    const tt = f.filter(g => g[t] != null).map(g => Math.abs(g.hp + g.ap - g[t]));
    const avg = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
    return { label, n: sp.length, spread: avg(sp), total: avg(tt) };
  });
}

function unitsOverTime(games) {
  // cumulative units by date, one series per bet type + tier
  const dates = [...new Set(games.filter(g => g.sr || g.tr).map(g => g.date))].sort();
  const series = {};
  for (const [kind, tk, rk] of [["Spread", "st", "sr"], ["Total", "tt", "tr"]])
    for (const tier of TIERS) {
      let run = 0;
      series[`${kind} ${tier}`] = dates.map(d => {
        for (const g of games) if (g.date === d && g[tk] === tier && g[rk])
          run += g[rk] === "W" ? WIN : g[rk] === "L" ? LOSS : 0;
        return run;
      });
    }
  return { dates, series };
}

if (typeof module !== "undefined") module.exports = { filterGames, tierRecord, accuracy, unitsOverTime };

// ---------- page ----------
if (typeof document !== "undefined") (async function () {
  const $ = id => document.getElementById(id);
  const fmt = (x, sign) => x == null ? "" : (sign && x > 0 ? "+" : "") + x.toFixed(1);
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const tierTag = t => t ? `<span class="tier ${t}">${t}</span>` : "";
  let data, tab = "games", sortKey = null, sortDir = 1;

  try { data = await (await fetch("data.json", { cache: "no-store" })).json(); }
  catch (e) { $("updated").textContent = "Couldn't load data.json yet. It appears after the first morning run."; return; }

  $("updated").textContent = `Updated ${data.updated} · ${data.games.length} games this season`;
  const dates = data.games.map(g => iso(g.date)).sort();
  const last = dates[dates.length - 1], first = dates[0];
  const weekAgo = new Date(last); weekAgo.setDate(weekAgo.getDate() - 7);
  $("from").value = [weekAgo.toISOString().slice(0, 10), first].sort().reverse()[0];
  $("to").value = last;
  const confs = [...new Set(data.games.flatMap(g => [g.hc, g.ac]).filter(Boolean))].sort();
  $("conf").innerHTML += confs.map(c => `<option>${esc(c)}</option>`).join("");

  const filters = () => ({ from: $("from").value, to: $("to").value, status: $("status").value,
                           team: $("team").value, conf: $("conf").value, tier: $("tier").value,
                           flagged: $("flagged").checked });

  const phone = () => window.matchMedia("(max-width: 700px)").matches;

  function gameCards(rows) {
    if (!rows.length) return '<div class="muted">No games match these filters.</div>';
    const cell = x => `<div>${x}</div>`;
    return `<div class="cards">${rows.map(g => `<div class="card">
      <div class="top"><div class="teams">${esc(g.away)} ${g.n ? "vs" : "@"} ${esc(g.home)}</div>
        <div class="meta">${iso(g.date).slice(5)}${g.n ? " · N" : ""}${g.final ? ` · Final ${g.ap}-${g.hp}` : ""}</div></div>
      ${g.st || g.tt ? `<div class="flags">
        ${g.st ? `<span>${tierTag(g.st)} ${esc(g.sp)} <b class="${g.sr || ""}">${g.sr || ""}</b></span>` : ""}
        ${g.tt ? `<span>${tierTag(g.tt)} ${esc(g.tp)} <b class="${g.tr || ""}">${g.tr || ""}</b></span>` : ""}</div>` : ""}
      <div class="grid">
        <div class="h"></div><div class="h">Torvik</div><div class="h">Vegas</div><div class="h">Model</div>
        <div class="l">Spread</div>${cell(fmt(g.tvs, 1))}${cell(fmt(g.vs, 1))}${cell(fmt(g.ps, 1))}
        <div class="l">Total</div>${cell(fmt(g.tvt))}${cell(fmt(g.vt))}${cell(fmt(g.pt))}
        <div class="l">Score</div><div></div><div></div>${cell(g.ph == null ? "" : `${g.pa.toFixed(0)}-${g.ph.toFixed(0)}`)}
      </div></div>`).join("")}</div>`;
  }

  function gamesTab() {
    let rows = filterGames(data.games, filters());
    const keys = { date: g => g.date, game: g => g.away, ms: g => g.ps, ss: g => g.st || "", ts: g => g.tt || "" };
    rows = rows.slice().sort((a, b) => {
      if (sortKey) { const x = keys[sortKey](a), y = keys[sortKey](b); return (x > y ? 1 : x < y ? -1 : 0) * sortDir; }
      return b.date.localeCompare(a.date) || TIERS.indexOf(a.st) - TIERS.indexOf(b.st);
    });
    const f = filters();
    if (!rows.length && f.flagged) {
      const lined = data.games.some(g => g.vs != null || g.vt != null);
      return `<div class="note">No flagged games in this range.</div><div class="muted">${lined
        ? "Try widening the dates or changing the tier, or uncheck <b>Flagged games only</b>."
        : "Flags need a Vegas line to compare against. ESPN removes lines once a game is over, so flags only appear on games predicted before tip-off, starting with the next morning run. Uncheck <b>Flagged games only</b> to see every game."}</div>`;
    }
    if (phone()) return `<div class="note">${rows.length} games · spreads from the home team's view (negative = home favored) · scores away-home</div>` + gameCards(rows);
    const body = rows.map(g => `<tr>
      <td class="l">${iso(g.date).slice(5)}</td>
      <td class="l">${esc(g.away)} ${g.n ? "vs" : "@"} ${esc(g.home)}${g.n ? ' <span class="muted">(N)</span>' : ""}</td>
      <td>${g.ph == null ? "" : `${g.pa.toFixed(0)}-${g.ph.toFixed(0)}`}</td>
      <td class="grp">${fmt(g.tvs, 1)}</td><td>${fmt(g.vs, 1)}</td><td>${fmt(g.ps, 1)}</td>
      <td class="l">${tierTag(g.st)} ${esc(g.sp || "")}</td><td class="${g.sr || ""}">${g.sr || ""}</td>
      <td class="grp">${fmt(g.tvt)}</td><td>${fmt(g.vt)}</td><td>${fmt(g.pt)}</td>
      <td class="l">${tierTag(g.tt)} ${esc(g.tp || "")}</td><td class="${g.tr || ""}">${g.tr || ""}</td>
      <td class="grp">${g.final ? `${g.ap}-${g.hp}` : ""}</td></tr>`).join("");
    return `<div class="note">${rows.length} games. Spreads are from the home team's view (negative = home favored). Scores are away-home. Click a column header to sort.</div>
      <table><thead><tr>
        <th class="l" data-k="date">Date</th><th class="l" data-k="game">Game</th><th data-k="ms">Model</th>
        <th class="grp">Torvik sprd</th><th>Vegas sprd</th><th>Model sprd</th><th class="l" data-k="ss">Spread flag</th><th>ATS</th>
        <th class="grp">Torvik tot</th><th>Vegas tot</th><th>Model tot</th><th class="l" data-k="ts">Total flag</th><th>O/U</th>
        <th class="grp">Final</th></tr></thead><tbody>${body || '<tr><td colspan="14" class="l muted">No games match these filters.</td></tr>'}</tbody></table>`;
  }

  function chart({ dates, series }) {
    if (!dates.length) return '<div class="muted">No graded picks in this range yet.</div>';
    const W = phone() ? 380 : 760, H = phone() ? 200 : 220, P = 36, all = Object.values(series).flat().concat([0]);
    const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
    const x = i => P + (dates.length > 1 ? i * (W - 2 * P) / (dates.length - 1) : (W - 2 * P) / 2);
    const y = v => H - P + 10 - (v - lo) * (H - 2 * P) / span;
    const colors = { "Spread STRONG": "#1f5fae", "Spread MEDIUM": "#5d8fd0", "Spread LEAN": "#a9c4e8",
                     "Total STRONG": "#c2541a", "Total MEDIUM": "#e08a52", "Total LEAN": "#f2c29c" };
    const lines = Object.entries(series).filter(([, v]) => v.some(n => n !== 0)).map(([k, v]) =>
      `<polyline fill="none" stroke="${colors[k]}" stroke-width="2" points="${v.map((n, i) => `${x(i)},${y(n)}`).join(" ")}"/>`);
    const legend = Object.keys(series).filter(k => series[k].some(n => n !== 0))
      .map(k => `<span style="--c:${colors[k]}">${k}</span>`).join("");
    return `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px">
      <line x1="${P}" x2="${W - P}" y1="${y(0)}" y2="${y(0)}" stroke="currentColor" opacity=".25"/>
      <text x="${P - 6}" y="${y(hi) + 4}" text-anchor="end">${hi.toFixed(1)}</text>
      <text x="${P - 6}" y="${y(lo) + 4}" text-anchor="end">${lo.toFixed(1)}</text>
      <text x="${x(0)}" y="${H - 4}">${iso(dates[0]).slice(5)}</text>
      <text x="${x(dates.length - 1)}" y="${H - 4}" text-anchor="end">${iso(dates[dates.length - 1]).slice(5)}</text>
      ${lines.join("")}</svg><div class="legend">${legend}</div>`;
  }

  function perfTab() {
    const f = filters(); f.flagged = false; f.tier = ""; f.status = "Final";
    const games = filterGames(data.games, f);
    const rec = tierRecord(games).map(r => `<tr><td class="l">${r.kind}</td><td class="l">${tierTag(r.tier)}</td>
      <td>${r.w}-${r.l}${r.p ? `-${r.p}` : ""}</td><td>${r.pct == null ? "" : r.pct.toFixed(1) + "%"}</td>
      <td class="${r.units > 0 ? "W" : r.units < 0 ? "L" : ""}">${fmt(r.units, 1)}</td></tr>`).join("");
    const same = $("same") ? $("same").checked : true;
    const acc = accuracy(games, same).map(a => `<tr><td class="l">${a.label}</td><td>${a.n}</td>
      <td>${a.spread == null ? "" : a.spread.toFixed(2)}</td><td>${a.total == null ? "" : a.total.toFixed(2)}</td></tr>`).join("");
    return `<h2>Record by edge tier vs Vegas</h2>
      <div class="note">Uses the date, team and conference filters. Breakeven at -110 is 52.4%. Units assume risking 1.1 to win 1.</div>
      <table style="max-width:520px"><thead><tr><th class="l">Bet</th><th class="l">Tier</th><th>Record</th><th>Win %</th><th>Units</th></tr></thead><tbody>${rec}</tbody></table>
      <h2>Units over time</h2>${chart(unitsOverTime(games))}
      <h2>Prediction accuracy</h2>
      <div class="note">Average miss in points; lower is better.
        <label class="check" style="display:inline-flex;margin-left:10px"><input type="checkbox" id="same" ${same ? "checked" : ""}> Only games all three predicted</label></div>
      <table style="max-width:520px"><thead><tr><th class="l">Source</th><th>Games</th><th>Spread miss</th><th>Total miss</th></tr></thead><tbody>${acc}</tbody></table>`;
  }

  function teamsTab() {
    const t = ($("team").value || "").toLowerCase(), c = $("conf").value;
    const rows = data.teams.filter(r => (!t || r.name.toLowerCase().includes(t)) && (!c || r.conf === c))
      .map(r => ({ ...r, net: (r.off ?? 0) + (r.def ?? 0) })).sort((a, b) => b.net - a.net);
    return `<div class="note">Our ratings are points above/below an average team per game (higher is better for both). Uses the team and conference filters.</div>
      <table><thead><tr><th class="l">#</th><th class="l">Team</th><th class="l hide-sm">Conf</th><th>Our off</th><th>Our def</th><th>Our net</th><th class="hide-sm">Games</th>
      <th class="grp hide-sm">Torvik AdjO</th><th class="hide-sm">Torvik AdjD</th><th class="hide-sm">Torvik tempo</th></tr></thead><tbody>
      ${rows.map((r, i) => `<tr><td class="l muted">${i + 1}</td><td class="l">${esc(r.name)}</td><td class="l hide-sm">${esc(r.conf || "")}</td>
        <td>${fmt(r.off, 1)}</td><td>${fmt(r.def, 1)}</td><td>${fmt(r.net, 1)}</td><td class="hide-sm">${r.g}</td>
        <td class="grp hide-sm">${fmt(r.tvo)}</td><td class="hide-sm">${fmt(r.tvd)}</td><td class="hide-sm">${fmt(r.tvt)}</td></tr>`).join("")}</tbody></table>`;
  }

  function render() {
    $("panel").innerHTML = tab === "games" ? gamesTab() : tab === "perf" ? perfTab() : teamsTab();
    if ($("same")) $("same").onchange = render;
    document.querySelectorAll("th[data-k]").forEach(th => th.onclick = () => {
      sortDir = sortKey === th.dataset.k ? -sortDir : 1; sortKey = th.dataset.k; render();
    });
  }
  document.querySelectorAll(".filters input, .filters select").forEach(el => el.addEventListener("input", render));
  document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
    document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("on", x === b));
    tab = b.dataset.tab; render();
  });
  $("ftoggle").onclick = () => {
    const open = $("filterbox").classList.toggle("open");
    $("ftoggle").setAttribute("aria-expanded", open);
    $("ftoggle").textContent = open ? "Hide filters ▴" : "Filters ▾";
  };
  window.matchMedia("(max-width: 700px)").addEventListener("change", render);   // rotate phone
  render();
})();
