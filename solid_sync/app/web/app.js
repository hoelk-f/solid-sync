const state = {
  profiles: [],
  entities: [],
  settings: null,
  status: null,
};

const basePath = window.location.pathname.endsWith("/")
  ? window.location.pathname
  : `${window.location.pathname}/`;

const apiUrl = (path) => `${basePath}api/${path}`;

const settingsForm = document.querySelector("#settings-form");
const profileForm = document.querySelector("#profile-form");
const measurementList = document.querySelector("#measurement-list");
const measurementTemplate = document.querySelector("#measurement-template");
const profileList = document.querySelector("#profile-list");
const profileTemplate = document.querySelector("#profile-template");
const formTitle = document.querySelector("#form-title");
const submitButton = document.querySelector("#submit-button");
const profileCount = document.querySelector("#profile-count");
const pageStatus = document.querySelector("#page-status");
const addMeasurementButton = document.querySelector("#add-measurement");
const refreshEntitiesButton = document.querySelector("#refresh-entities");

function defaultMeasurements() {
  return [
    { key: "temperature", entity_id: "" },
    { key: "humidity", entity_id: "" },
    { key: "air_pressure", entity_id: "" },
  ];
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  const text = await response.text();
  let body = null;

  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }

  if (!response.ok) {
    throw new Error(body?.message || text || `Request failed with ${response.status}`);
  }

  return body;
}

function setMessage(message, tone = "warn") {
  if (!message) {
    clearMessage();
    return;
  }

  pageStatus.hidden = false;
  pageStatus.className = `page-status page-status-${tone}`;
  pageStatus.textContent = message;
}

function clearMessage() {
  pageStatus.hidden = true;
  pageStatus.className = "page-status";
  pageStatus.textContent = "";
}

function renderStatus() {
  if (!state.status) {
    setMessage("Unable to load add-on status.", "warn");
    return;
  }

  if (!state.status.settings_complete) {
    setMessage("Complete the Solid connection settings to start syncing.", "warn");
    return;
  }

  if (!state.status.listener_connected) {
    const errorText = state.status.listener_last_error
      ? ` ${state.status.listener_last_error}`
      : "";
    setMessage(`Home Assistant listener disconnected.${errorText}`, "warn");
    return;
  }

  clearMessage();
}

function entityLabel(entityId) {
  const entity = state.entities.find((item) => item.entity_id === entityId);
  if (!entity) {
    return entityId;
  }
  return `${entity.name} (${entity.entity_id})`;
}

function populateEntitySelect(select, selectedValue = "") {
  select.innerHTML = "";

  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "Select entity";
  select.appendChild(placeholder);

  for (const entity of state.entities) {
    const option = document.createElement("option");
    option.value = entity.entity_id;
    option.textContent = `${entity.name} (${entity.entity_id})`;
    select.appendChild(option);
  }

  select.value = selectedValue || "";
}

function createMeasurementRow(measurement = { key: "", entity_id: "" }) {
  const node = measurementTemplate.content.cloneNode(true);
  const row = node.querySelector(".measurement-row");
  const keyInput = row.querySelector(".measurement-key");
  const entitySelect = row.querySelector(".measurement-entity");
  const removeButton = row.querySelector(".measurement-remove");

  keyInput.value = measurement.key || "";
  populateEntitySelect(entitySelect, measurement.entity_id || "");

  removeButton.addEventListener("click", () => {
    row.remove();
    if (!measurementList.children.length) {
      renderMeasurementRows(defaultMeasurements());
    }
  });

  measurementList.appendChild(row);
}

function renderMeasurementRows(measurements) {
  measurementList.innerHTML = "";
  for (const measurement of measurements) {
    createMeasurementRow(measurement);
  }
}

function refreshMeasurementSelects() {
  measurementList.querySelectorAll(".measurement-entity").forEach((select) => {
    const selectedValue = select.value;
    populateEntitySelect(select, selectedValue);
  });
}

function fillSettingsForm(settings = {}) {
  document.querySelector("#settings_oidc_url").value = settings.oidc_url || "";
  document.querySelector("#settings_pod_url").value = settings.pod_url || "";
  document.querySelector("#settings_client_token").value = settings.client_token || "";
  document.querySelector("#settings_client_secret").value = settings.client_secret || "";
}

function fillProfileForm() {
  profileForm.reset();
  setSectionExpanded("profile-form", true);
  formTitle.textContent = "New profile";
  submitButton.textContent = "Save profile";
  renderMeasurementRows(defaultMeasurements());
}

