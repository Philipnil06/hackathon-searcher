chrome.storage.local.get(["lastStatus", "lastSubmission"], ({lastStatus, lastSubmission}) => {
  let text;
  if (!lastStatus) {
    text = "Open a prepared Luma application first.";
  } else if (lastStatus.warning) {
    text = `${lastStatus.warning}\nLog out of Luma in the dedicated guest profile before continuing.`;
  } else {
    text = `${lastStatus.event}\n${lastStatus.applicant}\n${lastStatus.filled}/${lastStatus.filled + lastStatus.unmatched.length} fields filled`;
    if (lastStatus.unmatched.length) {
      text += `\nNeeds attention: ${lastStatus.unmatched.join(", ")}`;
    } else if (lastSubmission) {
      text += lastSubmission.ok && lastSubmission.status === "APPLIED"
        ? "\nSubmitted and confirmed"
        : `\nSubmission: ${lastSubmission.status || "unknown"}${lastSubmission.error ? ` (${lastSubmission.error})` : ""}`;
    } else {
      text += "\nReady";
    }
  }
  document.getElementById("status").textContent = text;
});
