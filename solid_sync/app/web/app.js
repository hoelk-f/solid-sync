const state = {
  editingId: null,
  profiles: [],
  sensors: [],
  status: null,
};

const basePath = window.location.pathname.endsWith("/")
  ? window.location.pathname
  : `${window.location.pathname}/`;

const apiUrl = (path) => `${basePath}api/${path}`;

const form = document.querySelector("#profile-form");
const sensorSelect = document.querySelector("#sensor_entity_id");
const profileList = document.querySelector("#profile-list");
const profileTemplate = document.querySelector("#profile-template");
const formTitle = document.querySelector("#form-title");
const submitButton = document.querySelector("#submit-button");
const cancelEditButton = document.querySelector("#cancel-edit");
const profileCount = document.querySelector("#profile-count");
const serviceStatus = document.querySelector("#service-status");
const refreshSensorsButton = document.querySelector("#refresh-sensors");

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
  serviceStatus.innerHTML = "";
  const pill = document.createElement("span");
  pill.className = `pill pill-${tone}`;
  pill.textContent = message;
  serviceStatus.appendChild(pill);
}

function renderStatus() {
  if (!state.status) {
    setMessage("No status", "warn");
    return;
  }

  if (state.status.listener_connected) {
    setMessage(`Connected - ${state.status.profile_count} profile(s)`, "ok");
    return;
  }

  const errorText = state.status.listener_last_error
    ? ` - ${state.status.listener_last_error}`
    : "";
  setMessage(`Disconnected${errorText}`, "warn");
}

function renderSensors() {
  const currentValue = sensorSelect.value;
  sensorSelect.innerHTML = "";

  for (const sensor of state.sensors) {
    const option = document.createElement("option");
    option.value = sensor.entity_id;
    option.textContent = `${sensor.name} (${sensor.entity_id})`;
    sensorSelect.appendChild(option);
  }

  if (state.sensors.some((sensor) => sensor.entity_id === currentValue)) {
    sensorSelect.value = currentValue;
  }
}

function fillForm(profile = null) {
  form.reset();
  document.querySelector("#enabled").checked = true;

  if (!profile) {
    state.editingId = null;
    formTitle.textContent = "New profile";
    submitButton.textContent = "Save profile";
    cancelEditButton.hidden = true;
    return;
  }

  state.editingId = profile.id;
  formTitle.textContent = "Edit profile";
  submitButton.textContent = "Update profile";
  cancelEditButton.hidden = false;

  document.querySelector("#name").value = profile.name;
  document.querySelector("#sensor_entity_id").value = profile.sensor_entity_id;
  document.querySelector("#oidc_url").value = profile.oidc_url;
  document.querySelector("#pod_url").value = profile.pod_url;
  document.querySelector("#client_token").value = profile.client_token;
  document.querySelector("#client_secret").value = profile.client_secret;
  document.querySelector("#resource_path").value = profile.resource_path;
  document.querySelector("#enabled").checked = profile.enabled;
}

function profileTime(value) {
  if (!value) {
    return "Never";
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
    node.querySelector(".profile-sensor").textContent = profile.sensor_entity_id;
    node.querySelector(".profile-resource").textContent = profile.resource_path;
    node.querySelector(".profile-last-sync").textContent = profileTime(profile.last_sync_at);
    node.querySelector(".profile-last-error").textContent = profile.last_error || "None";

    const enabled = node.querySelector(".profile-enabled");
    enabled.textContent = profile.enabled ? "Enabled" : "Disabled";
    enabled.classList.add(profile.enabled ? "pill-ok" : "pill-muted");

    node.querySelector(".action-edit").addEventListener("click", () => fillForm(profile));
    node.querySelector(".action-test").addEventListener("click", () => testProfile(profile.id));
    node.querySelector(".action-delete").addEventListener("click", () => deleteProfile(profile.id));

    profileList.appendChild(node);
  }
}

function payloadFromForm() {
  return {
    name: document.querySelector("#name").value.trim(),
    sensor_entity_id: document.querySelector("#sensor_entity_id").value,
    oidc_url: document.querySelector("#oidc_url").value.trim(),
    pod_url: document.querySelector("#pod_url").value.trim(),
    client_token: document.querySelector("#client_token").value.trim(),
    client_secret: document.querySelector("#client_secret").value.trim(),
    resource_path: document.querySelector("#resource_path").value.trim(),
    enabled: document.querySelector("#enabled").checked,
  };
}

async function loadBootstrap() {
  const data = await request(apiUrl("bootstrap"));
  state.profiles = data.profiles;
  state.sensors = data.sensors;
  state.status = data.status;
  renderSensors();
  renderProfiles();
  renderStatus();
}

async function saveProfile(event) {
  event.preventDefault();
  const payload = payloadFromForm();

  if (state.editingId) {
    await request(apiUrl(`profiles/${state.editingId}`), {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  } else {
    await request(apiUrl("profiles"), {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  await loadBootstrap();
  fillForm();
}

async function deleteProfile(profileId) {
  await request(apiUrl(`profiles/${profileId}`), { method: "DELETE" });
  await loadBootstrap();
  if (state.editingId === profileId) {
    fillForm();
  }
}

async function testProfile(profileId) {
  await request(apiUrl(`profiles/${profileId}/test`), { method: "POST" });
  await loadBootstrap();
}

form.addEventListener("submit", async (event) => {
  try {
    setMessage("Saving profile", "warn");
    await saveProfile(event);
    setMessage("Profile saved", "ok");
  } catch (error) {
    setMessage(error.message, "warn");
  }
});

cancelEditButton.addEventListener("click", () => fillForm());

refreshSensorsButton.addEventListener("click", async () => {
  try {
    setMessage("Refreshing sensors", "warn");
    await loadBootstrap();
    setMessage("Sensor list refreshed", "ok");
  } catch (error) {
    setMessage(error.message, "warn");
  }
});

loadBootstrap()
  .then(() => fillForm())
  .catch((error) => {
    setMessage(error.message, "warn");
  });
