const BRIDGE = "http://127.0.0.1:8765";
const PENDING_KEY = "pendingSubmissionResults";
const MAX_RETRIES = 120; // ~10 minutes of 5s retries; enough to survive a bridge restart.
const FLUSH_INTERVAL_MS = 5000;

let lastHandshake = {session: "", at: 0};
const sendHandshake = (lumaSession = "signed_out", force = false) => {
  const now = Date.now();
  // The dialog can emit many status mutations. One ping per minute (or when
  // the session state changes) is enough for diagnostics and saves work.
  if (!force && lastHandshake.session === lumaSession && now - lastHandshake.at < 60000) return;
  lastHandshake = {session: lumaSession, at: now};
  fetch(`${BRIDGE}/extension-handshake`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({extension_version: chrome.runtime.getManifest().version, luma_session: lumaSession}),
  }).catch(() => {});
};

const postResult = result =>
  fetch(`${BRIDGE}/submission-result`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(result),
  }).then(async response => ({ok: response.ok, status: response.status, data: await response.json().catch(() => ({}))}));

// Persistent queue of submission results that have not yet been accepted by
// the bridge. Entries are {result, attempts}. The queue is stored in
// chrome.storage.local so a restart of either Chrome or the bridge cannot
// lose an irreversible submission record.
const queueSubmissionResult = result => {
  chrome.storage.local.get({[PENDING_KEY]: []}, state => {
    const pending = state[PENDING_KEY] || [];
    pending.push({result, attempts: 0});
    chrome.storage.local.set({[PENDING_KEY]: pending});
    flushSubmissionResults();
  });
};

let flushing = false;
const flushSubmissionResults = () => {
  if (flushing) return;
  flushing = true;
  const step = () => {
    chrome.storage.local.get({[PENDING_KEY]: []}, state => {
      const pending = state[PENDING_KEY] || [];
      const entry = pending[0];
      if (!entry) {
        flushing = false;
        return;
      }
      postResult(entry.result)
        .then(({ok, data}) => {
          if (ok && data && data.ok) {
            chrome.storage.local.set({lastSubmission: {ok: true, status: data.status || "", snapshot: data.snapshot || ""}});
            pending.shift();
            chrome.storage.local.set({[PENDING_KEY]: pending}, step);
          } else {
            chrome.storage.local.set({lastSubmission: {ok: false, error: (data && data.error) || "bridge rejected"}});
            entry.attempts += 1;
            if (entry.attempts >= MAX_RETRIES) {
              // Give up after many retries; the audit log still shows the
              // submission click evidence, so it will surface in review.
              pending.shift();
            }
            chrome.storage.local.set({[PENDING_KEY]: pending}, () => { flushing = false; });
          }
        })
        .catch(() => {
          entry.attempts += 1;
          if (entry.attempts >= MAX_RETRIES) pending.shift();
          chrome.storage.local.set({[PENDING_KEY]: pending}, () => { flushing = false; });
        });
    });
  };
  step();
};

// Retry loop so a temporarily stopped bridge cannot lose a submission record.
setInterval(flushSubmissionResults, FLUSH_INTERVAL_MS);

chrome.runtime.onInstalled.addListener(() => { sendHandshake("signed_out", true); flushSubmissionResults(); });
chrome.runtime.onStartup.addListener(() => { sendHandshake("signed_out", true); flushSubmissionResults(); });

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
  if (message.type === "submission-result") {
    const result = message.result || {};
    // Queue first (persistent), then flush immediately. If the bridge is down
    // the entry stays queued and the retry loop delivers it once the bridge
    // is reachable again.
    queueSubmissionResult(result);
  }
  if (message.type === "submission-result-close-tab") {
    // The application was confirmed: close the working tab. The bridge record
    // is already queued/delivered, so closing cannot lose the audit trail.
    if (sender.tab?.id) {
      chrome.tabs.remove(sender.tab.id).catch(() => {});
    }
  }
});
