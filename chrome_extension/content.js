(() => {
  const STATUS_ID = "hackathon-searcher-status";
  // Luma can expose the same Swedish CTA with composed accents, decomposed
  // accents, or legacy mojibake depending on how the page was rendered.
  // Normalize to a stable comparison key before matching any label.
  const normalize = value => String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/ÃƒÂ¶|Ã¶/g, "o")
    .replace(/ÃƒÂ¥|Ã¥/g, "a")
    .replace(/ÃƒÂ¤|Ã¤/g, "a")
    .replace(/ÃƒÂ–|Ã–/g, "o")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
  const escapeRegExp = value => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const containsPhrase = (text, phrase) => {
    const normalizedText = normalize(text);
    const normalizedPhrase = normalize(phrase);
    if (!normalizedText || !normalizedPhrase) return false;
    // Single generic words need token boundaries: "name" must not match
    // "names" or "username" on another Luma field.
    if (!normalizedPhrase.includes(" ")) {
      return new RegExp(`(?:^|\\s)${escapeRegExp(normalizedPhrase)}(?:\\s|$)`).test(normalizedText);
    }
    return normalizedText === normalizedPhrase || normalizedText.includes(normalizedPhrase);
  };
  const visible = element => !!(element?.offsetWidth || element?.offsetHeight || element?.getClientRects().length);
  let payload = null;
  let filledKey = null;
  let timer = null;
  let ctaClickAttempted = false;
  let filling = false;
  let dropdownSelections = new Set();
  let dropdownFailures = new Set();
  let submitClicked = false;
  let submissionReported = false;
  let manualSubmissionInFlight = false;

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
    box.textContent = `Hackathon Searcher\n${applicant ? `${applicant} - ` : ""}${message}${total ? `\n${filled}/${total} fields filled` : ""}${unmatched.length ? `\nNeeds attention: ${unmatched.join(", ")}` : ""}`;
    Object.assign(box.style, {position:"fixed",right:"16px",bottom:"16px",zIndex:"2147483647",whiteSpace:"pre-line",padding:"10px 12px",maxWidth:"340px",background:colors[level],color:"#fff",borderRadius:"8px",font:"13px system-ui",boxShadow:"0 4px 18px #0006"});
    document.body.appendChild(box);
    safeMessage({type:"autofill-status", status:{event:payload?.event_name || "", applicant, filled, unmatched, message, level, lumaSession: lumaSessionPresent()}});
  };

  const lumaSessionPresent = () => /\b(log out|sign out)\b/i.test(document.body?.innerText || "");
  const labelFor = control => {
    const id = control.id;
    const explicit = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
    // Luma renders its question text in a nearby .inner-wrapper rather than
    // consistently using <label for=...>. Do not fall back to the whole dialog:
    // that makes every control appear to match every question.
    let nearby = control.closest("label, .inner-wrapper, .lux-input-wrapper, fieldset")?.innerText || "";
    // Some Luma builds omit the wrapper class on the first and lower fields.
    // Walk only a few ancestors and accept a container that owns this one
    // control, which keeps the label local without falling back to the whole
    // dialog.
    if (!nearby) {
      let ancestor = control.parentElement;
      for (let depth = 0; ancestor && depth < 6; depth += 1, ancestor = ancestor.parentElement) {
        const owned = ancestor.querySelectorAll("input:not([type=hidden]), textarea, select");
        if (owned.length === 1 && (ancestor.innerText || "").trim()) {
          nearby = ancestor.innerText;
          break;
        }
      }
    }
    return normalize([explicit?.innerText, nearby, control.getAttribute("aria-label"), control.name, control.placeholder]
      .filter(Boolean).join(" "));
  };
  const fieldKind = question => {
    const descriptor = normalize(`${question.label || question.name || ""} ${question.name || ""} ${question.field_type || ""}`);
    if (/(email|e-post|epost)/.test(descriptor)) return "email";
    if (/(^| )(name|namn|full name|fullstandigt namn)( |$)/.test(descriptor)) return "name";
    if (/(phone|telefon|mobile)/.test(descriptor)) return "phone";
    return "";
  };
  const controls = () => [...document.querySelectorAll("input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select")]
    .filter(visible)
    .filter(control => !["submit", "button", "hidden"].includes(control.type));
  // These are exact first-step labels observed on public Luma event pages.
  // Final submit labels are intentionally absent from this allowlist.
  const OPENING_CTA_LABELS = new Set([
    "request to join", "apply", "register", "ansok om att ga med",
  ]);
  const firstStepCtas = () => [...document.querySelectorAll("button, a, [role=button]")]
    .filter(visible)
    .filter(element => [element.innerText, element.textContent, element.getAttribute("aria-label")]
      .some(label => OPENING_CTA_LABELS.has(normalize(label))))
    .filter(element => !element.closest("form, [role=dialog]"))
    .filter(element => element.getAttribute("type") !== "submit" && !element.hasAttribute("form"))
    .filter(element => element.getAttribute("aria-disabled") !== "true" && !element.disabled);
  const matches = (control, question) => {
    const descriptor = labelFor(control);
    const questionLabel = normalize(question.label || question.name);
    const kind = fieldKind(question);
    const controlName = normalize(control.name || "");
    // Luma's consent checkbox is labelled in Swedish while the saved
    // question is English. Its field type is the reliable identity here.
    if (question.field_type === "checkbox") return control.type === "checkbox";
    // Luma translates the required identity field to "Namn". Resolve this
    // question from the field's own label only; generic substring matching
    // would also classify "username" or the teammate question's "names" as
    // the applicant name.
    if (kind === "name") {
      const localLabel = normalize(control.closest(".inner-wrapper, .lux-input-wrapper, fieldset, label")?.querySelector("label")?.innerText || "");
      return /^(name|namn)(?:\s|$)/.test(localLabel);
    }
    if (questionLabel && containsPhrase(descriptor, questionLabel)) return true;
    // An empty name matches every string in JavaScript, so only use the
    // question/name fallback when the DOM actually provides a name.
    if (questionLabel && controlName && containsPhrase(questionLabel, controlName)) return true;
    if (kind === "email") return control.type === "email" || /(email|e-post|epost)/.test(descriptor);
    if (kind === "phone") return control.type === "tel" || /(phone|telefon|mobile)/.test(descriptor);
    return false;
  };
  const isDropdownQuestion = question => {
    const descriptor = normalize(`${question.label || question.name || ""} ${question.placeholder || ""}`);
    return question.field_type === "dropdown"
      || /how did you hear|select an option|valj ett alternativ/.test(descriptor);
  };
  const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const selectLumaDropdownOption = async (control, answer, label = "") => {
    control.click();
    const answerKey = normalize(answer);
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await wait(75);
      const options = [...document.querySelectorAll('[role="tooltip"] .lux-menu-item, [role="option"], .lux-menu-item')]
        .filter(visible);
      let option = options.find(item => normalize(item.innerText) === answerKey || normalize(item.innerText).includes(answerKey));
      if (!option && answerKey.includes("hackathonhub")) {
        option = options.find(item => normalize(item.innerText) === "other");
      }
      // Saved answers for custom Luma dropdowns can be prose because the
      // original discovery page did not expose the option list. Resolve only
      // deterministic, profile-grounded mappings and only when that option is
      // actually present in the open menu.
      if (!option) {
        const labelKey = normalize(label);
        let fallback = "";
        if (/current role|professional role|employment|profession/.test(labelKey)) fallback = "other";
        else if (/team already|coming with a team|applying as a team/.test(labelKey)) fallback = "yes";
        else if (/experience level|experience building|experience.*apps/.test(labelKey)) fallback = "experienced founder builder";
        else if (/challenge preference/.test(labelKey)) fallback = "no preference";
        if (fallback) {
          option = options.find(item => normalize(item.innerText) === fallback || normalize(item.innerText).includes(fallback));
        }
      }
      if (option) {
        option.click();
        for (let check = 0; check < 6; check += 1) {
          await wait(75);
          const selected = normalize(control.value || "");
          if (selected && selected !== normalize(control.getAttribute("placeholder") || "välj ett alternativ") && selected !== "select an option") {
            return true;
          }
        }
        return false;
      }
    }
    document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    return false;
  };
  const setValue = (element, value) => {
    const prototype = element instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : element instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    if (setter) setter.call(element, value); else element.value = value;
    element.dispatchEvent(new Event("input", {bubbles:true}));
    element.dispatchEvent(new Event("change", {bubbles:true}));
    element.dispatchEvent(new Event("blur", {bubbles:true}));
    return element.value === value;
  };

  // --- Final submission (enabled only when the bridge sets auto_submit) ---
  const FINAL_SUBMIT_LABELS = new Set([
    "submit", "submit application", "send application", "send", "apply",
    "apply to join", "request to join", "complete registration", "finish",
    "submit registration", "confirm and submit", "complete application",
    "skicka", "skicka ansokan", "ansok om att ga med", "slutfor", "bekrafta",
  ]);
  const NON_FINAL_LABELS = new Set([
    "next", "continue", "back", "cancel", "skip", "previous", "close",
    "nasta", "fortsatt", "avbryt", "hoppa over", "stang",
  ]);
  const finalSubmitCtas = () => [...document.querySelectorAll("button, a, [role=button], input[type=submit]")]
    .filter(visible)
    .filter(element => element.closest("form, [role=dialog]"))
    .filter(element => {
      const label = normalize([element.innerText, element.textContent, element.getAttribute("aria-label"), element.getAttribute("value")].join(" "));
      if (NON_FINAL_LABELS.has(label)) return false;
      if ((element.getAttribute("type") || "").toLowerCase() === "submit") return true;
      return FINAL_SUBMIT_LABELS.has(label);
    });
  const CONFIRMATION_PHRASES = [
    "thank you for applying", "application received", "registration complete",
    "application submitted", "you're registered", "we've received your application",
    "application confirmed", "successfully registered", "your application has been",
    "thanks for applying", "submission received", "you are registered",
    "registration confirmed",
    "tack for din ansokan", "ansokan mottagen", "ansokan skickad",
    "registrering klar", "du ar registrerad", "din ansokan har skickats",
    "registrering bekraftad", "tack for att du ansokte",
    // Luma "Request to Join" events: after a successful request the page
    // switches to a pending-approval state instead of a classic thank-you.
    "vantar pa godkannande", "vi meddelar dig nar varden godkanner",
    "meddelar dig nar varden godkanner", "waiting for approval",
    "pending approval", "awaiting host approval",
    "we'll notify you when the host", "you'll receive an email when the host",
    // Luma rejects a second request with "already registered": the
    // registration demonstrably exists, so treat it as a confirmed apply.
    "du har redan registrerat dig", "redan registrerat dig for detta evenemang",
    "you have already registered", "you're already registered",
    "already registered for this event",
  ];
  // Per-user approval-state phrases: these appear only on the page for a
  // visitor who already has a pending registration. The generic "approval
  // required" description text is visible to everyone and must not be used
  // to conclude that an application was submitted.
  const APPROVAL_PHRASES = [
    "vantar pa godkannande", "vi meddelar dig nar varden godkanner",
    "meddelar dig nar varden godkanner", "waiting for approval",
    "pending approval", "awaiting host approval",
    "du har redan registrerat dig", "redan registrerat dig for detta evenemang",
    "you have already registered", "you're already registered",
    "already registered for this event",
  ];
  const detectConfirmation = () => {
    const rawText = String(document.body?.innerText || "");
    const bodyLower = rawText.toLowerCase();
    const normalizedBody = normalize(rawText);
    let reference = "";
    const refMatch = rawText.match(/(?:application|reference|confirmation|registration)\s*(?:#|id|number|code)[:\s]*([A-Za-z0-9_-]{4,})/i);
    if (refMatch) reference = refMatch[1];
    // Luma attaches a ticket key (tk=) to the visitor's own event URL only
    // after a successful request/registration. Use it as the reference id.
    const tkParam = new URLSearchParams(location.search).get("tk");
    if (!reference && tkParam) reference = tkParam;
    for (const phrase of CONFIRMATION_PHRASES) {
      const key = normalize(phrase);
      if (key && normalizedBody.includes(key)) {
        const idx = normalizedBody.indexOf(key);
        const snippet = bodyLower.slice(Math.max(0, idx - 80), idx + phrase.length + 240).trim();
        return { confirmed: true, text: snippet, reference };
      }
    }
    const urlLower = location.href.toLowerCase();
    for (const indicator of ["success", "confirm", "thank", "complete", "done", "submitted"]) {
      if (urlLower.includes(indicator)) {
        return { confirmed: true, text: rawText.slice(0, 500), reference };
      }
    }
    return { confirmed: false, text: "", reference };
  };
  const submissionResultMessage = outcome => ({
    type: "submission-result",
    result: {
      event_id: payload?.event_id || "",
      applicant_id: payload?.tab_applicant_binding || "",
      applicant_name: payload?.applicant_name || "",
      status: outcome.status,
      confirmation_text: outcome.confirmation_text || "",
      confirmation_url: outcome.confirmation_url || location.href,
      confirmation_reference: outcome.confirmation_reference || "",
      source: outcome.source || "",
    },
  });
  const waitForSubmissionOutcome = async (preClickUrl, submitButton) => {
    const timeoutMs = Number(payload.confirmation_timeout_ms) || 9000;
    const start = Date.now();
    let screenChanged = false;
    while (Date.now() - start < timeoutMs) {
      await wait(250);
      const confirmation = detectConfirmation();
      if (confirmation.confirmed) {
        return { status: "APPLIED", confirmation_text: confirmation.text, confirmation_reference: confirmation.reference };
      }
      const buttonGone = !submitButton?.isConnected || !visible(submitButton);
      if (location.href !== preClickUrl || buttonGone) screenChanged = true;
    }
    const last = detectConfirmation();
    return {
      status: screenChanged ? "MANUALLY_SUBMITTED" : "SUBMISSION_STATUS_UNKNOWN",
      confirmation_text: last.text || (screenChanged ? "Manual submit click detected and the Luma screen changed." : ""),
      confirmation_reference: last.reference,
    };
  };

  const observeManualSubmitClick = async event => {
    if (!payload || payload.auto_submit || !filledKey || submissionReported || manualSubmissionInFlight) return;
    const source = event.target instanceof Element
      ? event.target.closest("button, a, [role=button], input[type=submit]")
      : null;
    if (!source || !source.closest("form, [role=dialog]")) return;
    const candidates = finalSubmitCtas();
    if (!candidates.includes(source)) return;
    manualSubmissionInFlight = true;
    const applicant = payload.applicant_name.split(" ")[0];
    const preClickUrl = location.href;
    report({message:"Manual submit click detected - checking Luma result", applicant});
    const outcome = await waitForSubmissionOutcome(preClickUrl, source);
    if (outcome.status === "SUBMISSION_STATUS_UNKNOWN") {
      manualSubmissionInFlight = false;
      report({message:"Click detected, but Luma did not confirm a screen change", level:"warning", applicant});
      return;
    }
    outcome.source = "manual-click";
    submissionReported = true;
    safeMessage(submissionResultMessage(outcome));
    report({message: outcome.status === "APPLIED" ? "Application confirmed and saved" : "Application click recorded and saved", level:"success", applicant});
  };
  const attemptAutoSubmit = async () => {
    if (!payload || submitClicked || submissionReported) return;
    const applicant = payload.applicant_name.split(" ")[0];
    const candidates = finalSubmitCtas();
    if (candidates.length !== 1) {
      report({message: candidates.length ? "Final submit ambiguous - review manually" : "Final submit button not found - review manually", level: "warning", applicant});
      return;
    }
    const submitButton = candidates[0];
    submitClicked = true;
    const preClickUrl = location.href;
    console.info("HACKATHON_SEARCHER_FINAL_SUBMIT_CLICK", {text: submitButton.innerText, url: location.href});
    report({message: `Submitting - clicking "${(submitButton.innerText || "submit").trim()}"`, applicant});
    // Report the click BEFORE issuing it: a native form submit can navigate
    // synchronously and destroy this context before any later statement runs.
    // The bridge records SUBMISSION_STATUS_UNKNOWN (duplicate-safe) and
    // upgrades it to APPLIED once the confirmation report arrives.
    safeMessage(submissionResultMessage({status: "SUBMISSION_STATUS_UNKNOWN", confirmation_text: "Submit button clicked; awaiting confirmation", confirmation_reference: new URLSearchParams(location.search).get("tk") || ""}));
    try {
      submitButton.click();
    } catch (error) {
      submitClicked = false;
      report({message: "Submit click failed - review manually", level: "error", applicant});
      return;
    }
    const outcome = await waitForSubmissionOutcome(preClickUrl, submitButton);
    submitClicked = false;
    submissionReported = true;
    safeMessage(submissionResultMessage(outcome));
    if (outcome.status === "APPLIED") {
      report({message: "Application submitted and confirmed", level: "success", applicant});
      if (payload.auto_submit) safeMessage({type: "submission-result-close-tab"});
    } else {
      report({message: "Submitted but confirmation not detected - check Luma/email before retrying", level: "warning", applicant});
    }
  };

  const fill = async () => {
    if (!payload) return;
    if (filling) return;
    if (lumaSessionPresent()) {
      safeMessage({type:"luma-session-present", status:{message:"LUMA_SESSION_PRESENT"}});
      report({message:"LUMA_SESSION_PRESENT - log out before continuing", level:"error"});
      return;
    }
    const key = `${payload.event_id}:${payload.tab_applicant_binding}`;
    if (filledKey === key) return;
    filling = true;
    try {
      const activeControls = controls();
      const applicant = payload.applicant_name.split(" ")[0];
      if (!activeControls.length) {
        report({message:"Form not detected - waiting for Request to Join dialog", level:"warning", applicant});
        return;
      }
      const answerMap = new Map(payload.answers.map(answer => [normalize(answer.label), String(answer.answer ?? "")]));
      const unmatched = []; let filled = 0;
      for (const question of payload.questions) {
        const label = question.label || question.name || "Unnamed field";
        const answer = answerMap.get(normalize(label)) ?? String(question.answer ?? "");
        const candidates = activeControls.filter(control => matches(control, question));
        if (candidates.length !== 1) { unmatched.push(`${label}: ${candidates.length ? "ambiguous" : "not found"}`); continue; }
        const control = candidates[0];
        const visibleLabel = normalize(labelFor(control));
        const required = !!question.required || label.includes("*") || visibleLabel.includes("*");
        if (question.field_type === "checkbox") {
          const mustCheck = [answer, visibleLabel].some(value => /^(yes|true|1)$/.test(normalize(value)))
            || /consent|agree|terms|privacy|villkor|godkanner/.test(visibleLabel)
            || required;
          if (!mustCheck) continue;
          if (!control.checked) control.click();
          if (!control.checked) unmatched.push(`${label || visibleLabel}: checkbox not retained`);
          else filled++;
          continue;
        }
        if (!answer || answer === "UNKNOWN_REQUIRED_FIELD") {
          if (required) unmatched.push(`${label}: answer missing`);
          continue;
        }
        if (isDropdownQuestion(question) && control.tagName !== "SELECT") {
          // Luma mounts the floating menu asynchronously after the click.
          const dropdownKey = `${key}:${normalize(label)}`;
          const currentValue = normalize(control.value || "");
          if (dropdownSelections.has(dropdownKey) || currentValue === normalize(answer) || (answer.toLowerCase().includes("hackathonhub") && currentValue === "other")) {
            filled++;
            continue;
          }
          if (dropdownFailures.has(dropdownKey)) {
            unmatched.push(`${label}: option not found`);
            continue;
          }
          if (!await selectLumaDropdownOption(control, answer, label)) {
            dropdownFailures.add(dropdownKey);
            unmatched.push(`${label}: option not found`); continue;
          }
          dropdownSelections.add(dropdownKey);
        } else if (control.tagName === "SELECT") {
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
        } else if (String(control.value || "") === answer) {
          // Avoid dispatching new React input events for already-filled fields.
          filled++;
          continue;
        } else if (!setValue(control, answer)) {
          unmatched.push(`${label}: value not retained`); continue;
        }
        filled++;
      }
      report({message: unmatched.length ? "Needs attention" : "Ready", level: unmatched.length ? "warning" : "success", applicant, filled, total: payload.questions.length, unmatched});
      if (!unmatched.length) {
        filledKey = key;
        if (payload.auto_submit) {
          await attemptAutoSubmit();
        }
      }
    } finally {
      filling = false;
    }
  };
  const autoOpenFirstStep = () => {
    if (!payload || controls().length || ctaClickAttempted) return;
    const applicant = payload.applicant_name.split(" ")[0];
    const candidates = firstStepCtas();
    if (candidates.length !== 1) {
      report({message: candidates.length ? "Opening CTA ambiguous - click Request to Join manually" : "Opening CTA not found - click Request to Join manually", level:"warning", applicant});
      return;
    }
    const cta = candidates[0];
    ctaClickAttempted = true;
    console.info("HACKATHON_SEARCHER_OPENING_CTA_CLICK", {text: cta.innerText, url: location.href});
    report({message:`Opening ${cta.innerText} - waiting for application dialog`, applicant});
    // This is deliberately limited to an exact, non-submit CTA outside a
    // form/dialog. It is never used for the final application action.
    cta.click();
    setTimeout(() => {
      if (!controls().length && payload) {
        report({message:"Application dialog did not appear - review manually", level:"warning", applicant: payload.applicant_name.split(" ")[0]});
      }
    }, 1200);
  };
  // A reloaded tab whose request already succeeded must never re-open the
  // form and re-submit. The approval-state text plus a ticket key (or a click
  // we just issued in this session) proves the submission already happened.
  const alreadyApplied = () => {
    if (!payload) return false;
    const key = normalize(String(document.body?.innerText || ""));
    if (!APPROVAL_PHRASES.some(phrase => key.includes(normalize(phrase)))) return false;
    return new URLSearchParams(location.search).has("tk") || submitClicked;
  };
  const progress = () => {
    if (!payload) return;
    if (alreadyApplied()) {
      if (!submissionReported) {
        submissionReported = true;
        const confirmation = detectConfirmation();
        const applicant = payload.applicant_name.split(" ")[0];
        safeMessage(submissionResultMessage({status: "APPLIED", confirmation_text: confirmation.text || "Luma approval state detected", confirmation_reference: confirmation.reference, source: "approval-state"}));
        report({message: "Already applied - approval pending", level: "success", applicant});
        if (payload.auto_submit) safeMessage({type: "submission-result-close-tab"});
      }
      return;
    }
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
    report({message:"Content script loaded - contacting local bridge", applicant: applicantId});
    try {
      chrome.runtime.sendMessage({type:"prepared-application", eventId, applicantId}, response => {
        if (chrome.runtime.lastError) { report({message:`Bridge unavailable: ${chrome.runtime.lastError.message}`, level:"error", applicant: applicantId}); return; }
        if (!response?.ok) { report({message:`Bridge unavailable: HTTP ${response?.status || "no response"}`, level:"error", applicant: applicantId}); return; }
        if (!response.data?.applicant_name || !response.data?.answers) { report({message:"Prepared applicant payload invalid", level:"error", applicant: applicantId}); return; }
        payload = response.data;
        dropdownSelections = new Set();
        dropdownFailures = new Set();
        console.info("HACKATHON_SEARCHER_PREPARED_PAYLOAD_RETURNED", {url: location.href, eventId, applicantId, applicant: payload.applicant_name});
        report({message: payload.auto_submit ? "Prepared payload loaded - will fill and submit automatically" : "Prepared payload loaded - waiting for form", applicant: payload.applicant_name.split(" ")[0]});
        scheduleProgress();
      });
    } catch (error) {
      report({message:"Extension reloaded - refresh this Luma tab", level:"error", applicant: applicantId});
    }
  };
  const statusOnlyMutation = mutation => {
    const target = mutation.target?.nodeType === 1 ? mutation.target : mutation.target?.parentElement;
    if (target?.closest?.(`#${STATUS_ID}`)) return true;
    const nodes = [...mutation.addedNodes, ...mutation.removedNodes]
      .filter(node => node.nodeType === 1);
    return nodes.length > 0 && nodes.every(node => node.id === STATUS_ID || node.closest?.(`#${STATUS_ID}`));
  };
  new MutationObserver(mutations => {
    if (mutations.some(mutation => !statusOnlyMutation(mutation))) scheduleProgress();
  }).observe(document.documentElement, {childList:true, subtree:true});
  document.addEventListener("click", event => { void observeManualSubmitClick(event); }, true);
  window.addEventListener("popstate", start);
  window.addEventListener("hashchange", start);
  start();
})();