function profileTime(value) {
  if (!value) {
    return "Never";
  }

  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function profilePendingTime(value) {
  if (!value) {
    return "Waiting for first change";
  }

  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function renderProfiles() {
  profileCount.textContent = String(state.profiles.length);
  profileList.innerHTML = "";

  if (!state.profiles.length) {
    profileList.innerHTML = '<p class="empty">No profiles configured yet.</p>';
    return;
  }

  for (const profile of state.profiles) {
    const node = profileTemplate.content.cloneNode(true);
    node.querySelector(".profile-name").textContent = profile.name;
    node.querySelector(".profile-resource").textContent = profile.resource_path;
    node.querySelector(".profile-last-resource").textContent =
      profile.last_resource_path || "No file written yet";
    node.querySelector(".profile-last-sync").textContent = profileTime(profile.last_sync_at);
    const pendingCount = profile.pending_entry_count || 0;
    node.querySelector(".profile-next-flush").textContent = pendingCount
      ? profilePendingTime(profile.next_flush_at)
      : "No upload scheduled";
    node.querySelector(".profile-pending-count").textContent = String(pendingCount);
    node.querySelector(".profile-last-error").textContent = profile.last_error || "None";

    const chips = node.querySelector(".profile-measurements");
    for (const measurement of profile.measurements) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = `${measurement.key} -> ${entityLabel(measurement.entity_id)}`;
      chips.appendChild(chip);
    }

    node.querySelector(".action-test").addEventListener("click", async () => {
      try {
        setMessage("Running profile sync", "warn");
        await testProfile(profile.id);
        setMessage("Profile synced", "ok");
      } catch (error) {
        setMessage(error.message, "warn");
      }
    });
    node.querySelector(".action-delete").addEventListener("click", async () => {
      try {
        setMessage("Deleting profile", "warn");
        await deleteProfile(profile.id);
        setMessage("Profile deleted", "ok");
      } catch (error) {
        setMessage(error.message, "warn");
      }
    });

    profileList.appendChild(node);
  }
}

function payloadFromSettingsForm() {
  return {
    oidc_url: document.querySelector("#settings_oidc_url").value.trim(),
    pod_url: document.querySelector("#settings_pod_url").value.trim(),
    client_token: document.querySelector("#settings_client_token").value.trim(),
    client_secret: document.querySelector("#settings_client_secret").value.trim(),
  };
}

function collectMeasurements() {
  const measurements = [];

  for (const row of measurementList.querySelectorAll(".measurement-row")) {
    const key = row.querySelector(".measurement-key").value.trim();
    const entityId = row.querySelector(".measurement-entity").value;

    if (!entityId) {
      continue;
    }

    if (!key) {
      throw new Error("Each selected measurement needs a field key");
    }

    measurements.push({
      key,
      entity_id: entityId,
    });
  }

  return measurements;
}

function payloadFromProfileForm() {
  return {
    name: document.querySelector("#name").value.trim(),
    resource_path: document.querySelector("#resource_path").value.trim(),
    measurements: collectMeasurements(),
  };
}

function setSectionExpanded(sectionName, expanded) {
  const section = document.querySelector(`[data-section="${sectionName}"]`);
  if (!section) {
    return;
  }

  const toggle = section.querySelector("[data-section-toggle]");
  const body = section.querySelector("[data-section-body]");
  if (!toggle || !body) {
    return;
  }

  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.querySelector(".toggle-indicator").textContent = expanded ? "-" : "+";
  body.hidden = !expanded;
  section.classList.toggle("is-collapsed", !expanded);
}

function initCollapsibleSections() {
  document.querySelectorAll("[data-section]").forEach((section) => {
    const toggle = section.querySelector("[data-section-toggle]");
    const body = section.querySelector("[data-section-body]");
    if (!toggle || !body) {
      return;
    }

    setSectionExpanded(
      section.getAttribute("data-section"),
      toggle.getAttribute("aria-expanded") !== "false"
    );
    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") !== "true";
      setSectionExpanded(section.getAttribute("data-section"), expanded);
    });
  });
}

async function loadBootstrap() {
  const data = await request(apiUrl("bootstrap"));
  state.settings = data.settings;
  state.profiles = data.profiles;
  state.entities = data.entities;
  state.status = data.status;

  fillSettingsForm(state.settings);
  refreshMeasurementSelects();
  renderProfiles();
  renderStatus();
}

async function saveSettings(event) {
  event.preventDefault();
  await request(apiUrl("settings"), {
    method: "PUT",
    body: JSON.stringify(payloadFromSettingsForm()),
  });
  await loadBootstrap();
}

async function saveProfile(event) {
  event.preventDefault();
  const payload = payloadFromProfileForm();
  await request(apiUrl("profiles"), {
    method: "POST",
    body: JSON.stringify(payload),
  });

  await loadBootstrap();
  fillProfileForm();
}

async function deleteProfile(profileId) {
  await request(apiUrl(`profiles/${profileId}`), { method: "DELETE" });
  await loadBootstrap();
}

async function testProfile(profileId) {
  await request(apiUrl(`profiles/${profileId}/test`), { method: "POST" });
  await loadBootstrap();
}

settingsForm.addEventListener("submit", async (event) => {
  try {
    setMessage("Saving Solid settings", "warn");
    await saveSettings(event);
    setMessage("Solid settings saved", "ok");
  } catch (error) {
    setMessage(error.message, "warn");
  }
});

profileForm.addEventListener("submit", async (event) => {
  try {
    setMessage("Saving profile", "warn");
    await saveProfile(event);
    setMessage("Profile saved", "ok");
  } catch (error) {
    setMessage(error.message, "warn");
  }
});

addMeasurementButton.addEventListener("click", () => {
  createMeasurementRow({ key: "", entity_id: "" });
});

refreshEntitiesButton.addEventListener("click", async () => {
  try {
    setMessage("Refreshing entities", "warn");
    await loadBootstrap();
    setMessage("Entity list refreshed", "ok");
  } catch (error) {
    setMessage(error.message, "warn");
  }
});

initCollapsibleSections();

loadBootstrap()
  .then(() => fillProfileForm())
  .catch((error) => {
    setMessage(error.message, "warn");
  });
