(() => {
  "use strict";
  const BASE = window.TWITCH_GROWTH?.base || "";
  const API = `${BASE}/api`;
  const params = new URLSearchParams(location.search);
  const mode = window.STREAMLINE_MODE || "desk";
  const colours = new Set(["violet", "amber", "cyan", "green", "pink"]);
  const chosenColour = colours.has(params.get("colour")) ? params.get("colour") : "violet";
  document.documentElement.dataset.colour = chosenColour;
  let currentData = null;
  let previewCounter = 0;
  let lastStationId = null;

  const demoData = () => {
    const now = Date.now();
    const station = (id, minutes, type, label, detail, importance = "stop") => ({ id, occurred_at: new Date(now + minutes * 60000).toISOString(), type, label, detail, importance });
    return { live: true, updated_at: new Date().toISOString(), stream: { id: "demo", title: "An Unexpected Journey through Eorzea", category_name: "Final Fantasy XIV", peak_viewers: 31 }, stations: [
      station("demo-start", -145, "start", "Going Live", "Final Fantasy XIV", "terminus"),
      station("demo-chat", -132, "follow", "First-time chatter", "Community stop"),
      station("demo-10", -118, "milestone", "10 viewers", "Audience milestone", "interchange"),
      station("demo-sub", -94, "subscribe", "MoogleMage subscribed", "Subscription"),
      station("demo-boss", -61, "boss", "Trial cleared", "Major victory", "interchange"),
      station("demo-raid", -38, "raid", "Raid from PixelSarah (14)", "Incoming service", "interchange"),
      station("demo-25", -22, "milestone", "25 viewers", "Audience milestone", "interchange"),
      station("demo-now", 0, "current", "The journey continues", "Current stop", "terminus"),
    ]};
  };

  const icon = (type) => ({ follow: "+", subscribe: "★", subscription_gift: "✦", cheer: "◆", redemption: "◇", raid: "⇄", raid_outgoing: "→", milestone: "↑", boss: "!", funny: ":)", break: "Ⅱ", category: "×", start: "S", end: "T" }[type] || "•");
  const shortTime = (value) => new Date(value).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const escapeText = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  const splitLabel = (value) => {
    const words = String(value).slice(0, 46).split(/\s+/); let first = "", second = "";
    words.forEach((word) => { if ((first + " " + word).trim().length <= 22 && !second) first = (first + " " + word).trim(); else second = (second + " " + word).trim(); });
    return [first, second.slice(0, 25)];
  };

  function visibleStations(stations) {
    if (stations.length <= 15) return stations;
    const hidden = stations.length - 14;
    return [stations[0], { id: `earlier-${hidden}`, occurred_at: stations[1].occurred_at, type: "earlier", label: `${hidden} earlier stops`, detail: "Journey continues", importance: "stop" }, ...stations.slice(-13)];
  }

  function coordinates(count) {
    const columns = 5, left = 130, right = 1470, top = 170, rowGap = 240;
    const step = (right - left) / (columns - 1);
    return Array.from({ length: count }, (_, index) => {
      const row = Math.floor(index / columns), column = index % columns;
      const displayColumn = row % 2 ? columns - 1 - column : column;
      return { x: left + displayColumn * step, y: top + row * rowGap, row, column };
    });
  }

  function render(data) {
    currentData = data;
    const stations = visibleStations(data.stations || []);
    const points = coordinates(stations.length);
    const route = document.getElementById("route-layer");
    const stationLayer = document.getElementById("station-layer");
    const trainLayer = document.getElementById("train-layer");
    const empty = document.getElementById("map-empty");
    empty.hidden = stations.length > 0;
    document.getElementById("journey-title").textContent = data.stream?.title || "The Streamline";
    document.getElementById("service-name").textContent = data.stream?.category_name ? `${data.stream.category_name} line` : "Tonight's service";
    const state = document.querySelector(".service-state");
    state.classList.toggle("offline", !data.live);
    document.getElementById("live-state").textContent = data.live ? "Live service" : data.stream ? "Completed journey" : "No service";
    if (!stations.length) { route.innerHTML = stationLayer.innerHTML = trainLayer.innerHTML = ""; return; }
    const path = points.map((point, index) => `${index ? "L" : "M"}${point.x},${point.y}`).join(" ");
    const ties = points.slice(0, -1).map((point, index) => {
      const next = points[index + 1], pieces = [], distance = Math.hypot(next.x - point.x, next.y - point.y), count = Math.floor(distance / 43);
      for (let step = 1; step < count; step += 1) { const ratio = step / count, x = point.x + (next.x - point.x) * ratio, y = point.y + (next.y - point.y) * ratio; pieces.push(`<line class="route-tie" x1="${x - (point.y === next.y ? 0 : 13)}" y1="${y - (point.y === next.y ? 13 : 0)}" x2="${x + (point.y === next.y ? 0 : 13)}" y2="${y + (point.y === next.y ? 13 : 0)}"/>`); }
      return pieces.join("");
    }).join("");
    route.innerHTML = `<path class="route-shadow" d="${path}"/><path class="route-line" d="${path}"/>${ties}`;
    stationLayer.innerHTML = stations.map((station, index) => {
      const point = points[index], above = (index + point.row) % 2 === 0, direction = above ? -1 : 1;
      const [lineOne, lineTwo] = splitLabel(station.label), labelY = point.y + direction * 57;
      const importance = station.importance || "stop", radius = importance === "terminus" ? 25 : importance === "interchange" ? 21 : 15;
      return `<g class="station ${importance}" transform="translate(${point.x} ${point.y})"><circle class="station-ring" r="${radius}"/><circle class="event-icon" cx="0" cy="0" r="10"/><text class="event-symbol" y="7">${escapeText(icon(station.type))}</text><text x="0" y="${labelY - point.y}"><tspan x="0">${escapeText(lineOne)}</tspan>${lineTwo ? `<tspan x="0" dy="29">${escapeText(lineTwo)}</tspan>` : ""}<tspan class="station-meta" x="0" dy="25">${escapeText(shortTime(station.occurred_at))}</tspan></text></g>`;
    }).join("");
    const latest = stations.at(-1), end = points.at(-1);
    const isArrival = Boolean(lastStationId && lastStationId !== latest.id);
    trainLayer.innerHTML = `<g class="train${isArrival ? " arriving" : ""}" transform="translate(${end.x + (end.column === 4 ? -72 : 72)} ${end.y - 27})"><rect class="train-body" x="-42" y="-22" width="84" height="44" rx="13"/><rect class="train-window" x="-25" y="-12" width="20" height="17" rx="3"/><rect class="train-window" x="6" y="-12" width="20" height="17" rx="3"/><circle class="train-wheel" cx="-23" cy="23" r="9"/><circle class="train-wheel" cx="23" cy="23" r="9"/></g>`;
    document.getElementById("now-approaching").textContent = latest.label;
    document.getElementById("now-time").textContent = shortTime(latest.occurred_at);
    document.getElementById("station-list").innerHTML = stations.map((station) => `<li>${escapeText(shortTime(station.occurred_at))}: ${escapeText(station.label)}</li>`).join("");
    lastStationId = latest.id;
  }

  async function load() {
    if (params.get("demo") === "1") { render(demoData()); return; }
    try {
      const response = await fetch(`${API}/streamline`, { cache: "no-store" });
      if (!response.ok) throw new Error("Journey unavailable");
      render(await response.json());
    } catch {
      document.querySelector(".service-state").className = "service-state error";
      document.getElementById("live-state").textContent = "Reconnecting";
    }
  }

  function setupDesk() {
    const colour = document.getElementById("line-colour");
    colour.value = chosenColour;
    const updateUrl = () => {
      const overlay = new URL(`${BASE}/streamline/overlay`, location.origin);
      overlay.searchParams.set("colour", colour.value);
      if (params.get("demo") === "1") overlay.searchParams.set("demo", "1");
      document.getElementById("obs-url").textContent = overlay.href;
      document.getElementById("open-overlay").href = overlay.href;
      document.getElementById("demo-link").href = `?demo=1&colour=${colour.value}`;
    };
    colour.addEventListener("change", () => { document.documentElement.dataset.colour = colour.value; updateUrl(); });
    updateUrl();
    document.getElementById("copy-url").addEventListener("click", async () => { await navigator.clipboard.writeText(document.getElementById("obs-url").textContent); document.getElementById("copy-url").textContent = "Copied"; });
    document.getElementById("demo-event").addEventListener("click", () => {
      const base = currentData?.stations?.length ? structuredClone(currentData) : demoData();
      previewCounter += 1; base.live = true; base.stations.push({ id: `preview-${Date.now()}`, occurred_at: new Date().toISOString(), type: previewCounter % 3 === 0 ? "boss" : "gameplay", label: ["A wild moment appears", "Chat chose chaos", "Boss defeated"][previewCounter % 3], detail: "Local preview", importance: previewCounter % 3 === 0 ? "interchange" : "stop" }); render(base);
    });
    document.getElementById("marker-form").addEventListener("submit", async (event) => {
      event.preventDefault(); const status = document.getElementById("form-status"), label = document.getElementById("marker-label"); status.textContent = "Opening the station…";
      const response = await fetch(`${API}/markers`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: label.value, marker_type: document.getElementById("marker-type").value }) });
      const result = await response.json(); if (!response.ok) { status.textContent = result.error || "Station could not be added."; return; }
      status.textContent = "Station added to the journey."; label.value = ""; await load();
    });
  }

  if (mode === "desk") setupDesk();
  load();
  if (params.get("demo") !== "1") setInterval(load, 5000);
})();
