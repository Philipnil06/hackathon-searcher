chrome.storage.local.get("lastStatus", ({lastStatus}) => {
  document.getElementById("status").textContent = !lastStatus ? "Open a prepared Luma application first."
    : lastStatus.warning ? `${lastStatus.warning}\nLog out of Luma in the dedicated guest profile before continuing.`
    : `${lastStatus.event}\n${lastStatus.applicant}\n${lastStatus.filled}/${lastStatus.filled + lastStatus.unmatched.length} fields filled${lastStatus.unmatched.length ? `\nNeeds attention: ${lastStatus.unmatched.join(", ")}` : "\nReady for manual submission"}`;
});
