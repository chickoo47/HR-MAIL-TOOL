function setupDropzone(zoneId, inputId, labelId, accept) {
  const zone = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  const label = document.getElementById(labelId);

  zone.addEventListener("click", () => input.click());

  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("dragover");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      updateLabel();
    }
  });
  input.addEventListener("change", updateLabel);

  function updateLabel() {
    if (input.files.length) {
      label.textContent = input.files[0].name;
      zone.classList.add("has-file");
    }
  }

  return () => input.files[0];
}

const getResumeFile = setupDropzone("resume-drop", "resume-input", "resume-label");
const getRecipientsFile = setupDropzone("recipients-drop", "recipients-input", "recipients-label");

const form = document.getElementById("campaign-form");
const submitBtn = document.getElementById("submit-btn");
const errorBanner = document.getElementById("error-banner");
const progressSection = document.getElementById("progress-section");
const progressFill = document.getElementById("progress-fill");
const progressText = document.getElementById("progress-text");
const resultsBody = document.getElementById("results-body");

function showError(msg) {
  errorBanner.textContent = msg;
  errorBanner.hidden = false;
}

function clearError() {
  errorBanner.hidden = true;
  errorBanner.textContent = "";
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearError();

  const resumeFile = getResumeFile();
  const recipientsFile = getRecipientsFile();
  if (!resumeFile) return showError("Please attach your resume PDF.");
  if (!recipientsFile) return showError("Please attach a recipient list (.xlsx or .csv).");

  const fd = new FormData();
  fd.append("sender_name", document.getElementById("sender_name").value);
  fd.append("sender_email", document.getElementById("sender_email").value);
  fd.append("app_password", document.getElementById("app_password").value);
  fd.append("resume", resumeFile);
  fd.append("recipients", recipientsFile);

  submitBtn.disabled = true;
  submitBtn.textContent = "Starting...";

  try {
    const res = await fetch("/api/campaign/start", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to start campaign");

    document.getElementById("app_password").value = "";
    progressSection.hidden = false;
    resultsBody.innerHTML = "";
    pollStatus(data.job_id, data.total);
  } catch (err) {
    showError(err.message);
    submitBtn.disabled = false;
    submitBtn.textContent = "Send Applications";
  }
});

function pollStatus(jobId, total) {
  const seen = new Set();

  const timer = setInterval(async () => {
    const res = await fetch(`/api/campaign/${jobId}/status`);
    if (!res.ok) {
      clearInterval(timer);
      showError("Lost track of this job (it may have expired).");
      return;
    }
    const job = await res.json();

    const pct = total ? Math.round((job.done / total) * 100) : 0;
    progressFill.style.width = pct + "%";
    progressText.textContent = `${job.done} / ${total} processed`;

    for (const r of job.results) {
      const key = r.company + "|" + r.email;
      if (seen.has(key)) continue;
      seen.add(key);
      const row = document.createElement("tr");
      row.innerHTML = `<td>${escapeHtml(r.company)}</td><td>${escapeHtml(r.email)}</td>` +
        `<td class="status-${r.status}">${r.status === "sent" ? "Sent" : "Failed: " + escapeHtml(r.error || "")}</td>`;
      resultsBody.appendChild(row);
    }

    if (job.finished) {
      clearInterval(timer);
      submitBtn.disabled = false;
      submitBtn.textContent = "Send Applications";
      if (job.fatal_error) {
        showError("Campaign stopped early: " + job.fatal_error);
      }
    }
  }, 2000);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
