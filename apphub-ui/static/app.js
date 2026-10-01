const state = {
  section: "devops-tools",
  sections: [],
  apps: [],
  search: "",
  user: "",
  mongoName: "mongodb",
};

const $ = (id) => document.getElementById(id);

function applyTheme(dark) {
  document.documentElement.classList.toggle("dark", dark);
  localStorage.setItem("apphub-theme", dark ? "dark" : "light");
  const toggle = $("theme-toggle");
  if (toggle) toggle.checked = dark;
}

applyTheme(localStorage.getItem("apphub-theme") === "dark");

window.__iconErr = (img) => {
  const remote = img.getAttribute("data-remote") || "";
  if (remote && img.src !== remote) {
    img.onerror = () => {
      img.onerror = null;
    };
    img.src = remote;
    return;
  }
  img.onerror = null;
};

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, {
    ...options,
    credentials: "include",
    headers,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && state.user && !["/api/login", "/api/me", "/api/logout"].includes(path)) {
      leaveApp();
    }
    throw new Error(data.detail || res.statusText);
  }
  return data;
}

function renderMongoStatus(mongo) {
  const el = $("mongo-status");
  if (!el) return;
  const info = mongo || {};
  const name = info.name === "mongodb-atlas" ? "mongodb-atlas" : "mongodb";
  state.mongoName = name;
  const connected = Boolean(info.connected);
  el.textContent = `${name}: ${connected ? "Connected" : "Disconnected"}`;
  el.classList.toggle("down", !connected);
}

function escapeAttr(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;");
}

function iconSrc(app) {
  if (app.icon === "custom") {
    const name = app.icon_file || app.id;
    const bust = encodeURIComponent(app.icon_file || app.icon_url || Date.now());
    return `/api/icons/${encodeURIComponent(name)}?v=${bust}`;
  }
  return "/static/img/grafana.png";
}

function iconHtml(app) {
  const remote = app.icon === "custom" && app.icon_url ? app.icon_url : "";
  return `<img src="${escapeAttr(iconSrc(app))}" alt="" referrerpolicy="no-referrer" data-remote="${escapeAttr(remote)}" onerror="window.__iconErr(this)" />`;
}

function previewSrc() {
  return $("icon-preview").getAttribute("src") || "";
}

function setIconMode() {
  $("icon-custom-wrap").hidden = false;
  $("icon-preview-wrap").hidden = !previewSrc();
}

function currentSection() {
  return state.sections.find((row) => row.id === state.section) || state.sections[0] || null;
}

function setSection(section) {
  state.section = section;
  renderSectionPicker();
  render();
}

function setSectionMenuOpen(open) {
  $("section-menu").hidden = !open;
  $("section-current").classList.toggle("open", open);
}

function renderSectionPicker() {
  const current = currentSection();
  $("section-current-name").textContent = current?.name || "Select section";
  $("section-list").innerHTML = state.sections.map((section) => {
    const active = section.id === state.section;
    return `
      <div class="section-row${active ? " active" : ""}" data-section="${escapeAttr(section.id)}">
        <button type="button" class="section-row-name" data-choose="${escapeAttr(section.id)}">
          <span class="section-check">${active ? "✓" : ""}</span>
          <span>${escapeAttr(section.name)}</span>
        </button>
        <button type="button" class="section-icon-btn" data-rename="${escapeAttr(section.id)}" title="Edit">✎</button>
      </div>
    `;
  }).join("");
}

function openSectionDialog(existing) {
  $("section-error").hidden = true;
  $("section-id").value = existing?.id || "";
  $("section-name").value = existing?.name || "";
  $("section-dialog-title").textContent = existing ? "Edit section" : "Add section";
  $("section-delete").hidden = !existing;
  $("section-dialog").showModal();
}

