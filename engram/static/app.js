/* Engram inspector: hash-routed modules over the hub + device APIs. */
const HUB = "";
let DEVICES = {};
const states = {};
let hubState = null, catalog = {}, bench = null, mode = "trusted";
const ui = { robot: { tab: "memories", trusted: true, search: null, lastSync: "" },
             kiosk: { tab: "memories", trusted: true, search: null, lastSync: "" } };
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const MODULES = [
  { id: "floor", title: "Floor", badge: () => null },
  { id: "pipeline", title: "Sync pipeline", badge: () => null },
  { id: "devices", title: "Devices", badge: () => null },
  { id: "attacks", title: "Attack lab", badge: () => {
      const n = hubState ? new Set(hubState.attacks.map(a => a.mode)).size : 0;
      return n ? { text: `${stoppedCount()}/${n}`, ok: stoppedCount() === n } : null; } },
  { id: "conflicts", title: "Conflicts", badge: () => {
      const n = Object.values(states).reduce((s, x) => s + (x ? x.conflicts.length : 0), 0);
      return n ? { text: n, ok: true } : null; } },
  { id: "benchmark", title: "Benchmark", badge: () => null },
  { id: "hub", title: "Hub", badge: () => null },
];

const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pill = (cls, text) => `<span class="pill ${cls}">${esc(text)}</span>`;
const clock = ms => new Date(ms).toLocaleTimeString();
const statusPill = s => pill(s === "verified" ? "verified" : s === "superseded" ? "superseded" : "quarantined", s);

async function api(base, path, opts = {}) {
  const r = await fetch(base + path, opts);
  if (!r.ok) throw new Error(`${path} returned HTTP ${r.status}`);
  return r.json();
}
const post = (base, path) => api(base, path, { method: "POST" });

// ================================================================ routing
function current() { const id = location.hash.replace("#/", ""); return MODULES.some(m => m.id === id) ? id : "floor"; }
function go(id) { if (current() !== id) location.hash = `#/${id}`; else renderActive(); }
function renderNav() {
  const cur = current();
  document.getElementById("nav").innerHTML = MODULES.map((m, i) => {
    const b = m.badge();
    return `<a href="#/${m.id}" ${cur === m.id ? 'aria-current="page"' : ""}><span class="n">${String(i + 1).padStart(2, "0")}</span>
      <span>${m.title}</span>${b ? `<span class="badge ${b.ok ? "ok" : ""}">${esc(b.text)}</span>` : "<span></span>"}</a>`;
  }).join("");
}
function showView() {
  const cur = current();
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === `view-${cur}`));
  renderNav(); renderActive();
}
window.addEventListener("hashchange", () => {
  if (document.startViewTransition && !reduceMotion) {
    const vt = document.startViewTransition(showView);
    [vt.ready, vt.finished, vt.updateCallbackDone].forEach(p => p.catch(() => {})); // skipped when navigating quickly; harmless
  } else showView();
  window.scrollTo(0, 0);
});
function renderTitleblocks() {
  document.querySelectorAll(".titleblock").forEach(tb => {
    const i = +tb.dataset.sheet;
    tb.innerHTML = `<span>Sheet</span><span>${i} of ${MODULES.length}</span><span>Module</span><span>${esc(MODULES[i - 1].title)}</span>
      <span>Project</span><span>Engram, Qdrant Edge</span>`;
  });
}
function renderRail() {
  const bits = [["hub", !!hubState], ...Object.keys(DEVICES).map(id => [id, !!states[id] && !states[id].offline])];
  document.getElementById("rail-status").innerHTML = bits.map(([n, ok]) =>
    `<span><i class="dot ${ok ? "on" : "off"}"></i>${esc(n)} ${ok ? "online" : "offline"}</span>`).join("")
    + `<span class="mock" style="margin-top:6px">Seed memories are mock data</span>`;
}
function renderActive() {
  const cur = current();
  if (cur === "floor") renderFloor();
  if (cur === "pipeline") { drawPipeline(); renderQueues(); }
  if (cur === "devices") Object.keys(DEVICES).forEach(renderDevice);
  if (cur === "attacks") renderMatrix();
  if (cur === "conflicts") Object.keys(DEVICES).forEach(renderConflicts);
  if (cur === "benchmark") renderBench();
  if (cur === "hub") renderHub();
}

// ================================================================ floor map
const MAP = {
  W: 1000, H: 640, outside: { x: 30, y: 26 }, hub: { x: 410, y: 14, w: 190, h: 70 },
  robot: { x: 640, y: 400 }, kiosk: { x: 150, y: 560 },
  docks: { dock1: { x: 770, y: 160, label: "Dock 1" }, dock2: { x: 770, y: 305, label: "Dock 2" }, dock3: { x: 770, y: 450, label: "Dock 3" } },
};
const DOCK_ORDER = ["dock3", "dock2", "dock1"]; // nearest to the robot first
const usable = v => /^(ok|safe)/i.test(v || "") || /safe to ignore/i.test(v || "");

function beliefs() {
  const s = states.robot;
  const out = {};
  for (const dock of Object.keys(MAP.docks)) {
    if (!s) { out[dock] = null; continue; }
    let cands = s.memories.filter(m => m.subject === dock).map(m => ({ ...m, origin: m.trust_status }));
    if (mode === "trusted") cands = cands.filter(m => m.trust_status === "verified");
    else cands = cands.concat(s.quarantine.filter(q => q.payload && q.payload.subject === dock)
      .map(q => ({ ...q.payload, origin: "quarantined", reasons: q.reasons })));
    cands.sort((a, b) => b.ts - a.ts);
    out[dock] = cands[0] || null;
  }
  return out;
}

function mapPaths() {
  const h = MAP.hub, hx = h.x + h.w / 2, hy = h.y + h.h;
  return {
    robot: `M${MAP.robot.x},${MAP.robot.y - 22} C${MAP.robot.x},250 ${hx + 120},170 ${hx + 30},${hy}`,
    kiosk: `M${MAP.kiosk.x},${MAP.kiosk.y - 22} C${MAP.kiosk.x},250 ${hx - 150},170 ${hx - 30},${hy}`,
    outside: `M${MAP.outside.x},${MAP.outside.y + 10} C200,30 330,30 ${h.x},${h.y + h.h / 2}`,
  };
}

