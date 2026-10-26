"use strict";
(() => {
  const byId = (id) => document.getElementById(id);
  const devices = byId("devices");
  const diagnostics = byId("diagnostics");
  const refreshStatus = byId("refresh-status");
  const refreshButton = byId("refresh");
  const revealButton = byId("reveal-token");
  const tokenPanel = byId("token-panel");
  const tokenInput = byId("api-token");
  const tokenStatus = byId("token-status");
  let refreshing = false;
  let tokenGeneration = 0;

  async function read(path) {
    const response = await fetch(path, {
      cache: "no-store", credentials: "same-origin", redirect: "error",
      signal: AbortSignal.timeout(8000), headers: { Accept: "application/json" },
    });
    if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) {
      throw new Error("Request failed");
    }
    return response.json();
  }

  function interacting() {
    return devices.contains(document.activeElement) ||
      diagnostics.contains(document.activeElement) ||
      [...devices.querySelectorAll("details[open]"), ...diagnostics.querySelectorAll("details[open]")]
        .some((details) => !details.parentElement.closest("details:not([open])"));
  }

  async function refresh() {
    if (document.hidden || refreshing) return;
    if (interacting()) {
      refreshStatus.textContent = "Live refresh paused while you inspect details or edit settings. Close details to resume.";
      return;
    }
    refreshing = true;
    refreshButton.setAttribute("aria-busy", "true");
    try {
      const data = await read("./snapshot");
      if (document.hidden || interacting()) return;
      // These fragments are rendered and escaped by our own protected admin endpoint.
      devices.innerHTML = data.devices;
      diagnostics.innerHTML = data.diagnostics;
      byId("broker").innerHTML = data.broker;
      byId("device-summary").textContent = data.summary;
      byId("diagnostic-summary").textContent = data.diagnostic_summary;
      refreshStatus.textContent = `Updated ${new Date().toLocaleTimeString()}. Refreshes every 5 seconds.`;
    } catch {
      refreshStatus.textContent = "Cannot refresh. Displayed reports may be stale; retrying automatically.";
    } finally {
      refreshing = false;
      refreshButton.removeAttribute("aria-busy");
    }
  }

  function hideToken() {
    tokenGeneration += 1;
    tokenInput.value = "";
    tokenPanel.hidden = true;
    revealButton.textContent = "Reveal API token";
    revealButton.setAttribute("aria-expanded", "false");
    tokenStatus.textContent = "";
  }

  revealButton.addEventListener("click", async () => {
    if (!tokenPanel.hidden) {
      hideToken();
      return;
    }
    const generation = ++tokenGeneration;
    revealButton.disabled = true;
    try {
      const data = await read("./token");
      if (generation !== tokenGeneration || document.hidden) return;
      tokenInput.value = data.token;
      tokenPanel.hidden = false;
      revealButton.textContent = "Hide API token";
      revealButton.setAttribute("aria-expanded", "true");
    } catch {
      if (generation === tokenGeneration) tokenStatus.textContent = "Could not reveal token. Try again.";
    } finally {
      revealButton.disabled = false;
    }
  });

  byId("copy-token").addEventListener("click", async () => {
    const generation = tokenGeneration;
    try {
      await navigator.clipboard.writeText(tokenInput.value);
      if (generation === tokenGeneration) tokenStatus.textContent = "Copied. Treat the clipboard contents as a secret.";
    } catch {
      if (generation !== tokenGeneration) return;
      tokenInput.focus();
      tokenInput.select();
      tokenStatus.textContent = "Clipboard unavailable. The token is selected; copy it manually.";
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) hideToken();
    else refresh();
  });
  window.addEventListener("pagehide", hideToken);
  document.addEventListener("submit", (event) => {
    const message = event.target.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });
  byId("theme").addEventListener("change", (event) => {
    if (event.target.value === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.dataset.theme = event.target.value;
  });
  refreshButton.addEventListener("click", refresh);
  refreshButton.hidden = revealButton.hidden = false;
  refreshStatus.textContent = "Live refresh enabled. Updates every 5 seconds.";
  setInterval(refresh, 5000);
})();
