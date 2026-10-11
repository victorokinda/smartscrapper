let currentJobId = null;
let currentProjectId = null;
let recordsCache = [];

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) return;
    const data = await res.json();

    const dot = document.getElementById("status-dot");
    const statusText = document.getElementById("model-status-text");

    let statusMsg = `Backend: ${data.active_backend}`;
    if (data.resident_model) {
      statusMsg += ` | Resident: ${data.resident_model}`;
    }
    if (data.current_role && data.current_role !== "idle") {
      statusMsg += ` (${data.current_role})`;
      dot.className = "status-dot pulsing warning";
    } else {
      dot.className = "status-dot";
    }
    statusText.textContent = statusMsg;

    // Check throttling
    let isThrottled = false;
    for (const [orig, info] of Object.entries(data.limiter || {})) {
      if (info.in_cooldown || info.is_blocked) {
        isThrottled = true;
        break;
      }
    }
    const throtEl = document.getElementById("throttling-indicator");
    throtEl.style.display = isThrottled ? "block" : "none";

    // Update active job if running
    if (data.active_job) {
      currentJobId = data.active_job.id;
      currentProjectId = data.active_job.project_id;
      updateJobUI(data.active_job);
    }
  } catch (err) {
    console.error("Status fetch error", err);
  }
}

async function loadProjects() {
  try {
    const res = await fetch("/api/projects");
    if (!res.ok) return;
    const projects = await res.json();
    const sel = document.getElementById("project-select");
    const currentVal = sel.value;
    sel.innerHTML = '<option value="">-- New Project --</option>';
    projects.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = `${p.name} (${p.id})`;
      sel.appendChild(opt);
    });
    sel.value = currentVal;
  } catch (err) {
    console.error("Load projects error", err);
  }
}

document.getElementById("project-select").addEventListener("change", async (e) => {
  const pId = e.target.value;
  if (!pId) {
    currentProjectId = null;
    document.getElementById("output-dir-preview").textContent = "";
    return;
  }
  try {
    const res = await fetch(`/api/projects/${pId}`);
    if (!res.ok) return;
    const data = await res.json();
    const p = data.project;
    currentProjectId = p.id;
    document.getElementById("project-name").value = p.name;
    document.getElementById("source-url").value = p.source_url;
    document.getElementById("instructions").value = p.instructions;
    document.getElementById("output-dir-preview").textContent = p.output_dir;

    if (data.latest_job) {
      currentJobId = data.latest_job.id;
      updateJobUI(data.latest_job);
      fetchJobDetails();
    }
  } catch (err) {
    console.error("Project select error", err);
  }
});

function updateJobUI(job) {
  document.getElementById("job-stage-badge").textContent = job.current_stage || job.status;
  document.getElementById("classifier-mode").value = job.mode;

  const isRunning = job.status === "running";
  document.getElementById("btn-start").disabled = isRunning;
  document.getElementById("btn-cancel").disabled = !isRunning;
  document.getElementById("btn-resume").disabled = isRunning || (job.status !== "cancelled" && job.status !== "failed");

  // Update stats
  const s = job.stats || {};
  document.getElementById("stat-listing-pages").textContent = s.listing_pages_visited || 0;
  document.getElementById("stat-detail-pages").textContent = s.detail_pages_visited || 0;
  document.getElementById("stat-found").textContent = s.submissions_found || 0;
  document.getElementById("stat-selected").textContent = s.submissions_selected || 0;
  document.getElementById("stat-rejected").textContent = s.submissions_rejected || 0;
  document.getElementById("stat-uncertain").textContent = s.submissions_uncertain || 0;
  document.getElementById("stat-downloaded").textContent = s.attachments_downloaded || 0;
  document.getElementById("stat-reused").textContent = s.attachments_reused || 0;

  // Manifest links
  const csvBtn = document.getElementById("btn-export-csv");
  const jsonBtn = document.getElementById("btn-export-json");
  const dlApprovedBtn = document.getElementById("btn-download-approved");
  if (job.id) {
    csvBtn.href = `/api/jobs/${job.id}/manifest.csv`;
    csvBtn.style.display = "inline-block";
    jsonBtn.href = `/api/jobs/${job.id}/manifest.json`;
    jsonBtn.style.display = "inline-block";
    dlApprovedBtn.style.display = "inline-block";
  }
}

async function fetchJobDetails() {
  if (!currentJobId) return;
  try {
    const res = await fetch(`/api/jobs/${currentJobId}`);
    if (!res.ok) return;
    const data = await res.json();
    updateJobUI(data.job);

    // Logs
    const logBox = document.getElementById("log-box");
    if (data.events && data.events.length > 0) {
      logBox.innerHTML = data.events.map(ev => {
        const time = ev.timestamp ? ev.timestamp.substring(11, 19) : "";
        return `<div class="log-item ${ev.level}">[${time}] ${escapeHtml(ev.message)}</div>`;
      }).join("");
      logBox.scrollTop = logBox.scrollHeight;
    }

    // Records
    recordsCache = data.records || [];
    renderRecords();
  } catch (err) {
    console.error("Fetch job details error", err);
  }
}