function initFloor() {
  const el = document.getElementById("floor-map");
  if (el.querySelector("svg")) return;
  const svg = d3.select(el).append("svg").attr("viewBox", `0 0 ${MAP.W} ${MAP.H}`).attr("role", "img")
    .attr("aria-label", "Warehouse floor plan with docks, robot, kiosk and the hub");
  const defs = svg.append("defs");
  const hz = defs.append("pattern").attr("id", "hz").attr("width", 14).attr("height", 14).attr("patternUnits", "userSpaceOnUse")
    .attr("patternTransform", "rotate(45)");
  hz.append("rect").attr("width", 14).attr("height", 14).attr("fill", "#ff6a5c");
  hz.append("rect").attr("width", 7).attr("height", 14).attr("fill", "#20060a");
  defs.append("marker").attr("id", "arrow").attr("viewBox", "0 0 10 10").attr("refX", 8).attr("refY", 5)
    .attr("markerWidth", 7).attr("markerHeight", 7).attr("orient", "auto-start-reverse")
    .append("path").attr("d", "M0,0 L10,5 L0,10 z").attr("fill", "currentColor");

  const line = "rgba(190,216,255,.6)", faint = "rgba(190,216,255,.22)";
  // building
  svg.append("rect").attr("x", 60).attr("y", 120).attr("width", 900).attr("height", 500).attr("fill", "rgba(6,22,44,.35)")
    .attr("stroke", line).attr("stroke-width", 2);
  svg.append("text").attr("class", "map-label").attr("x", 74).attr("y", 146).text("Warehouse floor (mock layout)");
  // racks
  for (let i = 0; i < 4; i++) {
    const y = 180 + i * 92;
    svg.append("rect").attr("x", 130).attr("y", y).attr("width", 440).attr("height", 34).attr("fill", "none")
      .attr("stroke", faint).attr("stroke-dasharray", "4 4");
    for (let k = 1; k < 8; k++) svg.append("line").attr("x1", 130 + k * 55).attr("x2", 130 + k * 55).attr("y1", y).attr("y2", y + 34).attr("stroke", faint);
    svg.append("text").attr("class", "map-small").attr("x", 134).attr("y", y + 54).text(`Rack ${String.fromCharCode(65 + i)}`);
  }
  // docks
  const docks = svg.append("g").attr("class", "docks");
  Object.entries(MAP.docks).forEach(([id, d]) => {
    const g = docks.append("g").attr("id", `dock-${id}`);
    g.append("rect").attr("class", "dock-rect").attr("x", d.x).attr("y", d.y).attr("width", 176).attr("height", 112)
      .attr("stroke", line).attr("stroke-width", 1.5).attr("fill", "rgba(6,22,44,.5)");
    g.append("rect").attr("class", "dock-hz").attr("x", d.x).attr("y", d.y + 98).attr("width", 176).attr("height", 14).attr("fill", "url(#hz)").attr("opacity", 0);
    g.append("text").attr("class", "map-strong").attr("x", d.x + 12).attr("y", d.y + 30).text(d.label);
    g.append("text").attr("class", "map-label dock-val").attr("x", d.x + 12).attr("y", d.y + 64);
    g.append("text").attr("class", "map-small dock-src").attr("x", d.x + 12).attr("y", d.y + 90);
  });
  // hub + uplinks
  const P = mapPaths();
  const links = svg.append("g").attr("class", "links");
  ["robot", "kiosk"].forEach(id => links.append("path").attr("id", `link-${id}`).attr("d", P[id]).attr("fill", "none")
    .attr("stroke", "rgba(143,203,255,.45)").attr("stroke-width", 1.5).attr("stroke-dasharray", "6 6"));
  links.append("path").attr("id", "link-outside").attr("d", P.outside).attr("fill", "none")
    .attr("stroke", "rgba(255,106,92,.35)").attr("stroke-width", 1.5).attr("stroke-dasharray", "2 6");
  svg.append("text").attr("class", "map-label").attr("x", MAP.outside.x).attr("y", MAP.outside.y + 4).style("fill", "#ff6a5c").text("attacker");
  const hub = svg.append("g");
  hub.append("rect").attr("x", MAP.hub.x).attr("y", MAP.hub.y).attr("width", MAP.hub.w).attr("height", MAP.hub.h).attr("rx", 35)
    .attr("fill", "#11315a").attr("stroke", line);
  hub.append("text").attr("class", "map-strong").attr("x", MAP.hub.x + MAP.hub.w / 2).attr("y", MAP.hub.y + 30).attr("text-anchor", "middle").text("Hub");
  hub.append("text").attr("class", "map-small hub-count").attr("x", MAP.hub.x + MAP.hub.w / 2).attr("y", MAP.hub.y + 54)
    .attr("text-anchor", "middle");
  // route + kiosk + robot
  svg.append("path").attr("class", "route").attr("fill", "none").attr("stroke-width", 3).attr("stroke-dasharray", "9 7")
    .attr("marker-end", "url(#arrow)");
  svg.append("circle").attr("cx", MAP.robot.x).attr("cy", MAP.robot.y).attr("r", 22).attr("fill", "none")
    .attr("stroke", faint).attr("stroke-dasharray", "3 4");
  svg.append("text").attr("class", "map-small").attr("x", MAP.robot.x - 20).attr("y", MAP.robot.y + 44).text("home");
  const k = svg.append("g").attr("transform", `translate(${MAP.kiosk.x},${MAP.kiosk.y})`);
  k.append("rect").attr("x", -26).attr("y", -20).attr("width", 52).attr("height", 40).attr("rx", 4).attr("fill", "#eef4fc");
  k.append("rect").attr("x", -18).attr("y", -13).attr("width", 36).attr("height", 18).attr("fill", "#11315a");
  k.append("text").attr("class", "map-strong").attr("x", 36).attr("y", 6).text("kiosk");
  const r = svg.append("g").attr("class", "robot-g").attr("id", "robot-g")
    .style("transform", `translate(${MAP.robot.x}px, ${MAP.robot.y}px)`);
  r.append("rect").attr("x", -18).attr("y", -18).attr("width", 36).attr("height", 36).attr("rx", 8).attr("fill", "#eef4fc");
  r.append("circle").attr("cx", -7).attr("cy", -3).attr("r", 3.5).attr("fill", "#0e2a4d");
  r.append("circle").attr("cx", 7).attr("cy", -3).attr("r", 3.5).attr("fill", "#0e2a4d");
  r.append("rect").attr("x", -8).attr("y", 7).attr("width", 16).attr("height", 3).attr("fill", "#0e2a4d");
  r.append("text").attr("class", "map-strong").attr("x", 0).attr("y", 44).attr("text-anchor", "middle").text("robot");
  svg.append("g").attr("class", "packets");
}

