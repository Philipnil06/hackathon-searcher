const BRIDGE = "http://127.0.0.1:8765";

const sendHandshake = (lumaSession = "signed_out") => {
  fetch(`${BRIDGE}/extension-handshake`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({extension_version: chrome.runtime.getManifest().version, luma_session: lumaSession}),
  }).catch(() => {});
};

chrome.runtime.onInstalled.addListener(() => sendHandshake());
chrome.runtime.onStartup.addListener(() => sendHandshake());

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "prepared-application") return;
  const endpoint = `${BRIDGE}/prepared?event_id=${encodeURIComponent(message.eventId)}&applicant_id=${encodeURIComponent(message.applicantId)}`;
  fetch(endpoint)
    .then(async response => {
      const data = await response.json().catch(() => ({}));
      sendResponse({ok: response.ok, status: response.status, data, error: data.error || ""});
    })
    .catch(error => sendResponse({ok: false, error: String(error)}));
  return true;
});

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.type === "extension-heartbeat") {
    sendHandshake(message.lumaSession ? "present" : "signed_out");
  }
  if (message.type === "autofill-status") {
    chrome.storage.local.set({lastStatus: message.status});
    if (sender.tab?.id) chrome.action.setBadgeText({tabId: sender.tab.id, text: message.status.unmatched.length ? "!" : "OK"});
    sendHandshake(message.status.lumaSession ? "present" : "signed_out");
  }
  if (message.type === "luma-session-present") {
    chrome.storage.local.set({lastStatus: message.status});
    if (sender.tab?.id) {
      chrome.action.setBadgeBackgroundColor({tabId: sender.tab.id, color: "#b91c1c"});
      chrome.action.setBadgeText({tabId: sender.tab.id, text: "!"});
    }
    sendHandshake("present");
  }
});
