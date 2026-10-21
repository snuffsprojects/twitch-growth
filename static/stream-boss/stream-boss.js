(() => {
  "use strict";
  const BASE = window.TWITCH_GROWTH?.base || "";
  const API = `${BASE}/api/stream-boss`;
  const mode = window.STREAM_BOSS_MODE || "desk";
  let previousHp = null, previousEvent = null, hurtTimer = null, toastTimer = null;
  let selectedBossKey = "signal-gremlin", rosterReady = false;
  const $ = (id) => document.getElementById(id);
  const safe = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  const number = new Intl.NumberFormat("en-GB");
  const time = (value) => new Date(value).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

  function spriteState(boss, damaged) {
    if (boss.status === "defeated") return "defeated";
    if (damaged) return "hurt";
    if (boss.phase >= 3) return "enraged";
    return "idle";
  }

  function showToast(event) {
    const toast = $("event-toast");
    toast.textContent = event.label;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
  }

  function render(data) {
    const boss = data.boss, sprite = $("boss-sprite"), empty = $("empty-battle"), status = $("battle-status");
    if (!boss) {
      $("boss-name").textContent = "Awaiting a challenger…"; sprite.hidden = true; empty.hidden = false;
      status.className = "battle-status"; status.querySelector("span").textContent = "No active battle";
      $("hp-text").textContent = "— / —"; $("hp-fill").style.width = "0"; $("charge-fill").style.width = "0"; $("charge-text").textContent = "0%"; $("phase-text").textContent = "—";
      if (mode === "desk") { renderRoster(data.catalog || []); renderLists([], []); } return;
    }
    const selectedBoss = (data.catalog || []).find((item) => item.key === boss.boss_key);
    if (selectedBoss) sprite.style.setProperty("--boss-sheet", `url('${selectedBoss.sprite_url}')`);
    const damaged = previousHp != null && boss.hp < previousHp && boss.status !== "defeated";
    $("boss-name").textContent = boss.boss_name; sprite.hidden = false; empty.hidden = true;
    sprite.className = `boss-sprite ${spriteState(boss, damaged)}`;
    if (damaged) { $("effects").classList.remove("flash"); void $("effects").offsetWidth; $("effects").classList.add("flash"); clearTimeout(hurtTimer); hurtTimer = setTimeout(() => { sprite.className = `boss-sprite ${spriteState(boss, false)}`; }, 650); }
    status.className = `battle-status ${boss.status}`; status.querySelector("span").textContent = boss.status === "active" ? "Battle live" : boss.status === "paused" ? "Battle paused" : "Boss defeated";
    $("hp-text").textContent = `${number.format(boss.hp)} / ${number.format(boss.max_hp)}`;
    $("hp-fill").style.width = `${Math.max(0, boss.hp / boss.max_hp * 100)}%`;
    $("charge-text").textContent = `${boss.charge}%`; $("charge-fill").style.width = `${boss.charge}%`;
    $("phase-text").textContent = boss.status === "defeated" ? "Clear" : `${boss.phase} / 3`;
    sprite.setAttribute("aria-label", `${boss.boss_name}, ${boss.hp} of ${boss.max_hp} integrity, phase ${boss.phase}`);
    const latest = data.events.at(-1);
    if (latest && previousEvent && `${latest.occurred_at}-${latest.label}` !== previousEvent) showToast(latest);
    if (latest) previousEvent = `${latest.occurred_at}-${latest.label}`;
    previousHp = boss.hp;
    if (mode === "desk") { renderRoster(data.catalog || [], boss.boss_key); renderLists(data.leaders, data.events); }
  }

  function renderRoster(catalog, activeKey) {
    if (rosterReady || !catalog.length) return;
    selectedBossKey = activeKey && catalog.some((boss) => boss.key === activeKey) ? activeKey : catalog[0].key;
    const selectedBoss = catalog.find((boss) => boss.key === selectedBossKey);
    if (selectedBoss) $("summon-name").value = selectedBoss.name;
    const roster = $("boss-roster");
    roster.innerHTML = catalog.map((boss) => `<label class="boss-choice" title="${safe(boss.description)}"><input type="radio" name="boss-key" value="${safe(boss.key)}" ${boss.key === selectedBossKey ? "checked" : ""}><span><i class="boss-thumb" style="--sheet:url('${safe(boss.sprite_url)}')"></i><strong>${safe(boss.name)}</strong><small>${safe(boss.description)}</small></span></label>`).join("");
    roster.addEventListener("change", (event) => {
      const chosen = catalog.find((boss) => boss.key === event.target.value); if (!chosen) return;
      selectedBossKey = chosen.key; $("summon-name").value = chosen.name; $("summon-hp").value = "";
    });
    rosterReady = true;
  }

  function renderLists(leaders, events) {
    $("leader-list").innerHTML = leaders.length ? leaders.map((leader, index) => `<li><i>${index + 1}</i><div><strong>${safe(leader.user_name)}</strong><span>${number.format(leader.messages)} messages · ${number.format(leader.special_hits)} special hits</span></div><b>${number.format(leader.score)}</b></li>`).join("") : '<li class="empty-row">No attacks recorded yet.</li>';
    $("event-list").innerHTML = events.length ? [...events].reverse().map((event) => `<li><div><strong>${safe(event.label)}</strong><span>${safe(event.event_type.replaceAll("_", " "))}</span></div><time>${time(event.occurred_at)}</time></li>`).join("") : '<li class="empty-row">The platform is quiet.</li>';
  }

  async function load() {
    try { const response = await fetch(API, { cache: "no-store" }); if (!response.ok) throw new Error(); render(await response.json()); }
    catch { $("battle-status").className = "battle-status paused"; $("battle-status").querySelector("span").textContent = "Reconnecting"; }
  }

  async function control(action, extra = {}) {
    const status = $("control-status"); status.textContent = "Sending signal…";
    const response = await fetch(`${API}/control`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, ...extra }) });
    const result = await response.json();
    if (!response.ok) { status.textContent = result.error || "The signal was not accepted."; return; }
    status.textContent = action === "spawn" ? "Boss summoned. The battle is live." : "Signal accepted."; render(result);
  }

  if (mode === "desk") {
    const sources = [
      { name: "full", url: new URL(`${BASE}/stream-boss/overlay`, location.origin) },
      { name: "compact", url: new URL(`${BASE}/stream-boss/overlay/compact`, location.origin) },
    ];
    sources.forEach(({ name, url }) => {
      $(`obs-${name}-url`).textContent = url.href; $(`open-${name}-overlay`).href = url.href;
      $(`copy-${name}-url`).addEventListener("click", async () => { await navigator.clipboard.writeText(url.href); $(`copy-${name}-url`).textContent = "Copied"; });
    });
    $("summon-form").addEventListener("submit", (event) => { event.preventDefault(); control("spawn", { boss_key: selectedBossKey, boss_name: $("summon-name").value, max_hp: Number($("summon-hp").value) }); });
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => control(button.dataset.action, { amount: 250 })));
  }
  load(); setInterval(load, 2000);
})();