function renderFloor() {
  initFloor();
  const svg = d3.select("#floor-map svg");
  const b = beliefs();
  svg.select(".hub-count").text(hubState ? `${hubState.log_size} memories, unscreened` : "unreachable");
  Object.entries(MAP.docks).forEach(([id]) => {
    const m = b[id], g = svg.select(`#dock-${id}`);
    const val = m ? m.value : "no memory";
    const poisoned = m && m.origin === "quarantined";
    const broken = m && !usable(m.value);
    g.select(".dock-hz").attr("opacity", broken ? 1 : 0);
    g.select(".dock-rect").attr("fill", broken ? "rgba(255,106,92,.2)" : poisoned ? "rgba(255,106,92,.28)" : m ? "rgba(78,224,160,.18)" : "rgba(6,22,44,.5)")
      .attr("stroke", poisoned ? "#ff6a5c" : broken ? "#ff6a5c" : m ? "#4ee0a0" : "rgba(190,216,255,.6)");
    g.select(".dock-val").text(val).style("fill", broken ? "#ff6a5c" : null).style("font-weight", 700);
    g.select(".dock-src").text(m ? (poisoned ? `from ${m.source_device}` : `from ${(m.corroborated_by || [m.source_device]).join(" + ")}`) : "")
      .style("fill", poisoned ? "#ff6a5c" : null);
  });
  // route: nearest dock the robot believes is usable
  const target = DOCK_ORDER.find(id => b[id] && usable(b[id].value));
  const route = svg.select(".route");
  const robot = document.getElementById("robot-g");
  if (target) {
    const d = MAP.docks[target], cy = d.y + 56, poisoned = b[target].origin === "quarantined";
    route.attr("d", `M${MAP.robot.x + 22},${MAP.robot.y} H715 V${cy} H${d.x - 62}`)
      .attr("stroke", poisoned ? "#ff6a5c" : "#4ee0a0").style("color", poisoned ? "#ff6a5c" : "#4ee0a0").attr("opacity", 1);
    robot.style.transform = `translate(${d.x - 42}px, ${cy}px)`;
  } else {
    route.attr("opacity", 0);
    robot.style.transform = `translate(${MAP.robot.x}px, ${MAP.robot.y}px)`;
  }
  // side panel
  document.getElementById("beliefs").innerHTML = Object.entries(MAP.docks).map(([id, d]) => {
    const m = b[id];
    if (!m) return `<li><b>${d.label}</b><span class="muted">No memory yet</span></li>`;
    const tag = m.origin === "quarantined" ? pill("quarantined", "unverified claim") : statusPill(m.trust_status);
    return `<li><b>${d.label}</b><span>${esc(m.value)} ${tag}<br><span class="small muted">${esc(m.text)}</span></span></li>`;
  }).join("");
  const v = document.getElementById("verdict");
  if (!states.robot) { v.innerHTML = `<div class="calm">The robot is not reachable. Start everything with <code>python launch.py</code>.</div>`; return; }
  const t = target ? MAP.docks[target].label : null;
  if (mode === "all" && target && b[target].origin === "quarantined") {
    v.innerHTML = `<div class="alarm"><b>The robot would dock at a sparking charger.</b> Believing everything synced, it takes the
      claim from <b>${esc(b[target].source_device)}</b> that ${esc(t)} is "${esc(b[target].value)}". The screen had quarantined it:
      ${b[target].reasons.map(r => `<code>${esc(r)}</code>`).join(" ")}</div>`;
  } else if (mode === "all") {
    v.innerHTML = `<div class="calm">Nothing poisoned has reached the robot yet, so both views agree. Launch attacks in the
      <a href="#/attacks">Attack lab</a> and sync the robot.</div>`;
  } else {
    v.innerHTML = `<div class="calm">${t ? `Heading to <b>${esc(t)}</b>, the nearest dock its trusted memory says is working.` :
      "No working dock in trusted memory yet. Run step 1 of the tour."} Quarantined claims never reach this view.</div>`;
  }
}
function setMode(m) {
  mode = m;
  document.getElementById("mode-trusted").setAttribute("aria-pressed", m === "trusted");
  document.getElementById("mode-all").setAttribute("aria-pressed", m === "all");
  renderFloor();
}

function packets(pathId, specs, reverse = false) {
  if (reduceMotion || current() !== "floor" || !specs.length) return;
  const path = document.getElementById(pathId);
  if (!path) return;
  const L = path.getTotalLength(), layer = d3.select("#floor-map .packets");
  specs.slice(0, 14).forEach((s, i) => {
    const c = layer.append("circle").attr("r", 6).attr("fill", s.color).attr("opacity", 0);
    const stopAt = s.bounce ? 0.86 : 1;
    c.transition().delay(i * 130).duration(1100).ease(d3.easeCubicInOut).attr("opacity", 1)
      .attrTween("transform", () => t => {
        const f = reverse ? 1 - t * stopAt : t * stopAt, p = path.getPointAtLength(f * L);
        return `translate(${p.x},${p.y})`;
      })
      .transition().duration(s.bounce ? 380 : 250).attr("r", s.bounce ? 16 : 3).attr("opacity", 0).remove();
  });
}
function animateSync(id, r) {
  if (!r || !r.ok) return;
  const push = Array.from({ length: r.pushed || 0 }, () => ({ color: "#8fcbff" }));
  packets(`link-${id}`, push);
  const p = r.pull || {};
  const pull = [].concat(
    Array.from({ length: p.promoted || 0 }, () => ({ color: "#4ee0a0" })),
    Array.from({ length: p.corroborated || 0 }, () => ({ color: "#4ee0a0" })),
    Array.from({ length: p.conflicts || 0 }, () => ({ color: "#ffc857" })),
    Array.from({ length: p.quarantined || 0 }, () => ({ color: "#ff6a5c", bounce: true })));
  setTimeout(() => packets(`link-${id}`, pull, true), push.length ? 900 : 0);
}

// ================================================================ pipeline
const OUTCOMES = [["promoted", "Promoted", "#4ee0a0"], ["corroborated", "Corroborated", "#2fb07a"],
                  ["conflicts", "Conflict resolved", "#ffc857"], ["quarantined", "Quarantined", "#ff6a5c"]];
