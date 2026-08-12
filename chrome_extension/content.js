(() => {
  const STATUS_ID = "hackathon-searcher-status";
  const normalize = value => (value || "").toLowerCase().replace(/\s+/g, " ").replace(/[\*:\?]/g, "").trim();
  const visible = element => !!(element?.offsetWidth || element?.offsetHeight || element?.getClientRects().length);
  let payload = null;
  let filledKey = null;
  let timer = null;
  let ctaClickAttempted = false;

  // A Chrome extension reload invalidates an already injected content script.
  // Never let that lifecycle event abort form detection or autofill diagnostics.
  const safeMessage = message => {
    try {
      if (!chrome?.runtime?.id) return false;
      chrome.runtime.sendMessage(message);
      return true;
    } catch (error) {
      console.warn("HACKATHON_SEARCHER_EXTENSION_CONTEXT_INVALIDATED", error);
      return false;
    }
  };

  console.info("HACKATHON_SEARCHER_CONTENT_SCRIPT_LOADED", {url: location.href});
  safeMessage({type:"extension-heartbeat", lumaSession: /\b(log out|sign out)\b/i.test(document.body?.innerText || "")});

  const report = ({message, level = "info", filled = 0, total = 0, unmatched = [], applicant = ""}) => {
    const box = document.getElementById(STATUS_ID) || Object.assign(document.createElement("div"), {id: STATUS_ID});
    const colors = {info: "#152238", warning: "#854d0e", error: "#7f1d1d", success: "#14532d"};
    box.textContent = `Hackathon Searcher\n${applicant ? `${applicant} â€” ` : ""}${message}${total ? `\n${filled}/${total} fields filled` : ""}${unmatched.length ? `\nNeeds attention: ${unmatched.join(", ")}` : ""}`;
    Object.assign(box.style, {position:"fixed",right:"16px",bottom:"16px",zIndex:"2147483647",whiteSpace:"pre-line",padding:"10px 12px",maxWidth:"340px",background:colors[level],color:"#fff",borderRadius:"8px",font:"13px system-ui",boxShadow:"0 4px 18px #0006"});
    document.body.appendChild(box);
    safeMessage({type:"autofill-status", status:{event:payload?.event_name || "", applicant, filled, unmatched, message, level, lumaSession: lumaSessionPresent()}});
  };

  const lumaSessionPresent = () => /\b(log out|sign out)\b/i.test(document.body?.innerText || "");
  const labelFor = control => {
    const id = control.id;
    const explicit = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
    const direct = [explicit?.innerText, control.getAttribute("aria-label"), control.name, control.placeholder].filter(Boolean);
    if (direct.length) return normalize(direct.join(" "));
    return normalize(control.closest("label,fieldset,[role=group],[role=dialog]")?.innerText?.slice(0, 400));
  };
  const fieldKind = question => {
    const descriptor = normalize(`${question.label || question.name || ""} ${question.name || ""} ${question.field_type || ""}`);
    if (/(email|e-post|epost)/.test(descriptor)) return "email";
    if (/(^| )(name|namn|full name|fullstÃ¤ndigt namn)( |$)/.test(descriptor)) return "name";
    if (/(phone|telefon|mobile)/.test(descriptor)) return "phone";
    return "";
  };
  const controls = () => [...document.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select")]
    .filter(visible)
    .filter(control => !["submit", "button", "hidden"].includes(control.type));
  // These are exact first-step labels observed on public Luma event pages.
  // Final submit labels are intentionally absent from this allowlist.
  const OPENING_CTA_LABELS = new Set([
    "request to join", "apply", "register", "ansÃ¶k om att gÃ¥ med",
  ]);
  const firstStepCtas = () => [...document.querySelectorAll("button, a, [role=button]")]
    .filter(visible)
    .filter(element => OPENING_CTA_LABELS.has(normalize(element.innerText || element.getAttribute("aria-label"))))
    .filter(element => !element.closest("form, [role=dialog]"))
    .filter(element => element.getAttribute("type") !== "submit" && !element.hasAttribute("form"))
    .filter(element => element.getAttribute("aria-disabled") !== "true" && !element.disabled);
  const matches = (control, question) => {
    const descriptor = labelFor(control);
    const questionLabel = normalize(question.label || question.name);
    const kind = fieldKind(question);
    if (descriptor.includes(questionLabel) || questionLabel.includes(normalize(control.name))) return true;
    if (kind === "email") return control.type === "email" || /(email|e-post|epost)/.test(descriptor);
    if (kind === "name") return /(name|namn)/.test(descriptor) || (control.type === "text" && controls().filter(item => item.type === "text").length === 1);
    if (kind === "phone") return control.type === "tel" || /(phone|telefon|mobile)/.test(descriptor);
    return false;
  };
  const setValue = (element, value) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set
      || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set
      || Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    if (setter) setter.call(element, value); else element.value = value;
    element.dispatchEvent(new Event("input", {bubbles:true}));
    element.dispatchEvent(new Event("change", {bubbles:true}));
    element.dispatchEvent(new Event("blur", {bubbles:true}));
    return element.value === value;
  };
  const fill = () => {
    if (!payload) return;
    if (lumaSessionPresent()) {
      safeMessage({type:"luma-session-present", status:{message:"LUMA_SESSION_PRESENT"}});
      report({message:"LUMA_SESSION_PRESENT â€” log out before continuing", level:"error"});
      return;
    }
    const key = `${payload.event_id}:${payload.tab_applicant_binding}`;
    if (filledKey === key) return;
    const activeControls = controls();
    const applicant = payload.applicant_name.split(" ")[0];
    if (!activeControls.length) {
      report({message:"Form not detected â€” waiting for Request to Join dialog", level:"warning", applicant});
      return;
    }
    const answerMap = new Map(payload.answers.map(answer => [normalize(answer.label), String(answer.answer ?? "")]));
    const unmatched = []; let filled = 0;
    for (const question of payload.questions) {
      const label = question.label || question.name || "Unnamed field";
      const answer = answerMap.get(normalize(label)) ?? String(question.answer ?? "");
      if (!answer) { if (question.required) unmatched.push(`${label}: answer missing`); continue; }
      const candidates = activeControls.filter(control => matches(control, question));
      if (candidates.length !== 1) { unmatched.push(`${label}: ${candidates.length ? "ambiguous" : "not found"}`); continue; }
      const control = candidates[0];
      if (control.tagName === "SELECT") {
        const option = [...control.options].find(item => normalize(item.text).includes(normalize(answer)) || normalize(item.value) === normalize(answer));
        if (!option) { unmatched.push(`${label}: option not found`); continue; }
        control.value = option.value;
        control.dispatchEvent(new Event("input", {bubbles:true}));
        control.dispatchEvent(new Event("change", {bubbles:true}));
        if (control.value !== option.value) { unmatched.push(`${label}: value not retained`); continue; }
      } else if (control.type === "checkbox") {
        if (["yes", "true", "1"].includes(normalize(answer)) && !control.checked) control.click();
        if (!["yes", "true", "1"].includes(normalize(answer)) || !control.checked) { unmatched.push(`${label}: checkbox not retained`); continue; }
      } else if (control.type === "radio") {
        const option = activeControls.find(item => item.type === "radio" && labelFor(item).includes(normalize(answer)));
        if (!option) { unmatched.push(`${label}: option not found`); continue; }
        option.click();
        if (!option.checked) { unmatched.push(`${label}: selection not retained`); continue; }
      } else if (!setValue(control, answer)) {
        unmatched.push(`${label}: value not retained`); continue;
      }
      filled++;
    }
    report({message: unmatched.length ? "Needs attention" : "Ready", level: unmatched.length ? "warning" : "success", applicant, filled, total: payload.questions.length, unmatched});
    if (!unmatched.length) {
      filledKey = key;
    }
  };
  const autoOpenFirstStep = () => {
    if (!payload || controls().length || ctaClickAttempted) return;
    const applicant = payload.applicant_name.split(" ")[0];
    const candidates = firstStepCtas();
    if (candidates.length !== 1) {
      report({message: candidates.length ? "Opening CTA ambiguous â€” click Request to Join manually" : "Opening CTA not found â€” click Request to Join manually", level:"warning", applicant});
      return;
    }
    const cta = candidates[0];
    ctaClickAttempted = true;
    console.info("HACKATHON_SEARCHER_OPENING_CTA_CLICK", {text: cta.innerText, url: location.href});
    report({message:`Opening ${cta.innerText} â€” waiting for application dialog`, applicant});
    // This is deliberately limited to an exact, non-submit CTA outside a
    // form/dialog. It is never used for the final application action.
    cta.click();
    setTimeout(() => {
      if (!controls().length && payload) {
        report({message:"Application dialog did not appear â€” review manually", level:"warning", applicant: payload.applicant_name.split(" ")[0]});
      }
    }, 1200);
  };
  const progress = () => {
    if (!payload) return;
    if (controls().length) fill(); else autoOpenFirstStep();
  };
  const scheduleProgress = () => {
    clearTimeout(timer);
    timer = setTimeout(progress, 150);
  };
  const start = () => {
    const params = new URLSearchParams(location.search);
    const eventId = params.get("hs_event");
    const applicantId = params.get("hs_applicant");
    if (!eventId) { report({message:"Event ID missing from tab URL", level:"error"}); return; }
    if (!applicantId) { report({message:"Applicant binding missing from tab URL", level:"error"}); return; }
    report({message:"Content script loaded â€” contacting local bridge", applicant: applicantId});
    try {
      chrome.runtime.sendMessage({type:"prepared-application", eventId, applicantId}, response => {
      if (chrome.runtime.lastError) { report({message:`Bridge unavailable: ${chrome.runtime.lastError.message}`, level:"error", applicant: applicantId}); return; }
      if (!response?.ok) { report({message:`Bridge unavailable: HTTP ${response?.status || "no response"}`, level:"error", applicant: applicantId}); return; }
      if (!response.data?.applicant_name || !response.data?.answers) { report({message:"Prepared applicant payload invalid", level:"error", applicant: applicantId}); return; }
      payload = response.data;
      console.info("HACKATHON_SEARCHER_PREPARED_PAYLOAD_RETURNED", {url: location.href, eventId, applicantId, applicant: payload.applicant_name});
      report({message:"Prepared payload loaded â€” waiting for form", applicant: payload.applicant_name.split(" ")[0]});
      scheduleProgress();
      });
    } catch (error) {
      report({message:"Extension reloaded â€” refresh this Luma tab", level:"error", applicant: applicantId});
    }
  };
  new MutationObserver(scheduleProgress).observe(document.documentElement, {childList:true, subtree:true});
  window.addEventListener("popstate", start);
  window.addEventListener("hashchange", start);
  start();
})();