function renderRecords() {
  const filter = document.getElementById("filter-status").value;
  const tbody = document.getElementById("records-tbody");
  const countLabel = document.getElementById("record-count-label");

  let filtered = recordsCache;
  if (filter === "select") {
    filtered = recordsCache.filter(r => r.decision === "select" || r.status === "manual_included");
  } else if (filter === "reject") {
    filtered = recordsCache.filter(r => r.decision === "reject" || r.status === "manual_excluded");
  } else if (filter === "uncertain") {
    filtered = recordsCache.filter(r => r.decision === "uncertain" || r.decision === "needs_detail");
  }

  countLabel.textContent = `${filtered.length} of ${recordsCache.length} records`;

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 24px;">No records match the current filter.</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map(r => {
    let tagClass = "tag-uncertain";
    let badgeText = r.decision || "Pending";
    if (r.status === "manual_included") {
      tagClass = "tag-manual";
      badgeText = "Manual Include";
    } else if (r.status === "manual_excluded") {
      tagClass = "tag-manual";
      badgeText = "Manual Exclude";
    } else if (r.decision === "select") {
      tagClass = "tag-select";
    } else if (r.decision === "reject") {
      tagClass = "tag-reject";
    } else if (r.decision === "needs_detail") {
      tagClass = "tag-needs_detail";
    }

    // Attachments rendering
    let attHtml = "-";
    if (r.attachments && r.attachments.length > 0) {
      attHtml = r.attachments.map(a => {
        let fileLink = escapeHtml(a.original_name);
        if (a.download_status === "downloaded" || a.download_status === "reused") {
          fileLink = `<a class="file-link" href="/api/files/${currentProjectId}/${encodeURIComponent(a.original_name)}" target="_blank" download>${escapeHtml(a.original_name)}</a>`;
        }
        return `<div>${fileLink} <span style="font-size:0.75rem; color:var(--text-muted);">(${a.download_status})</span></div>`;
      }).join("");
    }

    // Actions (include / exclude for uncertain/review items)
    let actionHtml = "-";
    if (r.decision === "uncertain" || r.status === "manual_included" || r.status === "manual_excluded") {
      actionHtml = `
        <button class="btn-secondary action-btn-sm" onclick="handleRecordAction(${r.id}, 'include')">Include</button>
        <button class="btn-secondary action-btn-sm" onclick="handleRecordAction(${r.id}, 'exclude')">Exclude</button>
      `;
    }

    return `
      <tr>
        <td style="font-family:monospace; font-size:0.8rem;">${escapeHtml(r.record_id)}</td>
        <td><strong>${escapeHtml(r.submitter || "Anonymous")}</strong>${r.organization ? `<br><small style="color:var(--text-muted);">${escapeHtml(r.organization)}</small>` : ""}</td>
        <td>${escapeHtml(r.title || r.proceeding || "-")}</td>
        <td><span class="tag ${tagClass}">${badgeText}</span></td>
        <td style="font-size:0.82rem; color:var(--text-muted);">${escapeHtml(r.reason || "-")}</td>
        <td>${attHtml}</td>
        <td>${actionHtml}</td>
      </tr>
    `;
  }).join("");
}

document.getElementById("filter-status").addEventListener("change", renderRecords);

async function handleRecordAction(recordId, action) {
  try {
    const res = await fetch(`/api/records/${recordId}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action })
    });
    if (res.ok) {
      fetchJobDetails();
    }
  } catch (err) {
    console.error("Record action error", err);
  }
}

document.getElementById("btn-start").addEventListener("click", async () => {
  const name = document.getElementById("project-name").value.trim();
  const url = document.getElementById("source-url").value.trim();
  const instructions = document.getElementById("instructions").value.trim();
  const mode = document.getElementById("classifier-mode").value;

  if (!name || !url) {
    alert("Please provide a Project Name and Starting URL.");
    return;
  }

  try {
    const res = await fetch("/api/jobs/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_name: name,
        source_url: url,
        instructions: instructions,
        mode: mode,
        project_id: currentProjectId
      })
    });
    if (!res.ok) {
      const err = await res.json();
      alert(`Error starting job: ${err.detail || "Unknown error"}`);
      return;
    }
    const data = await res.json();
    currentJobId = data.job.id;
    currentProjectId = data.project.id;
    document.getElementById("output-dir-preview").textContent = data.project.output_dir;
    updateJobUI(data.job);
    loadProjects();
    fetchJobDetails();
  } catch (err) {
    console.error("Start job error", err);
  }
});

document.getElementById("btn-cancel").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
    fetchJobDetails();
  } catch (err) {
    console.error("Cancel job error", err);
  }
});

document.getElementById("btn-resume").addEventListener("click", async () => {
  if (!currentJobId) return;
  try {
    await fetch(`/api/jobs/${currentJobId}/resume`, { method: "POST" });
    fetchJobDetails();
  } catch (err) {
    console.error("Resume job error", err);
  }
});

document.getElementById("btn-download-approved").addEventListener("click", async () => {
  if (!currentJobId) return;
  const btn = document.getElementById("btn-download-approved");
  btn.disabled = true;
  btn.textContent = "Downloading...";
  try {
    const res = await fetch(`/api/jobs/${currentJobId}/download-approved`, { method: "POST" });
    if (res.ok) {
      setTimeout(fetchJobDetails, 600);
    }
  } catch (err) {
    console.error("Download approved error", err);
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Download Approved Rows";
      fetchJobDetails();
    }, 2500);
  }
});

// Polling intervals
fetchStatus();
loadProjects();
setInterval(fetchStatus, 2500);
setInterval(() => {
  if (currentJobId) {
    fetchJobDetails();
  }
}, 2500);