function drawPipeline() {
  const el = document.getElementById("pipeline");
  const W = 1000, laneH = 132, top = 48, H = top + laneH * 2 + 4;
  let svg = d3.select(el).select("svg");
  if (svg.empty()) {
    svg = d3.select(el).append("svg").attr("viewBox", `0 0 ${W} ${H}`).attr("role", "img")
      .attr("aria-label", "From hub through staging and the poison screen to trusted shard or quarantine");
    const p = svg.append("defs").append("pattern").attr("id", "hazard").attr("width", 12).attr("height", 12)
      .attr("patternUnits", "userSpaceOnUse").attr("patternTransform", "rotate(45)");
    p.append("rect").attr("width", 12).attr("height", 12).attr("fill", "#20060a");
    p.append("rect").attr("width", 6).attr("height", 12).attr("fill", "#ff6a5c");
    const hdr = svg.append("g").attr("class", "map-label");
    [["Hub log", 20], ["Staging shard", 250], ["Poison screen", 470], ["Outcome", 720]].forEach(([t, x]) =>
      hdr.append("text").attr("x", x).attr("y", 18).text(t).style("font-weight", 700).style("fill", "#eef4fc"));
    hdr.append("text").attr("x", 470).attr("y", 34).text("signature, pinned key, trust, corroboration, semantic").style("font-size", "11.5px");
    svg.append("g").attr("class", "lanes");
  }
  const lanes = Object.keys(DEVICES).map((id, i) => ({ id, i, flow: (states[id] && states[id].flow) || {} }));
  const t = svg.transition().duration(reduceMotion ? 0 : 900).ease(d3.easeCubicOut);
  const g = svg.select("g.lanes").selectAll("g.lane").data(lanes, d => d.id).join(enter => {
    const e = enter.append("g").attr("class", "lane");
    const y = d => top + d.i * laneH;
    e.append("text").attr("class", "map-strong").attr("x", 20).attr("y", d => y(d) + 16).text(d => d.id);
    e.append("rect").attr("x", 20).attr("y", d => y(d) + 26).attr("width", 150).attr("height", 64).attr("fill", "rgba(143,203,255,.14)").attr("stroke", "rgba(143,203,255,.5)");
    e.append("text").attr("class", "map-strong hubtext").attr("x", 34).attr("y", d => y(d) + 55).style("font-size", "14px");
    e.append("text").attr("class", "map-label").attr("x", 34).attr("y", d => y(d) + 76).text("append-only, untrusted").style("font-size", "12px");
    e.append("rect").attr("x", 250).attr("y", d => y(d) + 26).attr("width", 150).attr("height", 64).attr("fill", "rgba(6,22,44,.5)").attr("stroke", "rgba(190,216,255,.6)");
    e.append("text").attr("class", "map-strong stagetext").attr("x", 264).attr("y", d => y(d) + 55).style("font-size", "14px");
    e.append("text").attr("class", "map-label").attr("x", 264).attr("y", d => y(d) + 76).text("embedded once").style("font-size", "12px");
    e.append("rect").attr("x", 470).attr("y", d => y(d) + 8).attr("width", 12).attr("height", 100).attr("fill", "#eef4fc");
    ["M170", "M400"].forEach((m, k) => e.append("path").attr("fill", "none").attr("stroke", "rgba(143,203,255,.45)").attr("stroke-width", 10)
      .attr("d", d => { const yy = y(d) + 58; return k ? `M400,${yy} L470,${yy}` : `M170,${yy} L250,${yy}`; }));
    e.append("g").attr("class", "outs");
    e.append("text").attr("class", "map-label empty").attr("x", 720).attr("y", d => y(d) + 62);
    return e;
  });
  g.select(".hubtext").text(`${hubState ? hubState.log_size : 0} memories`);
  g.select(".stagetext").text(d => `${d.flow.received || 0} pulled`);
  g.select(".empty").text(d => (d.flow.received ? "" : "Nothing pulled yet. Sync this device."));
  g.each(function (d) {
    const total = OUTCOMES.reduce((s, [k]) => s + (d.flow[k] || 0), 0);
    const y0 = top + d.i * laneH + 6, gap = 5, usableH = laneH - 22 - gap * 3;
    const shown = OUTCOMES.map(([k, label, color]) => ({ k, label, color, n: d.flow[k] || 0 }));
    shown.forEach(o => { o.h = total ? Math.max(o.n ? 8 : 2, usableH * o.n / total) : usableH / 4; });
    const sc = usableH / d3.sum(shown, o => o.h);
    let yy = y0; shown.forEach(o => { o.h *= sc; o.y = yy; yy += o.h + gap; });
    const gy = top + d.i * laneH + 58;
    const outs = d3.select(this).select("g.outs").selectAll("g.out").data(shown, o => o.k).join(enter => {
      const e = enter.append("g").attr("class", "out");
      e.append("path").attr("class", "ribbon").attr("fill", o => o.color).attr("opacity", 0);
      e.append("rect").attr("class", "seg").attr("x", 720).attr("width", 14).attr("fill", o => o.k === "quarantined" ? "url(#hazard)" : o.color);
      e.append("text").attr("class", "map-label lbl").attr("x", 744).style("font-size", "13px");
      return e;
    });
    outs.select(".seg").transition(t).attr("y", o => o.y).attr("height", o => Math.max(o.h, 1)).attr("opacity", total ? 1 : .25);
    outs.select(".ribbon").transition(t).attr("opacity", o => (o.n ? .32 : 0)).attr("d", o => {
      const h = Math.max(2, Math.min(40, o.h)), yc = o.y + o.h / 2;
      return `M482,${gy - h / 2} C600,${gy - h / 2} 600,${yc - h / 2} 720,${yc - h / 2} L720,${yc + h / 2} C600,${yc + h / 2} 600,${gy + h / 2} 482,${gy + h / 2} Z`;
    });
    outs.select(".lbl").transition(t).attr("y", o => o.y + o.h / 2 + 4).text(o => (total ? `${o.label}  ${o.n}` : o.label))
      .style("font-weight", o => (o.n ? 700 : 400)).style("fill", o => (o.n ? o.color : null));
  });
}
function renderQueues() {
  for (const id of Object.keys(DEVICES)) {
    const s = states[id], el = document.getElementById(`queue-${id}`);
    if (!s) { el.innerHTML = `<p class="muted">Not reachable.</p>`; continue; }
    const rows = s.sync_queue.map(m => `<tr><td>${esc(m.subject)}</td><td>${pill("info", "will push")}</td><td class="muted">${esc(m.route_reason)}</td></tr>`)
      .concat(s.held_back.map(m => `<tr><td>${esc(m.subject)}</td><td>${pill("superseded", "stays here")}</td><td class="muted">${esc(m.route_reason)}</td></tr>`));
    el.innerHTML = `<p class="small muted">Selected by the filter <code>source_device = ${esc(id)} AND sensitivity = SYNC AND ts &gt; last push</code>.
      Private memories cannot match it.</p>
      <div class="scroll-x"><table class="data"><tr><th>Subject</th><th>Route</th><th>Reason</th></tr>
      ${rows.join("") || `<tr><td colspan="3" class="muted">Nothing waiting to push.</td></tr>`}</table></div>
      <div class="row" style="margin-top:10px"><button onclick="sync('${id}')" ${s.offline ? "disabled" : ""}>Sync ${esc(id)}</button>
      <span class="small muted">${esc(ui[id].lastSync)}</span></div>`;
  }
}

