const base = window.TWITCH_GROWTH?.base || "";
const number = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 });

const text = (id, value) => { document.getElementById(id).textContent = value; };
const safe = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const formatDate = value => value ? new Date(value).toLocaleDateString("en-GB", { day:"numeric", month:"short", year:"numeric" }) : "—";
const localDateTimeValue = value => {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0,16);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0,16);
};
const duration = (start, end) => {
  if (!start) return "";
  const minutes = Math.max(0, Math.round((new Date(end || Date.now()) - new Date(start)) / 60000));
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
};
const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const timeWindow = hour => `${String(hour).padStart(2,"0")}:00–${String((hour + 3) % 24).padStart(2,"0")}:00`;
let finderData = null;

function finderVerdict(game) {
  if (!game.total_viewers) return "No live audience in this snapshot";
  if (game.top_share >= .65) return "Audience is concentrated at the top";
  if (game.score >= 65) return "Strong candidate right now";
  if (game.score >= 50) return "Worth considering";
  return "A quieter or more competitive option";
}

function renderGameFinderList() {
  if (!finderData) return;
  const filter = document.getElementById("game-finder-filter").value;
  const query = document.getElementById("game-finder-search").value.trim().toLowerCase();
  const games = (finderData.games || []).filter(game => {
    if (query && !`${game.name} ${game.twitch_name || ""}`.toLowerCase().includes(query)) return false;
    if (filter === "recent") return game.recent_playtime_minutes > 0;
    if (filter === "proven") return game.personal_streams > 0;
    if (filter === "unplayed") return game.playtime_minutes === 0;
    if (filter === "live") return game.total_viewers > 0;
    return true;
  });
  const list = document.getElementById("game-finder-list");
  if (!games.length) {
    list.innerHTML = `<div class="game-finder-empty">No sampled games match this view. Each refresh rotates more of the library into consideration.</div>`;
    return;
  }
  list.innerHTML = games.map((game, index) => {
    const icon = game.icon_url ? `<img src="${safe(game.icon_url)}" alt="" loading="lazy">` : `<span class="finder-game-placeholder" aria-hidden="true">${safe(game.name.slice(0,1))}</span>`;
    const reasons = game.reasons?.length ? game.reasons.join(" · ") : "Early evidence—collect more snapshots before deciding";
    const channels = `${number.format(game.active_channels)}${game.truncated ? "+" : ""}`;
    return `<article class="finder-game ${index < 3 ? "top" : ""}">
      <div class="finder-score" title="Recommendation score out of 100">${number.format(game.score)}</div>
      <div class="finder-identity">${icon}<span><strong>${safe(game.name)}</strong><small>${number.format(game.playtime_hours)} Steam hours · ${safe(game.twitch_name || "Unmatched")}</small></span></div>
      <div class="finder-reason"><strong>${finderVerdict(game)} <em class="confidence ${safe(game.confidence)}">${safe(game.confidence)} evidence</em></strong><small title="${safe(reasons)}">${safe(reasons)}</small></div>
      <div class="finder-metric"><span>${number.format(game.total_viewers)}</span><small>live viewers</small></div>
      <div class="finder-metric"><span>${channels}</span><small>channels</small></div>
      <div class="finder-metric"><span>${number.format(game.top_share * 100)}%</span><small>held by #1</small></div>
    </article>`;
  }).join("");
}

function renderGameFinder(data) {
  finderData = data;
  document.getElementById("steam-id").value = data.setup?.steam_id || "";
  document.getElementById("steam-api-key").placeholder = data.setup?.api_key_configured ? "Saved securely—leave blank to keep" : "32-character API key";
  document.getElementById("finder-owned").textContent = number.format(data.total_owned || 0);
  document.getElementById("finder-matched").textContent = number.format(data.matched || 0);
  document.getElementById("finder-sampled").textContent = number.format(data.candidate_count || 0);
  document.getElementById("finder-baseline").textContent = data.typical_viewers == null ? "—" : number.format(data.typical_viewers);
  document.getElementById("game-finder-content").hidden = !(data.setup?.ready || data.games?.length);
  document.getElementById("game-finder-refresh").disabled = !data.setup?.ready;
  document.getElementById("game-finder-updated").textContent = data.sampled_at ? `Live snapshot ${new Date(data.sampled_at).toLocaleString("en-GB", {day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}` : "Not sampled yet";
  const message = document.getElementById("game-finder-message");
  if (data.setup?.last_error) { message.textContent = data.setup.last_error; message.classList.add("error"); }
  renderGameFinderList();
}