async function saveSection(name, id) {
  const form = new FormData();
  form.append("name", name);
  const res = await fetch(id ? `/api/sections/${encodeURIComponent(id)}` : "/api/sections", {
    method: id ? "PUT" : "POST",
    credentials: "include",
    body: form,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function render() {
  const q = state.search.toLowerCase();
  const rows = state.apps.filter((app) => {
    if (app.section !== state.section) return false;
    const hay = `${app.name} ${app.url}`.toLowerCase();
    return hay.includes(q);
  });
  const tiles = rows.map((app) => `
    <article class="tile">
      <a href="${app.url}" target="_blank" rel="noopener noreferrer">
        <div class="tile-icon icon-${app.icon === "custom" ? "custom" : "grafana"}">${iconHtml(app)}</div>
        <h3>${app.name}</h3>
      </a>
      <div class="tile-foot">
        <button type="button" data-edit="${app.id}">Edit</button>
        <button type="button" data-del="${app.id}">Remove</button>
      </div>
    </article>
  `).join("");
  $("grid").innerHTML = `
    ${tiles}
    <button class="add-tile" id="add-tile" type="button">
      <span class="add-plus">+</span>
      <strong>Add app</strong>
      <p>Save a URL here</p>
    </button>
  `;
  $("add-tile").addEventListener("click", () => openDialog());
}

function openDialog(existing) {
  $("app-error").hidden = true;
  $("app-id").value = existing?.id || "";
  $("app-name").value = existing?.name || "";
  $("app-url").value = existing?.url || "";
  $("app-icon").value = "custom";
  $("app-icon-file").value = "";
  $("app-icon-url").value = existing?.icon_url || "";
  if (existing?.icon === "custom" && (existing.icon_file || existing.icon_url)) {
    $("icon-preview").src = existing.icon_file
      ? `/api/icons/${encodeURIComponent(existing.icon_file)}?v=${encodeURIComponent(existing.icon_file)}`
      : existing.icon_url;
  } else if (existing?.icon === "grafana") {
    $("icon-preview").src = "/static/img/grafana.png";
  } else {
    $("icon-preview").removeAttribute("src");
  }
  $("dialog-title").textContent = existing ? "Edit app" : "Add app";
  setIconMode();
  $("app-dialog").showModal();
}

async function refresh() {
  const data = await api("/api/apps");
  state.apps = data.apps;
  state.sections = data.sections || [];
  if (!state.sections.some((row) => row.id === state.section)) {
    state.section = state.sections[0]?.id || "";
  }
  renderMongoStatus(data.mongodb);
  renderSectionPicker();
  render();
}

async function enterApp(user, mongo) {
  state.user = user;
  $("welcome-user").textContent = `Welcome, ${user}`;
  if (mongo) renderMongoStatus(mongo);
  await refresh();
  $("login-screen").hidden = true;
  $("app-screen").hidden = false;
  startIdleWatch();
}

const AUTO_LOGOUT_TIME = 3 * 60 * 1000;
const WARNING_TIME = 30 * 1000;
const idle = {
  timer: null,
  warningTimer: null,
  successTimer: null,
  lastActivity: Date.now(),
  warningWasOpen: false,
  warningOpen: false,
  running: false,
};

function showToast(message, severity) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${severity}`;
  el.hidden = false;
}

function hideToast() {
  $("toast").hidden = true;
}

function openIdleWarning() {
  idle.warningOpen = true;
  if ($("app-dialog").open) $("app-dialog").close();
  if ($("section-dialog").open) $("section-dialog").close();
  setSectionMenuOpen(false);
  const dialog = $("idle-dialog");
  if (!dialog.open) dialog.showModal();
}

function closeIdleWarning() {
  const dialog = $("idle-dialog");
  if (dialog.open) dialog.close();
}

function stopIdleWatch() {
  idle.running = false;
  idle.warningOpen = false;
  clearTimeout(idle.timer);
  clearTimeout(idle.warningTimer);
  clearTimeout(idle.successTimer);
  hideToast();
  closeIdleWarning();
  window.removeEventListener("mousemove", onActivity);
  window.removeEventListener("keydown", onActivity);
  window.removeEventListener("mousedown", onActivity);
  window.removeEventListener("touchstart", onActivity);
  window.removeEventListener("scroll", onActivity);
  document.removeEventListener("visibilitychange", onVisibility);
  window.removeEventListener("focus", onFocus);
  window.removeEventListener("pageshow", onFocus);
}

async function leaveApp() {
  stopIdleWatch();
  if ($("app-dialog").open) $("app-dialog").close();
  if ($("section-dialog").open) $("section-dialog").close();
  setSectionMenuOpen(false);
  try {
    await api("/api/logout", { method: "POST" });
  } catch (_err) {
    /* already logged out */
  }
  state.user = "";
  $("username").value = "";
  $("password").value = "";
  $("login-error").hidden = true;
  $("welcome-user").textContent = "";
  renderMongoStatus({ name: state.mongoName || "mongodb", connected: false });
  $("app-screen").hidden = true;
  $("login-screen").hidden = false;
}

function resetIdleTimer() {
  if (!idle.running) return;
  idle.lastActivity = Date.now();
  clearTimeout(idle.timer);
  clearTimeout(idle.warningTimer);
  if (idle.warningOpen) {
    idle.warningWasOpen = true;
    idle.warningOpen = false;
    closeIdleWarning();
    hideToast();
  }
  idle.warningWasOpen = false;
  idle.timer = setTimeout(() => {
    leaveApp();
  }, AUTO_LOGOUT_TIME);
  idle.warningTimer = setTimeout(() => {
    openIdleWarning();
  }, AUTO_LOGOUT_TIME - WARNING_TIME);
}

function onActivity() {
  resetIdleTimer();
}

function idleExpired() {
  return Date.now() - idle.lastActivity >= AUTO_LOGOUT_TIME;
}

function onVisibility() {
  if (document.visibilityState !== "visible") return;
  if (idleExpired()) leaveApp();
  else resetIdleTimer();
}

function onFocus() {
  if (idleExpired()) leaveApp();
  else resetIdleTimer();
}

function startIdleWatch() {
  stopIdleWatch();
  idle.running = true;
  idle.warningWasOpen = false;
  idle.warningOpen = false;
  window.addEventListener("mousemove", onActivity);
  window.addEventListener("keydown", onActivity);
  window.addEventListener("mousedown", onActivity);
  window.addEventListener("touchstart", onActivity);
  window.addEventListener("scroll", onActivity);
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("focus", onFocus);
  window.addEventListener("pageshow", onFocus);
  resetIdleTimer();
}

$("theme-toggle").addEventListener("change", (e) => {
  applyTheme(e.target.checked);
});
$("section-current").addEventListener("click", () => {
  setSectionMenuOpen($("section-menu").hidden);
});
$("section-menu-close").addEventListener("click", () => setSectionMenuOpen(false));
$("section-list").addEventListener("click", async (e) => {
  const rename = e.target.closest("[data-rename]");
  if (rename) {
    const section = state.sections.find((row) => row.id === rename.dataset.rename);
    if (section) {
      setSectionMenuOpen(false);
      openSectionDialog(section);
    }
    return;
  }
  const choose = e.target.closest("[data-choose]");
  if (choose) {
    setSection(choose.dataset.choose);
    setSectionMenuOpen(false);
  }
});
$("section-new-add").addEventListener("click", async () => {
  $("section-menu-error").hidden = true;
  const name = $("section-new-name").value.trim();
  if (!name) {
    $("section-menu-error").textContent = "Type a section name";
    $("section-menu-error").hidden = false;
    return;
  }
  try {
    const data = await saveSection(name);
    $("section-new-name").value = "";
    state.section = data.id;
    await refresh();
  } catch (err) {
    $("section-menu-error").textContent = err.message;
    $("section-menu-error").hidden = false;
  }
});
$("section-new-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    $("section-new-add").click();
  }
});
document.addEventListener("click", (e) => {
  if (!$("section-picker").contains(e.target)) setSectionMenuOpen(false);
});
$("search").addEventListener("input", (e) => {
  state.search = e.target.value;
  render();
});
$("app-cancel").addEventListener("click", () => $("app-dialog").close());
$("section-cancel").addEventListener("click", () => $("section-dialog").close());
$("section-delete").addEventListener("click", async () => {
  const id = $("section-id").value;
  const section = state.sections.find((row) => row.id === id);
  if (!id || !section) return;
  $("section-error").hidden = true;
  if (!confirm(`Delete "${section.name}" and all apps in it?`)) return;
  try {
    await api(`/api/sections/${encodeURIComponent(id)}`, { method: "DELETE" });
    $("section-dialog").close();
    await refresh();
  } catch (err) {
    $("section-error").textContent = err.message;
    $("section-error").hidden = false;
  }
});
$("section-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("section-error").hidden = true;
  const id = $("section-id").value;
  const name = $("section-name").value.trim();
  try {
    const data = await saveSection(name, id);
    $("section-dialog").close();
    if (!id) state.section = data.id;
    await refresh();
  } catch (err) {
    $("section-error").textContent = err.message;
    $("section-error").hidden = false;
  }
});
$("app-icon-url").addEventListener("input", () => {
  const url = $("app-icon-url").value.trim();
  if (!url) {
    setIconMode();
    return;
  }
  $("app-icon-file").value = "";
  $("icon-preview").src = url;
  $("icon-preview-wrap").hidden = false;
});
$("app-icon-file").addEventListener("change", () => {
  const file = $("app-icon-file").files[0];
  if (!file) {
    setIconMode();
    return;
  }
  $("app-icon-url").value = "";
  $("icon-preview").src = URL.createObjectURL(file);
  $("icon-preview-wrap").hidden = false;
});
$("logout-btn").addEventListener("click", () => leaveApp());
$("idle-stay").addEventListener("click", () => resetIdleTimer());
$("idle-dialog").addEventListener("cancel", (e) => {
  e.preventDefault();
  resetIdleTimer();
});
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").hidden = true;
  try {
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("username").value,
        password: $("password").value,
      }),
    });
    await enterApp(data.user, data.mongodb);
  } catch (err) {
    $("login-error").textContent = err.message;
    $("login-error").hidden = false;
  }
});
$("app-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("app-error").hidden = true;
  const id = $("app-id").value;
  const current = id ? state.apps.find((row) => row.id === id) : null;
  const icon = "custom";
  const file = $("app-icon-file").files[0];
  const iconUrl = $("app-icon-url").value.trim();
  if (!file && !iconUrl && !(current?.icon_file || current?.icon_url || current?.icon === "grafana")) {
    $("app-error").textContent = "Paste an icon URL or upload an image";
    $("app-error").hidden = false;
    return;
  }
  const form = new FormData();
  form.append("name", $("app-name").value);
  form.append("url", $("app-url").value);
  form.append("section", current?.section || state.section);
  form.append("icon", current?.icon === "grafana" && !file && !iconUrl ? "grafana" : icon);
  form.append("icon_url", iconUrl);
  if (file) form.append("icon_file", file);
  try {
    const res = await fetch(id ? `/api/apps/${id}` : "/api/apps", {
      method: id ? "PUT" : "POST",
      credentials: "same-origin",
      body: form,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    $("app-dialog").close();
    await refresh();
  } catch (err) {
    $("app-error").textContent = err.message;
    $("app-error").hidden = false;
  }
});
$("grid").addEventListener("click", async (e) => {
  const edit = e.target.closest("[data-edit]");
  const del = e.target.closest("[data-del]");
  if (edit) {
    e.preventDefault();
    const app = state.apps.find((row) => row.id === edit.dataset.edit);
    if (app) openDialog(app);
  }
  if (del && confirm("Remove this app from AppHub?")) {
    await api(`/api/apps/${del.dataset.del}`, { method: "DELETE" });
    await refresh();
  }
});

api("/api/me")
  .then((me) => enterApp(me.user, me.mongodb))
  .catch(() => {
    $("app-screen").hidden = true;
    $("login-screen").hidden = false;
  });