// ================================================================ devices
function renderDevice(id) {
  const s = states[id], u = ui[id], el = document.getElementById(`dev-${id}`);
  if (!s) { el.innerHTML = `<h2>${esc(id)}</h2><p class="muted">Not reachable. Start everything with <code>python launch.py</code>.</p>`; return; }
  const a = document.activeElement;
  if (a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName) && el.contains(a)) return;
  const st = s.stats;
  const tabs = [["memories", "Memories"], ["quarantine", `Quarantine${s.quarantine.length ? ` (${s.quarantine.length})` : ""}`],
                ["write", "Add memory"], ["log", "Activity"]];
  el.innerHTML = `
    <div class="row" style="justify-content:space-between"><h2 style="margin:0;text-transform:capitalize">${esc(id)}</h2>
      <span>${s.offline ? pill("quarantined", "offline") : pill("verified", "online")} <code title="Ed25519 public key">${esc(s.public_key.slice(0, 10))}</code></span></div>
    <div class="row" style="margin-top:10px">
      <button onclick="toggleOffline('${id}', ${!s.offline})">${s.offline ? "Go online" : "Go offline"}</button>
      <button class="solid" onclick="sync('${id}')" ${s.offline ? "disabled" : ""}>Sync with hub</button>
    </div>
    <div class="counts"><span><b>${st.verified}</b>verified</span><span><b>${st.superseded}</b>superseded</span>
      <span><b>${s.quarantine.length}</b>quarantined</span><span><b>${st.private}</b>private</span><span><b>${st.from_peers}</b>from peers</span></div>
    <form class="row" onsubmit="search(event, '${id}')">
      <input class="grow" name="q" aria-label="Search ${esc(id)}" placeholder="Search, e.g. dock 3 charger" value="${esc(u.search ? u.search.q : "")}">
      <label class="check"><input type="checkbox" name="trusted" ${u.trusted ? "checked" : ""}> Trust filter</label>
      <button>Search</button>
    </form>
    ${renderSearch(u.search)}
    <div class="tabs" role="tablist">${tabs.map(([k, label]) =>
      `<button role="tab" aria-selected="${u.tab === k}" onclick="setTab('${id}', '${k}')">${label}</button>`).join("")}</div>
    <div class="tabpane" role="tabpanel">${renderTab(id, s, u.tab)}</div>`;
}
function renderSearch(r) {
  if (!r) return "";
  if (r.error) return `<p class="small" style="color:var(--bad)">${esc(r.error)}</p>`;
  const total = r.embed_ms + r.search_ms || 1;
  return `<p class="small muted" style="margin:10px 0 0">${r.results.length} results. Query embedding <b>${r.embed_ms} ms</b>, vector search
      <b>${r.search_ms} ms</b>. Filter: ${r.filter ? "<code>trust_status = verified</code>" : "<b style='color:var(--bad)'>none, unsafe view</b>"}</p>
    <div class="timing" aria-hidden="true"><span style="width:${100 * r.embed_ms / total}%;background:var(--ink-3)"></span><span style="width:${100 * r.search_ms / total}%;background:var(--info)"></span></div>
    <div class="scroll-x"><table class="data"><tr><th>RRF</th><th>Subject</th><th>Value</th><th>Status</th><th>Text</th></tr>
    ${r.results.map(h => `<tr><td>${h.score.toFixed(3)}</td><td>${esc(h.payload.subject)}</td><td>${esc(h.payload.value)}</td>
      <td>${statusPill(h.payload.trust_status)}</td><td class="muted">${esc(h.payload.text)}</td></tr>`).join("")}</table></div>`;
}
const PROV = ["unregistered_device", "registry_key_mismatch", "invalid_or_missing_signature", "private_memory_on_the_wire", "malformed_payload"];
function renderTab(id, s, tab) {
  if (tab === "memories") {
    return `<div class="scroll-x"><table class="data"><tr><th>Subject</th><th>Value</th><th>Status</th><th>Route</th><th>From</th><th>Agreed by</th><th>Signature</th></tr>
      ${s.memories.map(m => `<tr><td>${esc(m.subject)}</td><td>${esc(m.value)}</td><td>${statusPill(m.trust_status)}</td>
        <td>${m.sensitivity === "PRIVATE" ? pill("superseded", "private") : pill("info", "sync")}</td><td>${esc(m.source_device)}</td>
        <td>${esc((m.corroborated_by || []).join(", "))}</td><td>${m.sig_valid ? pill("verified", "valid") : pill("quarantined", "invalid")}</td></tr>`).join("")}</table></div>`;
  }
  if (tab === "quarantine") {
    if (!s.quarantine.length) return `<p class="muted">Nothing quarantined. Launch an attack in the <a href="#/attacks">Attack lab</a>, then sync.</p>`;
    return s.quarantine.map(q => {
      const p = q.payload || {}, blocked = q.reasons.some(r => PROV.includes(r));
      return `<div class="card q"><div class="row"><span class="grow"><b>${esc(p.subject)}</b> = "${esc(p.value)}", claimed source <b>${esc(p.source_device)}</b></span>
        <button data-qid="${esc(q.id)}" onclick="release('${id}', this.dataset.qid)" ${blocked ? "disabled title='Unproven authorship can never be released'" : ""}>Release</button></div>
        <div class="row" style="margin-top:6px">${q.reasons.map(r => pill(PROV.includes(r) ? "quarantined" : "superseded", r)).join("")}</div>
        ${q.evidence.length ? `<details style="margin-top:6px"><summary class="small muted">Evidence (${q.evidence.length})</summary>
          <div class="small">${q.evidence.map(e => `<div><code>${esc(JSON.stringify(e))}</code></div>`).join("")}</div></details>` : ""}</div>`;
    }).join("");
  }
  if (tab === "write") {
    return `<form class="row" onsubmit="write(event, '${id}')">
        <input name="subject" aria-label="Subject" placeholder="Subject, e.g. dock3" required>
        <input name="value" aria-label="Value" placeholder="Value" required>
        <input class="grow" name="text" aria-label="Memory text" placeholder="What the device observed" required>
        <select name="sensitivity" aria-label="Sensitivity"><option value="SYNC">Share on sync</option><option value="PRIVATE">Keep on device</option></select>
        <button class="solid">Sign and save</button></form>
      <p class="small muted">Text with an email, phone number, ID number, card number or credential stays on the device automatically.</p>`;
  }
  return s.log.map(e => { const { ts, event, ...rest } = e;
    return `<div class="small"><span class="muted">${clock(ts)}</span> <b>${esc(event)}</b> <span class="muted">${esc(JSON.stringify(rest))}</span></div>`; }).join("");
}