async function loadGameFinder() {
  const response = await fetch(`${base}/api/game-finder`, { cache:"no-store" });
  if (!response.ok) throw new Error("Game Finder unavailable");
  renderGameFinder(await response.json());
}

async function refreshGameFinder() {
  const button = document.getElementById("game-finder-refresh");
  const message = document.getElementById("game-finder-message");
  button.disabled = true; button.textContent = "Sampling Twitch…";
  message.textContent = "Syncing the Steam library, matching categories and checking live English channels. This can take a minute.";
  message.classList.remove("error");
  try {
    const response = await fetch(`${base}/api/game-finder/refresh`, { method:"POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Refresh failed");
    message.textContent = `Updated ${result.refresh.sampled} categories from ${result.refresh.owned} owned games${result.refresh.failed ? ` · ${result.refresh.failed} temporarily unavailable` : ""}.`;
  } catch (error) {
    message.textContent = error.message; message.classList.add("error");
  } finally { button.disabled = false; button.textContent = "Refresh recommendations"; }
}

function renderInsights(insights) {
  const cells = insights.schedule || [];
  const populated = cells.filter(cell => cell.streams && cell.average_viewers != null);
  if (populated.length) {
    const maximum = Math.max(...populated.map(cell => cell.average_viewers), 1);
    const lookup = new Map(cells.map(cell => [`${cell.day}-${cell.hour}`, cell]));
    let table = `<table class="heatmap-table"><caption class="sr-only">Average raid-adjusted viewers by weekday and three-hour start window</caption><thead><tr><th>Start</th>${days.map(day => `<th scope="col">${day}</th>`).join("")}</tr></thead><tbody>`;
    for (let hour = 0; hour < 24; hour += 3) {
      table += `<tr><th scope="row">${timeWindow(hour)}</th>`;
      for (let day = 0; day < 7; day++) {
        const cell = lookup.get(`${day}-${hour}`);
        if (!cell?.streams) table += `<td class="heat-empty"><span>—</span><small>no data</small></td>`;
        else {
          const heat = Math.max(.12, cell.average_viewers / maximum);
          table += `<td class="heat-cell" style="--heat:${heat};--confidence:${Math.max(.28, cell.confidence)}" title="${days[day]} ${timeWindow(hour)}: ${cell.average_viewers} raid-adjusted average viewers from ${cell.streams} stream${cell.streams === 1 ? "" : "s"}"><strong>${number.format(cell.average_viewers)}</strong><small>n=${cell.streams}</small></td>`;
        }
      }
      table += `</tr>`;
    }
    document.getElementById("schedule-heatmap").innerHTML = table + `</tbody></table>`;
  }
  if (insights.best_start) {
    text("best-start", `${days[insights.best_start.day]} ${timeWindow(insights.best_start.hour)}`);
    text("best-start-note", `${number.format(insights.best_start.average_viewers)} baseline viewers · ${insights.best_start.streams} streams`);
  } else {
    const fallback=insights.timing_fallback;
    if(fallback?.level==="clock"){text("best-start",`Any day · ${timeWindow(fallback.hour)}`);text("best-start-note",`Broader evidence: ${fallback.average_viewers} baseline viewers · ${fallback.streams} streams`);}
    else if(fallback?.level==="daypart"){text("best-start",`${fallback.weekpart} ${fallback.daypart}`);text("best-start-note",`Broader evidence: ${fallback.average_viewers} baseline viewers · ${fallback.streams} streams`);}
    else text("best-start-note",`${insights.completed_streams} completed · needs 3 in a comparable broad window`);
  }
  if (insights.best_finish) {
    text("best-finish", `${days[insights.best_finish.day]} ${timeWindow(insights.best_finish.hour)}`);
    text("best-finish-note", `${number.format(insights.best_finish.momentum * 100)}% closing momentum · ${insights.best_finish.streams} streams`);
  }

  if (insights.transitions.length) document.getElementById("transition-insights").innerHTML = insights.transitions.slice(0,6).map(item => `<div class="rank-row"><span><strong>${safe(item.from)} → ${safe(item.to)}</strong><small>${item.changes} switch${item.changes === 1 ? "" : "es"}</small></span><b class="${item.retention < .65 ? "risk" : ""}">${number.format(item.retention * 100)}%</b></div>`).join("");
  if (insights.raid_affinity.length) document.getElementById("affinity-insights").innerHTML = insights.raid_affinity.slice(0,6).map(item => `<div class="rank-row"><span><strong>${safe(item.source)}</strong><small>${item.raids} raid${item.raids === 1 ? "" : "s"} · ${item.viewers} announced</small></span><b>${item.retention_30 == null ? "—" : number.format(item.retention_30) + "%"}<em>${item.followers_per_10 == null ? "" : number.format(item.followers_per_10) + " follows / 10"}</em></b></div>`).join("");
  if (insights.chatter_velocity.length) document.getElementById("chatter-insights").innerHTML = insights.chatter_velocity.slice(0,6).map(item => `<div class="rank-row"><span><strong>${safe(item.title || "Untitled stream")}</strong><small>${item.messages} messages · ${item.average_viewers ?? "—"} avg viewers</small></span><b>${item.messages_per_minute_per_viewer == null ? "—" : number.format(item.messages_per_minute_per_viewer)}<em>CPM / viewer</em></b></div>`).join("");
  if (insights.energy.length) document.getElementById("energy-insights").innerHTML = insights.energy.map(item => `<div class="rank-row"><span><strong>Energy ${item.energy} / 5</strong><small>${item.streams} journal${item.streams === 1 ? "" : "s"}</small></span><b>${item.average_viewers ?? "—"}<em>baseline avg</em></b></div>`).join("");
  if(insights.recommendations.length) document.getElementById("coach-card").innerHTML=insights.recommendations.slice(0,3).map((item,index)=>`<div class="coach-rule"><span class="coach-number">${String(index+1).padStart(2,"0")}</span><div><strong>${safe(item.suggested_experiment)}</strong><p>${safe(item.observation)}</p><small>${safe(item.confidence)} evidence · n=${item.sample_size} · ${item.supporting_dates.map(formatDate).join(", ")} · review after ${item.re_evaluate_after_streams} comparable streams</small><details><summary>Evidence caveats</summary>${item.confounding_factors.map(value=>`<p>${safe(value)}</p>`).join("")}</details></div></div>`).join("");
}

function renderCampaigns(data){const links=data.campaign_links||[];text("campaign-count",`${links.length} link${links.length===1?"":"s"}`);if(links.length)document.getElementById("campaign-list").innerHTML=links.map(link=>`<div class="campaign-row"><span><strong>${safe(link.campaign_name)}</strong><small>${safe(link.platform)} · /r/${safe(link.slug)} · ${link.clicks} clicks</small></span><code>${location.origin}${base}/r/${safe(link.slug)}</code><button data-campaign-toggle="${safe(link.slug)}">${link.is_active?"Disable":"Enable"}</button></div>`).join("");const clicks=data.campaign_clicks||[];const strip=document.getElementById("campaign-strip");if(clicks.length){const maximum=Math.max(...clicks.map(item=>item.click_count),1);strip.innerHTML=`<span>Campaign arrivals</span><div>${clicks.map(item=>`<i style="height:${Math.max(4,item.click_count/maximum*30)}px" title="${safe(item.platform)}: ${item.click_count} click${item.click_count===1?"":"s"}"></i>`).join("")}</div><small>${clicks.reduce((sum,item)=>sum+item.click_count,0)} clicks observed during this stream</small>`;}else strip.innerHTML="";}

async function loadStatus() {
  const response = await fetch(`${base}/api/status`, { cache: "no-store" });
  if (!response.ok) throw new Error("Status unavailable");
  const status = await response.json();
  const connection = document.getElementById("connection");
  connection.classList.toggle("connected", status.connected);
  connection.querySelector("span").textContent = status.connected ? `Connected · ${status.channel}` : (status.authorization_expired ? "Twitch authorization expired" : "Twitch not connected");
  const liveChip = document.getElementById("live-chip");
  liveChip.classList.toggle("live", status.live);
  liveChip.lastChild.textContent = status.live ? " Live" : " Offline";
  const setup = document.getElementById("setup-panel");
  setup.hidden = status.connected;
  document.getElementById("setup-checklist").innerHTML = [
    ["Twitch Client ID", status.credentials.client_id],
    [status.authorization_expired ? "Broadcaster token expired" : "Broadcaster token", status.credentials.access_token],
    ["Channel identity", Boolean(status.channel)]
  ].map(([label, ready]) => `<span class="${ready ? "ready" : ""}"><i></i>${safe(label)}</span>`).join("");
}

let authTimer;
async function pollAuthorization(interval) {
  clearTimeout(authTimer);
  const response = await fetch(`${base}/api/auth/poll`, { method: "POST" });
  const result = await response.json();
  if (response.status === 202) {
    authTimer = setTimeout(() => pollAuthorization(interval), interval * 1000);
    return;
  }
  if (!response.ok) {
    document.getElementById("device-status").textContent = result.error || "Authorization failed";
    document.getElementById("connect-button").disabled = false;
    return;
  }
  document.getElementById("device-status").textContent = `Connected as ${result.channel}`;
  setTimeout(() => location.reload(), 900);
}

async function startAuthorization() {
  const button = document.getElementById("connect-button");
  button.disabled = true;
  button.textContent = "Creating code…";
  try {
    const response = await fetch(`${base}/api/auth/start`, { method: "POST" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to start authorization");
    document.getElementById("device-flow").hidden = false;
    document.getElementById("device-code").textContent = result.user_code;
    document.getElementById("device-link").href = result.verification_uri;
    document.getElementById("device-status").textContent = "Waiting for approval…";
    button.textContent = "Authorization in progress";
    window.open(result.verification_uri, "_blank", "noopener,noreferrer");
    pollAuthorization(Math.max(5, result.interval));
  } catch (error) {
    button.disabled = false;
    button.textContent = "Authorise with Twitch";
    alert(error.message);
  }
}

function renderChart(points, markers = []) {
  if (!points.length) return;
  const width = 900, height = 250, pad = 18;
  const max = Math.max(1, ...points.map(point => point.viewer_count));
  const xy = points.map((point, index) => ({
    x: pad + (index / Math.max(1, points.length - 1)) * (width - pad * 2),
    y: height - pad - (point.viewer_count / max) * (height - pad * 2),
    value: point.viewer_count
  }));
  const line = xy.map((point, index) => `${index ? "L" : "M"}${point.x},${point.y}`).join(" ");
  const area = `${line} L${xy.at(-1).x},${height-pad} L${xy[0].x},${height-pad} Z`;
  const start = new Date(points[0].sampled_at).getTime();
  const end = new Date(points.at(-1).sampled_at).getTime();
  const markerSvg = markers.map(marker => {
    const ratio = Math.max(0, Math.min(1, (new Date(marker.occurred_at).getTime() - start) / Math.max(1, end - start)));
    const x = pad + ratio * (width - pad * 2);
    return `<line class="chart-marker" x1="${x}" y1="${pad}" x2="${x}" y2="${height-pad}"/><circle class="chart-marker-dot" cx="${x}" cy="${pad}" r="5"><title>${safe(marker.label)}</title></circle>`;
  }).join("");
  document.getElementById("viewer-chart").innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Viewer count and event markers over the latest stream"><defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#a985ff" stop-opacity=".28"/><stop offset="1" stop-color="#a985ff" stop-opacity="0"/></linearGradient></defs><path class="chart-area" d="${area}"/><path class="chart-line" d="${line}"/>${markerSvg}${xy.map(point => `<circle class="chart-dot" cx="${point.x}" cy="${point.y}" r="4"><title>${point.value} viewers</title></circle>`).join("")}</svg>`;
}

function renderDashboard(data) {
  const totals = data.totals;
  text("average-viewers", number.format(totals.average_viewers));
  text("baseline-viewers", number.format(totals.baseline_average_viewers));
  text("best-peak", number.format(totals.best_peak));
  text("followers", number.format(totals.followers));
  text("chatters", number.format(totals.unique_chatters));
  text("viewer-hours", number.format(totals.viewer_hours));
  text("streams", number.format(totals.streams));
  text("stream-count", `${totals.streams} stream${totals.streams === 1 ? "" : "s"}`);
  renderChart(data.viewer_timeline, data.markers);
  renderInsights(data.insights);
  renderCampaigns(data);

  if (data.recent_streams.length) {
    document.getElementById("stream-table").innerHTML = data.recent_streams.map(stream => `<tr><td><strong class="stream-title">${safe(stream.title || "Untitled stream")}</strong><span class="stream-meta">${safe(stream.category_name || "No category")} · ${formatDate(stream.started_at)} · ${duration(stream.started_at, stream.ended_at)}</span></td><td>${number.format(stream.average_viewers || 0)}</td><td>${stream.baseline_average_viewers == null ? "—" : number.format(stream.baseline_average_viewers)}</td><td>${number.format(stream.peak_viewers || 0)}</td><td>${number.format(stream.unique_chatters || 0)}</td><td>${number.format(stream.follows || 0)}</td></tr>`).join("");
  }

  if (data.categories.length) {
    const max = Math.max(...data.categories.map(item => item.average_viewers || 0), 1);
    document.getElementById("category-list").innerHTML = data.categories.map(item => `<div class="category-row"><strong>${safe(item.category_name)}</strong><div class="category-bar"><i style="width:${Math.max(3,(item.average_viewers || 0) / max * 100)}%"></i></div><span><b>${number.format(item.average_viewers || 0)}</b> avg</span><span>${item.streams} streams</span></div>`).join("");
  }

  if (data.event_mix.length) {
    document.getElementById("signal-empty").hidden = true;
    const icons = { follow:"+", subscribe:"S", subscription_message:"S", subscription_gift:"G", cheer:"◆", raid:"↗", redemption:"★" };
    document.getElementById("signal-list").innerHTML = data.event_mix.map(item => `<div class="signal-row"><span class="signal-icon">${icons[item.event_type] || "•"}</span><span><strong>${safe(item.event_type.replaceAll("_", " "))}</strong><small>Since tracking began</small></span><b>${number.format(item.count)}</b></div>`).join("");
  }

  if (data.top_clips.length) {
    document.getElementById("clip-list").innerHTML = data.top_clips.map(clip => `<a class="clip-card" href="${safe(clip.url)}" target="_blank" rel="noopener noreferrer"><strong>${safe(clip.title || "Untitled clip")}</strong><span><i>${safe(clip.creator_name || "Unknown creator")}</i><b>${number.format(clip.view_count)} views ↗</b></span></a>`).join("");
  }

  if (data.raids.length) {
    document.getElementById("raid-list").innerHTML = data.raids.map(raid => `<div class="raid-row"><strong>${safe(raid.user_name || "Outgoing raid")}</strong><small>${formatDate(raid.occurred_at)} · announced ${number.format(raid.amount || 0)} viewers</small><div class="raid-stats"><span>Before<b>${raid.before ?? "—"}</b></span><span>+5m<b>${raid.after_5 ?? "—"}</b></span><span>+15m<b>${raid.after_15 ?? "—"}</b></span><span>+30m<b>${raid.after_30 ?? "—"}</b></span></div></div>`).join("");
  }

  const notesForm = document.getElementById("notes-form");
  notesForm.hidden = !data.latest_stream_id;
  document.getElementById("notes-empty").hidden = Boolean(data.latest_stream_id);
  notesForm.dataset.streamId = data.latest_stream_id || "";
  const notes = data.notes || {};
  document.getElementById("note-summary").value = notes.summary || "";
  document.getElementById("note-promotion").value = notes.promotion || "";
  document.getElementById("note-promotion-at").value = localDateTimeValue(notes.promotion_at);
  document.getElementById("note-discord-promoted").value = notes.discord_promoted == null ? "" : String(notes.discord_promoted);
  document.getElementById("note-instagram-promoted").value = notes.instagram_promoted == null ? "" : String(notes.instagram_promoted);
  document.getElementById("note-threads-promoted").value = notes.threads_promoted == null ? "" : String(notes.threads_promoted);
  document.getElementById("note-stream-plan").value = notes.stream_plan || "";
  document.getElementById("note-special-event").value = notes.special_event_type || "";
  document.getElementById("note-guests").value = notes.guests || "";
  document.getElementById("note-well").value = notes.went_well || "";
  document.getElementById("note-improve").value = notes.improve || "";
  document.getElementById("note-next").value = notes.next_test || "";
  document.getElementById("note-energy").value = notes.energy || "";
}

document.getElementById("add-marker").addEventListener("click", () => {
  const form = document.getElementById("marker-form");
  form.hidden = !form.hidden;
  if (!form.hidden) document.getElementById("marker-label").focus();
});

document.getElementById("marker-form").addEventListener("submit", async event => {
  event.preventDefault();
  const response = await fetch(`${base}/api/markers`, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ label:document.getElementById("marker-label").value, marker_type:document.getElementById("marker-type").value }) });
  const result = await response.json();
  if (!response.ok) return alert(result.error || "Could not save marker");
  document.getElementById("marker-label").value = "";
  document.getElementById("marker-form").hidden = true;
  boot();
});

document.getElementById("notes-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const streamId = form.dataset.streamId;
  if (!streamId) return;
  const payload = { summary:document.getElementById("note-summary").value, promotion:document.getElementById("note-promotion").value, promotion_at:document.getElementById("note-promotion-at").value, discord_promoted:document.getElementById("note-discord-promoted").value, instagram_promoted:document.getElementById("note-instagram-promoted").value, threads_promoted:document.getElementById("note-threads-promoted").value, stream_plan:document.getElementById("note-stream-plan").value, special_event_type:document.getElementById("note-special-event").value, guests:document.getElementById("note-guests").value, went_well:document.getElementById("note-well").value, improve:document.getElementById("note-improve").value, next_test:document.getElementById("note-next").value, energy:document.getElementById("note-energy").value };
  const response = await fetch(`${base}/api/streams/${streamId}/notes`, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) });
  const result = await response.json();
  const status = document.getElementById("notes-status");
  status.textContent = response.ok ? "Saved" : (result.error || "Save failed");
  setTimeout(() => status.textContent = "", 2500);
});

document.getElementById("campaign-form").addEventListener("submit",async event=>{event.preventDefault();const payload={slug:document.getElementById("campaign-slug").value,platform:document.getElementById("campaign-platform").value,campaign_name:document.getElementById("campaign-name").value,target_url:document.getElementById("campaign-target").value};const response=await fetch(`${base}/api/campaigns`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const result=await response.json();if(!response.ok)return alert(result.error||"Could not create campaign");event.currentTarget.reset();boot();});
document.getElementById("campaign-list").addEventListener("click",async event=>{const slug=event.target.dataset.campaignToggle;if(!slug)return;await fetch(`${base}/api/campaigns/${encodeURIComponent(slug)}/toggle`,{method:"POST"});boot();});

async function boot() {
  try {
    await loadStatus();
    const response = await fetch(`${base}/api/dashboard`, { cache: "no-store" });
    if (!response.ok) throw new Error("Dashboard unavailable");
    renderDashboard(await response.json());
    await loadGameFinder();
  } catch (error) {
    const connection = document.getElementById("connection");
    connection.querySelector("span").textContent = "Dashboard unavailable";
    console.error(error);
  }
}

document.getElementById("connect-button").addEventListener("click", startAuthorization);
boot();