// ================================================================ attack lab
const CHECKS = [
  ["Signature and registration", r => ["unregistered_device", "invalid_or_missing_signature", "malformed_payload", "private_memory_on_the_wire"].includes(r)],
  ["Pinned key", r => r === "registry_key_mismatch"],
  ["Trust score", r => r.startsWith("low_trust_source")],
  ["Corroboration", r => r === "contradicts_corroborated_memory"],
  ["Semantic neighbors", r => r === "semantic_conflict_with_corroborated_memory"],
];
function attackOutcome(mode) {
  const last = hubState ? [...hubState.attacks].reverse().find(x => x.mode === mode) : null;
  const o = { last, reasons: [], where: [], leaked: [] };
  if (last) for (const [dev, s] of Object.entries(states)) {
    if (!s) continue;
    const q = s.quarantine.find(q => q.id === last.id);
    if (q) { o.reasons = o.reasons.concat(q.reasons); o.where.push(dev); }
    if (s.memories.some(m => m.id === last.id && m.trust_status === "verified")) o.leaked.push(dev);
  }
  return o;
}
function stoppedCount() { return Object.keys(catalog).filter(m => { const o = attackOutcome(m); return o.last && o.where.length && !o.leaked.length; }).length; }
function renderMatrix() {
  const el = document.getElementById("matrix");
  if (!Object.keys(catalog).length) { el.innerHTML = `<p class="muted">The hub is not reachable.</p>`; return; }
  const rows = Object.entries(catalog).map(([m, a]) => {
    const o = attackOutcome(m);
    const cells = CHECKS.map(([name, test]) => { const f = o.reasons.some(test);
      return `<td><span class="cell ${f ? "fired" : ""}" role="img" aria-label="${esc(name)}: ${f ? "fired" : "not fired"}"></span></td>`; }).join("");
    const result = !o.last ? `<span class="muted">Not launched</span>`
      : o.leaked.length ? pill("quarantined", `Reached ${o.leaked.join(", ")}`)
      : o.where.length ? pill("verified", `Stopped on ${o.where.join(" and ")}`)
      : `<span class="small muted">Waiting in the hub. Sync a device.</span>`;
    return `<tr><td class="attack"><b>${esc(a.title)}</b><span class="small muted">${esc(a.story)}</span></td>${cells}<td>${result}</td>
      <td><button class="hit" data-mode="${esc(m)}" onclick="attack(this.dataset.mode)">Launch</button></td></tr>`;
  }).join("");
  el.innerHTML = `<table class="matrix"><thead><tr><th>Attack</th>${CHECKS.map(([n]) => `<th>${esc(n)}</th>`).join("")}<th>Result</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// ================================================================ conflicts
function renderConflicts(id) {
  const s = states[id], el = document.getElementById(`conf-${id}`);
  if (!s) { el.innerHTML = `<h2>${esc(id)}</h2><p class="muted">Not reachable.</p>`; return; }
  const body = s.conflicts.length ? s.conflicts.map(c => {
    const entries = Object.entries(c.breakdown).sort((a, b) => b[1].score - a[1].score);
    const max = Math.max(...entries.map(([, b]) => b.score), 0.0001);
    return `<div class="card"><div><b>${esc(c.subject)}</b> resolved to ${pill("verified", c.winner)}
      <div class="small muted">after ${esc(c.trigger)}, ${clock(c.ts)}</div></div>
      <div class="scroll-x"><table class="data" style="margin-top:8px"><tr><th>Value</th><th>Devices</th><th>Trust × decay × devices</th><th style="width:34%">Score</th></tr>
      ${entries.map(([v, b]) => `<tr><td>${esc(v)}</td><td>${esc(b.devices.join(", "))}</td><td>${esc(b.formula)}</td>
        <td><div class="row" style="flex-wrap:nowrap"><div style="flex:1"><div class="bar ${v === c.winner ? "win" : ""}" style="transform:scaleX(0)" data-w="${b.score / max}"></div></div>
        <b>${b.score}</b></div></td></tr>`).join("")}</table></div></div>`;
  }).join("") : `<p class="muted">No conflicts yet. Robot says firmware 2.3.1 and kiosk says 2.3.0; sync both devices to see it resolved.</p>`;
  el.innerHTML = `<h2 style="text-transform:capitalize">${esc(id)}</h2>${body}`;
  el.querySelectorAll(".bar[data-w]").forEach(b => requestAnimationFrame(() => requestAnimationFrame(() => { b.style.transform = `scaleX(${b.dataset.w})`; })));
}

// ================================================================ benchmark
let benchDrawn = false;
function renderBench() {
  const notes = document.getElementById("bench-notes"), chartEl = document.getElementById("bench-chart");
  if (!bench || !bench.available) { notes.innerHTML = `<p class="muted">No results yet. Run <code>python benchmark.py</code>.</p>`; return; }
  if (benchDrawn) return;
  benchDrawn = true;
  const rows = bench.scenarios.map(s => ({ name: s.name.replace(" (NO payload index)", ", no index"), plain: s.p50_ms,
    scoped: s.scoped_p50_ms && s.name !== "no filter" ? s.scoped_p50_ms : null, noindex: /NO payload index/.test(s.name) }));
  document.getElementById("bench-legend").innerHTML =
    `<span><i class="swatch" style="background:#eef4fc"></i>Filter only, p50</span>
     <span><i class="swatch" style="background:#8aa2c4"></i>With trust-scoped BM25 IDF</span>
     <span><i class="swatch" style="background:#ff6a5c"></i>Same filter, payload index dropped</span>`;
  const W = 1100, rowH = 36, left = 340, right = 100, H = rows.length * rowH + 40;
  const svg = d3.select(chartEl).append("svg").attr("viewBox", `0 0 ${W} ${H}`).attr("role", "img").attr("aria-label", "Search latency by filter, log scale");
  const x = d3.scaleLog().domain([0.5, 2000]).range([left, W - right]);
  svg.append("g").attr("transform", `translate(0,${H - 24})`)
    .call(d3.axisBottom(x).tickValues([1, 10, 100, 1000]).tickFormat(d => `${d} ms`).tickSize(-(H - 32)))
    .call(g => g.selectAll("line").attr("stroke", "rgba(190,216,255,.16)")).call(g => g.select(".domain").remove())
    .call(g => g.selectAll("text").attr("fill", "#8aa2c4").style("font-size", "12px"));
  const rg = svg.selectAll("g.r").data(rows).join("g").attr("transform", (d, i) => `translate(0,${i * rowH + 8})`);
  rg.append("text").attr("x", left - 12).attr("y", 15).attr("text-anchor", "end").text(d => d.name).style("font-size", "14px")
    .attr("fill", d => (d.noindex ? "#ff6a5c" : "#b9cae2"));
  const grow = sel => sel.attr("width", 0).transition().duration(reduceMotion ? 0 : 1000).delay((d, i) => (reduceMotion ? 0 : 200 + i * 70)).ease(d3.easeCubicOut);
  grow(rg.append("rect").attr("x", left).attr("y", 3).attr("height", 13).attr("fill", d => (d.noindex ? "#ff6a5c" : "#eef4fc")))
    .attr("width", d => x(d.plain) - left);
  grow(rg.filter(d => d.scoped).append("rect").attr("x", left).attr("y", 19).attr("height", 6).attr("fill", "#8aa2c4"))
    .attr("width", d => x(d.scoped) - left);
  rg.append("text").attr("y", 14).attr("x", d => x(d.plain) + 8).text(d => `${d.plain} ms`).style("font-size", "13px").style("font-weight", 700)
    .attr("fill", d => (d.noindex ? "#ff6a5c" : "#eef4fc"));
  const s = Object.fromEntries(bench.scenarios.map(r => [r.name, r]));
  const trust = s["trust_status=verified"], none = s["no filter"];
  const worst = bench.scenarios.filter(r => /NO payload index/.test(r.name)).map(r => Math.round(r.p50_ms / s[r.name.replace(" (NO payload index)", "")].p50_ms));
  const fp = bench.device_footprint;
  notes.innerHTML = `<h2>Findings</h2><ul class="findings">
    <li>The trust filter adds <b>${(trust.p50_ms - none.p50_ms).toFixed(2)} ms</b> to a search over ${bench.n_points.toLocaleString()} memories.</li>
    <li>Drop the payload indexes and the same selective filters get <b>${Math.min(...worst)}–${Math.max(...worst)}× slower</b>. The index is what makes the boundary cheap.</li>
    <li>Scoping BM25's IDF to trusted memories costs <b>${(trust.scoped_p50_ms - trust.p50_ms).toFixed(1)} ms</b> and stops untrusted text from skewing term weights.</li>
    <li>Filtered HNSW recall@10 against exact search: <b>${Math.min(...Object.values(bench.recall_at_10))}</b> or better.</li>
    <li>Embedding the query takes <b>${bench.query_embed_ms.p50} ms</b>, more than the search itself.</li>
    <li>A device holding the model and all ${bench.n_points.toLocaleString()} memories uses <b>${fp.rss_mb.after_100_queries} MB</b> of RAM; the shard loads in ${fp.shard_load_s} s.</li>
    <li>int8 quantization did not pay off: recall fell to ${bench.int8 ? bench.int8.recall_trusted : "n/a"} and disk use grew.</li></ul>
    <p class="small muted">Ingest ${bench.seed.points_per_s} memories/s, limited by CPU embedding. ${esc(bench.machine)}.</p>`;
}

// ================================================================ hub
function renderHub() {
  if (!hubState) { document.getElementById("hub-registry").innerHTML = `<p class="muted">The hub is not reachable.</p>`; return; }
  document.getElementById("hub-registry").innerHTML = `<table class="data"><tr><th>Device</th><th>Trust</th><th>Pinned key</th><th>Registered</th></tr>
    ${Object.entries(hubState.registry).map(([d, r]) => `<tr><td>${esc(d)}</td><td>${pill(r.trust_score >= 0.5 ? "verified" : "quarantined", r.trust_score)}</td>
      <td><code>${esc(r.public_key.slice(0, 18))}</code></td><td class="muted">${clock(r.registered_at)}</td></tr>`).join("")}</table>
    <p class="small muted">Unknown devices get trust ${hubState.config.default_trust}; the screen rejects anything under 0.5.</p>`;
  document.getElementById("hub-events").innerHTML = hubState.events.map(e => { const { ts, event, ...rest } = e;
    return `<div class="small"><span class="muted">${clock(ts)}</span> <b>${esc(event)}</b> <span class="muted">${esc(JSON.stringify(rest))}</span></div>`; }).join("")
    || `<p class="muted">No events yet.</p>`;
  document.getElementById("hub-log").innerHTML = `<table class="data"><tr><th>Seq</th><th>Via</th><th>Subject</th><th>Claimed source</th><th>Route</th></tr>
    ${hubState.log.map(e => `<tr><td>${e.seq}</td><td>${e.via === "attacker" ? pill("quarantined", "attacker") : esc(e.via)}</td><td>${esc(e.subject)}</td>
      <td>${esc(e.source_device)}</td><td>${esc(e.sensitivity)}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">Empty.</td></tr>`}</table>`;
}

// ================================================================ actions
async function refresh() {
  await Promise.all([
    api(HUB, "/state").then(h => { hubState = h; }).catch(() => { hubState = null; }),
    ...Object.entries(DEVICES).map(([id, b]) => api(b, "/api/state").then(s => { states[id] = s; }).catch(() => { states[id] = null; })),
  ]);
  renderNav(); renderRail(); renderActive();
}
function setTab(id, t) { ui[id].tab = t; renderDevice(id); }
async function toggleOffline(id, off) { await post(DEVICES[id], `/api/offline?offline=${off}`); await refresh(); }
async function sync(id) {
  const r = await post(DEVICES[id], "/api/sync");
  const p = r.pull;
  ui[id].lastSync = r.ok ? `Pushed ${r.pushed}. Pulled ${p.received}: ${p.promoted} promoted, ${p.corroborated} corroborated, ${p.conflicts} conflicts, ${p.quarantined} quarantined.` : r.reason;
  animateSync(id, r);
  await refresh(); return r;
}
async function runSearch(id, q, trusted) {
  ui[id].trusted = trusted;
  try { ui[id].search = { q, ...(await api(DEVICES[id], `/api/search?q=${encodeURIComponent(q)}&trusted_only=${trusted}`)) }; }
  catch (e) { ui[id].search = { q, error: e.message }; }
  if (current() === "devices") renderDevice(id);
}
function search(ev, id) { ev.preventDefault(); const f = ev.target; document.activeElement.blur(); runSearch(id, f.q.value, f.trusted.checked); }
async function write(ev, id) {
  ev.preventDefault(); const f = ev.target;
  await api(DEVICES[id], "/api/write", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject: f.subject.value, value: f.value.value, text: f.text.value, sensitivity: f.sensitivity.value }) });
  document.activeElement.blur(); ui[id].tab = "memories"; await refresh();
}
async function release(id, qid) {
  const r = await post(DEVICES[id], `/api/quarantine/${encodeURIComponent(qid)}/release`);
  if (!r.ok) alert(r.reason); else go("conflicts");
  await refresh();
}
async function attack(m) {
  const r = await post(HUB, `/demo/attack/${m}`);
  if (!r.ok) alert(r.error);
  packets("link-outside", [{ color: "#ff6a5c" }]);
  await refresh();
}
async function launchAll() { for (const m of Object.keys(catalog)) await post(HUB, `/demo/attack/${m}`); await refresh(); }

// ================================================================ tour
const TOUR = [
  ["Reset and load", "floor", async () => {
    await post(HUB, "/demo/reset");
    for (const b of Object.values(DEVICES)) { await post(b, "/api/reset"); await post(b, "/api/seed"); }
    ui.robot.search = ui.kiosk.search = null; ui.robot.lastSync = ui.kiosk.lastSync = ""; setMode("trusted");
    return "<b>Both devices hold their own memories.</b> The robot knows Dock 3 is sparking, so it heads for Dock 2. Its owner's email was marked private and will never leave it.";
  }],
  ["Search offline", "devices", async () => {
    for (const b of Object.values(DEVICES)) await post(b, "/api/offline?offline=true");
    await runSearch("robot", "dock 3 charger fault", true); await runSearch("kiosk", "dock 3 charger fault", true);
    return "<b>No network, still answering.</b> Dense and BM25 search fused with RRF on each device's own shard. The thin bar splits query embedding time from search time.";
  }],
  ["Sync", "floor", async () => {
    for (const b of Object.values(DEVICES)) await post(b, "/api/offline?offline=false");
    await sync("robot"); await sync("kiosk"); await sync("robot");
    return "<b>Memories travel through the hub.</b> Each pulled memory is screened on arrival. Dock 3 = broken is now agreed by both devices, and a firmware disagreement was settled; see Conflicts.";
  }],
  ["Attack", "attacks", async () => {
    await launchAll();
    return "<b>Six poisoned memories are in the hub</b>, all saying the sparking charger is safe. The hub does not screen, so nothing is stopped yet.";
  }],
  ["Devices pull", "attacks", async () => {
    await sync("kiosk"); await sync("robot");
    return "<b>All six quarantined.</b> Each red cell is the check that fired. The stolen-key attack is signed by a trusted device under a renamed subject; only the semantic check catches it.";
  }],
  ["See the stakes", "floor", async () => {
    setMode("all");
    return "<b>Without the screen, the robot docks at the sparking charger.</b> This view believes everything that was synced. Flip back to trusted memory to see Engram's answer.";
  }],
  ["Remove the filter", "devices", async () => {
    setMode("trusted");
    await runSearch("kiosk", "firmware version", false); await runSearch("robot", "firmware version", true);
    return "<b>The filter is the boundary.</b> Same query: the kiosk, filter off, also sees the superseded firmware; the robot, filter on, does not.";
  }],
];
const tour = { done: new Set(), next: 0, text: "Seven steps. Each runs against the real APIs and opens the module to watch.", collapsed: matchMedia("(max-width: 900px)").matches, busy: false };
function renderTour() {
  const el = document.getElementById("tour");
  el.classList.toggle("collapsed", tour.collapsed);
  const n = tour.next;
  el.innerHTML = `<div class="steps">${TOUR.map((s, i) => `<button class="${tour.done.has(i) ? "done" : ""} ${i === n ? "current" : ""}"
      title="${esc(s[0])}" aria-label="Step ${i + 1}: ${esc(s[0])}" onclick="runTour(${i})">${i + 1}</button>`).join("")}</div>
    <p aria-live="polite">${tour.text}</p>
    <div class="row">${n < TOUR.length ? `<button class="go" onclick="runTour(${n})" ${tour.busy ? "disabled" : ""}>${tour.busy ? "Running…" : `${n + 1}. ${esc(TOUR[n][0])}`}</button>`
      : `<button class="go" onclick="runTour(0)">Start over</button>`}
      <button class="ghost" onclick="tour.collapsed = !tour.collapsed; renderTour()">${tour.collapsed ? "Tour" : "Hide"}</button></div>`;
}
async function runTour(i) {
  if (tour.busy) return;
  tour.busy = true; tour.collapsed = false; tour.text = `Running step ${i + 1}: ${TOUR[i][0]}…`; renderTour();
  go(TOUR[i][1]);
  try { tour.text = await TOUR[i][2](); tour.done.add(i); tour.next = i + 1; }
  catch (e) { tour.text = `Step ${i + 1} failed: ${esc(e.message)}. Are the services running (python launch.py)?`; }
  tour.busy = false; renderTour(); await refresh();
}

// ================================================================ boot
(async () => {
  try { DEVICES = (await api(HUB, "/config")).devices; } catch { DEVICES = { robot: "http://127.0.0.1:8001", kiosk: "http://127.0.0.1:8002" }; }
  try { catalog = await api(HUB, "/demo/attacks"); } catch { catalog = {}; }
  try { bench = await api(Object.values(DEVICES)[0], "/api/benchmark"); } catch { bench = null; }
  renderTitleblocks(); renderTour(); showView(); await refresh();
  setInterval(refresh, 3000);
})();
