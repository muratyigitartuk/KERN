import { escapeHTML, secureFetch } from "/static/js/utils.js";

function formatDate(value) {
  if (!value) return "Unknown time";
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(value));
  } catch {
    return String(value);
  }
}

async function fetchJson(url, options = {}) {
  const response = await secureFetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || payload.message || "Request failed.");
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function renderMetricGrid(element, cards = []) {
  if (!element) return;
  if (!cards.length) {
    element.innerHTML = "";
    return;
  }
  element.innerHTML = cards
    .map(
      (card) => `
        <article class="workbench-metric-card">
          <span>${escapeHTML(card.label || "")}</span>
          <strong>${escapeHTML(card.value || "0")}</strong>
          <small>${escapeHTML(card.detail || "")}</small>
        </article>
      `
    )
    .join("");
}

function rowMeta(parts) {
  const filtered = parts.filter(Boolean);
  if (!filtered.length) return "";
  return `<div class="workbench-inline-meta">${filtered.map((part) => `<span>${escapeHTML(part)}</span>`).join("")}</div>`;
}

function actionButtons(actions = []) {
  if (!actions.length) return "";
  return `
    <div class="workbench-action-row">
      ${actions
        .map(
          (action) => `
            <button
              type="button"
              class="${action.primary ? "solid-button" : "ghost-button"}"
              data-workbench-action="${escapeHTML(action.type)}"
              ${Object.entries(action.dataset || {})
                .map(([key, value]) => `data-${key}="${escapeHTML(value)}"`)
                .join(" ")}
            >${escapeHTML(action.label)}</button>
          `
        )
        .join("")}
    </div>
  `;
}

function renderList(element, items, renderItem, emptyText) {
  if (!element) return;
  if (!items?.length) {
    element.innerHTML = `
      <li class="detail-list__item--state">
        <div class="panel-state">
          <div class="panel-state__head">
            <span class="panel-state__pill">Empty</span>
            <strong class="panel-state__title">${escapeHTML(emptyText)}</strong>
          </div>
        </div>
      </li>
    `;
    return;
  }
  element.innerHTML = items.map(renderItem).join("");
}

function setStateCard(element, title, body, tone = "warning") {
  if (!element) return;
  element.classList.remove("panel-state--success", "panel-state--warning", "panel-state--error");
  element.classList.add(`panel-state--${tone}`);
  const titleEl = element.querySelector(".panel-state__title");
  const bodyEl = element.querySelector(".panel-state__body");
  if (titleEl) titleEl.textContent = title;
  if (bodyEl) bodyEl.textContent = body;
}

export function createWorkbenchController({ renderer }) {
  const state = {
    session: null,
    activeTab: "workspace",
  };

  const dom = {
    modal: document.getElementById("utilityModal"),
    tabs: [...document.querySelectorAll(".utility-tab")],
    workspaceSummaryState: document.getElementById("workspaceSummaryState"),
    workspaceSummaryTitle: document.getElementById("workspaceSummaryTitle"),
    workspaceSummaryBody: document.getElementById("workspaceSummaryBody"),
    workspaceMetricGrid: document.getElementById("workspaceMetricGrid"),
    workspaceAccessList: document.getElementById("workspaceAccessList"),
    adminAccessState: document.getElementById("adminAccessState"),
    adminMetricGrid: document.getElementById("adminMetricGrid"),
    adminWorkspaceList: document.getElementById("adminWorkspaceList"),
    adminPendingUsersList: document.getElementById("adminPendingUsersList"),
    adminUsersList: document.getElementById("adminUsersList"),
    adminSessionsList: document.getElementById("adminSessionsList"),
    adminWorkspaceSlug: document.getElementById("adminWorkspaceSlug"),
    adminWorkspaceTitle: document.getElementById("adminWorkspaceTitle"),
    adminUserEmail: document.getElementById("adminUserEmail"),
    adminUserDisplayName: document.getElementById("adminUserDisplayName"),
    adminUserRole: document.getElementById("adminUserRole"),
    adminUserWorkspace: document.getElementById("adminUserWorkspace"),
    complianceStateCard: document.getElementById("complianceStateCard"),
    complianceMetricGrid: document.getElementById("complianceMetricGrid"),
    complianceRetentionList: document.getElementById("complianceRetentionList"),
    complianceHoldList: document.getElementById("complianceHoldList"),
    complianceErasureList: document.getElementById("complianceErasureList"),
    complianceExportList: document.getElementById("complianceExportList"),
    complianceInventoryList: document.getElementById("complianceInventoryList"),
    retentionDataClass: document.getElementById("retentionDataClass"),
    retentionDays: document.getElementById("retentionDays"),
    retentionLegalHoldEnabled: document.getElementById("retentionLegalHoldEnabled"),
    legalHoldReason: document.getElementById("legalHoldReason"),
    legalHoldWorkspace: document.getElementById("legalHoldWorkspace"),
    selfErasureReason: document.getElementById("selfErasureReason"),
    regulatedCandidateList: document.getElementById("regulatedCandidateList"),
    regulatedDocumentWorkbenchList: document.getElementById("regulatedDocumentWorkbenchList"),
    intelligenceStateCard: document.getElementById("intelligenceStateCard"),
    intelligenceMetricGrid: document.getElementById("intelligenceMetricGrid"),
    recommendationList: document.getElementById("recommendationList"),
    obligationList: document.getElementById("obligationList"),
    workflowList: document.getElementById("workflowList"),
    decisionHistoryList: document.getElementById("decisionHistoryList"),
    promotionCandidatesList: document.getElementById("promotionCandidatesList"),
    trainingExamplesList: document.getElementById("trainingExamplesList"),
    trainingExportsList: document.getElementById("trainingExportsList"),
    evidenceStateCard: document.getElementById("evidenceStateCard"),
    evidenceMetricGrid: document.getElementById("evidenceMetricGrid"),
    evidenceManifestList: document.getElementById("evidenceManifestList"),
    evidenceTrainingManifestList: document.getElementById("evidenceTrainingManifestList"),
  };

  function currentRoles() {
    return state.session?.roles || [];
  }

  function hasRole(...roles) {
    if (state.session?.is_break_glass) return true;
    return roles.some((role) => currentRoles().includes(role));
  }

  function isMemberOnly() {
    return !!state.session && !hasRole("org_owner", "org_admin", "auditor");
  }

  function populateWorkspaceOptions(workspaces = []) {
    const selects = [dom.adminUserWorkspace, dom.legalHoldWorkspace].filter(Boolean);
    for (const select of selects) {
      const current = select.value;
      select.innerHTML = workspaces
        .map(
          (workspace) => `<option value="${escapeHTML(workspace.slug)}">${escapeHTML(workspace.title || workspace.slug)}</option>`
        )
        .join("");
      if (current && workspaces.some((workspace) => workspace.slug === current)) {
        select.value = current;
      } else if (state.session?.workspace_slug) {
        select.value = state.session.workspace_slug;
      }
    }
  }

  function applyRoleVisibility() {
    const roles = new Set(currentRoles());
    dom.tabs.forEach((tab) => {
      const allowed = String(tab.dataset.roleAllow || "")
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean);
      const visible = !allowed.length || state.session?.is_break_glass || allowed.some((role) => roles.has(role));
      tab.classList.toggle("is-role-hidden", !visible);
      tab.toggleAttribute("hidden", !visible);
    });
  }

  async function refreshSession() {
    state.session = await fetchJson("/auth/session");
    populateWorkspaceOptions(state.session.workspaces || []);
    applyRoleVisibility();
    if (dom.workspaceSummaryTitle) {
      dom.workspaceSummaryTitle.textContent = state.session.workspace_slug || "Workspace not selected";
    }
    if (dom.workspaceSummaryBody) {
      const email = state.session.user?.email || state.session.user_email || "anonymous";
      const roles = currentRoles().length ? currentRoles().join(", ") : "no workspace role";
      dom.workspaceSummaryBody.textContent = `${email} is operating in ${state.session.workspace_slug || "no workspace"} with roles: ${roles}.`;
    }
    return state.session;
  }

  async function renderWorkspace() {
    await refreshSession();
    const workspaces = state.session.workspaces || [];
    renderMetricGrid(dom.workspaceMetricGrid, [
      { label: "Workspaces", value: String(workspaces.length), detail: "Accessible from this session" },
      { label: "Selected", value: state.session.workspace_slug || "None", detail: state.session.auth_method || "session" },
      { label: "Roles", value: String(currentRoles().length || 0), detail: currentRoles().join(", ") || "No role assigned" },
      { label: "Sessions", value: String((state.session.sessions || []).length), detail: "Concurrent sessions for this actor" },
    ]);
    renderList(
      dom.workspaceAccessList,
      workspaces,
      (workspace) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(workspace.title || workspace.slug)}</strong>
            <span class="workbench-pill" data-tone="${workspace.slug === state.session.workspace_slug ? "success" : "warning"}">${workspace.slug === state.session.workspace_slug ? "active" : "available"}</span>
          </div>
          ${rowMeta([workspace.slug, workspace.profile_root])}
        </li>
      `,
      "No workspaces are assigned to this session."
    );
  }

  async function renderAdmin() {
    if (!hasRole("org_owner", "org_admin")) {
      setStateCard(dom.adminAccessState, "Admin access is not available.", "This account can review only its allowed surfaces in the current workspace.", "error");
      renderMetricGrid(dom.adminMetricGrid, []);
      [dom.adminWorkspaceList, dom.adminPendingUsersList, dom.adminUsersList, dom.adminSessionsList].forEach((element) =>
        renderList(element, [], () => "", "Not allowed for this role.")
      );
      return;
    }
    const [workspacesPayload, usersPayload, sessionsPayload] = await Promise.all([
      fetchJson("/admin/workspaces"),
      fetchJson("/admin/users"),
      fetchJson("/admin/sessions"),
    ]);
    const workspaces = workspacesPayload.items || workspacesPayload.workspaces || [];
    const users = usersPayload.items || usersPayload.users || [];
    const sessions = sessionsPayload.items || sessionsPayload.sessions || [];
    const pendingUsers = users.filter((user) => String(user.status || "").toLowerCase() === "pending");
    populateWorkspaceOptions(workspaces);
    renderMetricGrid(dom.adminMetricGrid, [
      { label: "Workspaces", value: String(workspaces.length), detail: "Owned by this organization" },
      { label: "Users", value: String(users.length), detail: "Provisioned identities" },
      { label: "Pending", value: String(pendingUsers.length), detail: "Awaiting approval" },
      { label: "Sessions", value: String(sessions.length), detail: "Open authenticated sessions" },
    ]);
    renderList(
      dom.adminWorkspaceList,
      workspaces,
      (workspace) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(workspace.title || workspace.slug)}</strong>
            <span class="workbench-pill" data-tone="${workspace.slug === state.session.workspace_slug ? "success" : "warning"}">${escapeHTML(workspace.slug)}</span>
          </div>
          ${rowMeta([workspace.db_path, workspace.backup_root])}
        </li>
      `,
      "No workspaces exist yet."
    );
    renderList(
      dom.adminPendingUsersList,
      pendingUsers,
      (user) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(user.display_name || user.email || user.id)}</strong>
            <span class="workbench-pill" data-tone="warning">${escapeHTML(user.status || "pending")}</span>
          </div>
          ${rowMeta([user.email, user.auth_source])}
          ${actionButtons([
            { type: "approve-user", label: "Approve", primary: true, dataset: { userId: user.id } },
            { type: "suspend-user", label: "Suspend", dataset: { userId: user.id } },
          ])}
        </li>
      `,
      "No pending user approvals."
    );
    renderList(
      dom.adminUsersList,
      users,
      (user) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(user.display_name || user.email || user.id)}</strong>
            <span class="workbench-pill" data-tone="${String(user.status).toLowerCase() === "active" ? "success" : "warning"}">${escapeHTML(user.status || "unknown")}</span>
          </div>
          ${rowMeta([user.email, user.auth_source])}
          ${String(user.status).toLowerCase() === "active" ? actionButtons([{ type: "suspend-user", label: "Suspend", dataset: { userId: user.id } }]) : ""}
        </li>
      `,
      "No users available."
    );
    renderList(
      dom.adminSessionsList,
      sessions,
      (session) => `
        <li>
          <div class="workbench-row-title">
            <strong>${escapeHTML(session.user_id || "service session")}</strong>
            <span class="workbench-pill" data-tone="${session.revoked_at ? "danger" : "success"}">${session.revoked_at ? "revoked" : "active"}</span>
          </div>
          ${rowMeta([session.workspace_slug, formatDate(session.last_activity_at), session.auth_method])}
          ${session.revoked_at ? "" : actionButtons([{ type: "revoke-session", label: "Revoke", dataset: { sessionId: session.id } }])}
        </li>
      `,
      "No active sessions found."
    );
  }

  async function renderCompliance() {
    await refreshSession();
    const selfServiceCards = [
      { label: "Scope", value: state.session.workspace_slug || "None", detail: "Current workspace context" },
      { label: "Actor", value: state.session.user?.email || state.session.user_email || "unknown", detail: "Signed-in account" },
    ];
    if (isMemberOnly()) {
      setStateCard(dom.complianceStateCard, "Self-service compliance only.", "This role can generate its own export and create an erasure request, but cannot administer holds or retention presets.", "warning");
      renderMetricGrid(dom.complianceMetricGrid, selfServiceCards);
      [dom.complianceRetentionList, dom.complianceHoldList, dom.complianceErasureList, dom.complianceExportList, dom.complianceInventoryList, dom.regulatedCandidateList, dom.regulatedDocumentWorkbenchList].forEach((element) =>
        renderList(element, [], () => "", "Admin review is required for this queue.")
      );
      return;
    }
    const [retentionPayload, holdsPayload, erasuresPayload, exportsPayload, inventoryPayload, candidatePayload, regulatedPayload] = await Promise.all([
      fetchJson("/compliance/retention-policies"),
      fetchJson("/compliance/legal-holds"),
      fetchJson("/compliance/erasure-requests"),
      fetchJson("/compliance/data-exports"),
      fetchJson("/compliance/data-inventory"),
      fetchJson("/compliance/regulated-documents/candidates"),
      fetchJson("/compliance/regulated-documents"),
    ]);
    const policies = retentionPayload.items || retentionPayload.policies || [];
    const holds = holdsPayload.items || holdsPayload.legal_holds || [];
    const erasures = erasuresPayload.items || erasuresPayload.erasure_requests || [];
    const exports = exportsPayload.items || exportsPayload.data_exports || [];
    const inventory = inventoryPayload.item || inventoryPayload.inventory || {};
    const candidates = candidatePayload.items || candidatePayload.regulated_document_candidates || [];
    const regulated = regulatedPayload.items || regulatedPayload.regulated_documents || [];
    renderMetricGrid(dom.complianceMetricGrid, [
      { label: "Retention presets", value: String(policies.length), detail: "By data class" },
      { label: "Legal holds", value: String(holds.filter((item) => item.active).length), detail: "Currently active" },
      { label: "Erasure queue", value: String(erasures.length), detail: "Requests under review" },
      { label: "Exports", value: String(exports.length), detail: "Evidence-bearing jobs" },
    ]);
    renderList(dom.complianceRetentionList, policies, (policy) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(policy.data_class)}</strong>
          <span class="workbench-pill" data-tone="${policy.legal_hold_enabled ? "warning" : "success"}">${policy.retention_days} days</span>
        </div>
        ${rowMeta([policy.legal_hold_enabled ? "Legal hold ready" : "No legal hold trigger", formatDate(policy.updated_at)])}
      </li>
    `, "No retention presets configured.");
    renderList(dom.complianceHoldList, holds, (hold) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(hold.reason || "Legal hold")}</strong>
          <span class="workbench-pill" data-tone="${hold.active ? "warning" : "success"}">${hold.active ? "active" : "released"}</span>
        </div>
        ${rowMeta([hold.workspace_slug, hold.target_user_id, formatDate(hold.created_at)])}
      </li>
    `, "No legal holds are active.");
    renderList(dom.complianceErasureList, erasures, (request) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(request.target_user_id)}</strong>
          <span class="workbench-pill" data-tone="${request.status === "completed" ? "success" : request.status === "blocked" ? "danger" : "warning"}">${escapeHTML(request.status || "requested")}</span>
        </div>
        ${rowMeta([request.workspace_slug, request.legal_hold_decision, request.retention_decision, formatDate(request.updated_at)])}
        ${request.status === "requested" ? actionButtons([{ type: "execute-erasure", label: "Execute", primary: true, dataset: { requestId: request.id } }]) : ""}
      </li>
    `, "No erasure requests in the queue.");
    renderList(dom.complianceExportList, exports, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.workspace_slug || item.target_user_id || item.id)}</strong>
          <span class="workbench-pill" data-tone="${item.status === "completed" ? "success" : "warning"}">${escapeHTML(item.status || "requested")}</span>
        </div>
        ${rowMeta([item.workspace_slug ? "workspace export" : "subject export", formatDate(item.updated_at)])}
        ${actionButtons([{ type: "inspect-export", label: "Inspect", dataset: { exportId: item.id } }])}
      </li>
    `, "No export jobs have been generated.");
    renderList(dom.regulatedCandidateList, candidates, (candidate) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(candidate.title || candidate.id)}</strong>
          <span class="workbench-pill" data-tone="warning">${escapeHTML(candidate.data_class || "regulated_business")}</span>
        </div>
        ${rowMeta([candidate.category, candidate.retention_state, formatDate(candidate.updated_at)])}
        ${actionButtons([{ type: "finalize-regulated", label: "Finalize", primary: true, dataset: { documentId: candidate.id, title: candidate.title || candidate.id } }])}
      </li>
    `, "No regulated-document candidates are waiting.");
    renderList(dom.regulatedDocumentWorkbenchList, regulated, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.title)}</strong>
          <span class="workbench-pill" data-tone="${item.immutability_state === "finalized" ? "success" : "warning"}">${escapeHTML(item.immutability_state)}</span>
        </div>
        ${rowMeta([item.retention_state, item.current_version_number ? `v${item.current_version_number}` : null, formatDate(item.finalized_at || item.updated_at)])}
      </li>
    `, "No regulated documents have been finalized.");
    renderList(dom.complianceInventoryList, Object.entries(inventory), ([name, info]) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(name)}</strong>
          <span class="workbench-pill" data-tone="${info.erasable ? "success" : "warning"}">${info.data_class || "unclassified"}</span>
        </div>
        ${rowMeta([info.exportable ? "exportable" : "not exportable", info.erasable ? "erasable" : "retention bound", info.pseudonymize_only ? "pseudonymize only" : null])}
      </li>
    `, "No inventory records were returned.");
  }

  async function renderIntelligence() {
    const governanceRole = hasRole("org_owner", "org_admin");
    const [workbenchPayload, candidatePayload, examplesPayload, exportsPayload] = await Promise.all([
      fetchJson("/intelligence/workbench"),
      governanceRole ? fetchJson("/intelligence/promotion-candidates") : Promise.resolve({ items: [] }),
      governanceRole ? fetchJson("/intelligence/training-examples") : Promise.resolve({ items: [] }),
      governanceRole ? fetchJson("/intelligence/training-exports") : Promise.resolve({ items: [] }),
    ]);
    const workbench = workbenchPayload.item || workbenchPayload.workbench || {};
    const worldState = workbench.world_state || {};
    const recommendations = workbench.recommendations || [];
    const focusHints = workbench.focus_hints || [];
    const decisions = workbench.decisions || [];
    const obligations = worldState.obligations || [];
    const candidates = candidatePayload.items || candidatePayload.promotion_candidates || [];
    const examples = examplesPayload.items || examplesPayload.training_examples || [];
    const exports = exportsPayload.items || exportsPayload.training_exports || [];
    renderMetricGrid(dom.intelligenceMetricGrid, [
      { label: "Prepared work", value: String(recommendations.length), detail: "Worker-ready packets ranked by local evidence" },
      { label: "Missing pieces", value: String(obligations.length), detail: "Blocked, due, or waiting-on-input work" },
      { label: "Focus hints", value: String(focusHints.length), detail: `${worldState.risk_count || 0} blocked or elevated` },
      { label: "Dataset exports", value: String(exports.length), detail: governanceRole ? "Generated offline corpora" : "Governance review required" },
    ]);
    setStateCard(
      dom.intelligenceStateCard,
      recommendations[0]?.title || "Prepared work is available.",
      recommendations[0]
        ? `${recommendations[0].readiness_status || "ready_now"} - ${recommendations[0].recommendation_type || "prepared work"} - score ${recommendations[0].ranking_explanation?.score || 0}`
        : governanceRole
          ? "No work packet is currently ranked above the local threshold."
          : "KERN is preparing worker-facing context here. Shared promotion and training review still stay manual for governance roles.",
      recommendations[0]?.readiness_status === "blocked" || recommendations[0]?.risk_level === "high" ? "warning" : "success"
    );
    renderList(dom.recommendationList, recommendations, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.title || item.recommendation_type || item.id)}</strong>
          <span class="workbench-pill" data-tone="${item.readiness_status === "blocked" ? "danger" : item.readiness_status === "waiting_on_input" ? "warning" : "success"}">${escapeHTML(item.readiness_status || "ready_now")}</span>
        </div>
        ${rowMeta([
          item.workflow_type ? item.workflow_type.replaceAll("_", " ") : null,
          item.evidence_bundle?.scope ? `${item.evidence_bundle.scope} scope` : null,
          item.preparation_scope ? `${item.preparation_scope} prep` : null,
          item.ranking_explanation?.score != null ? `score ${item.ranking_explanation.score}` : null,
        ])}
        <p class="panel-state__body">${escapeHTML(item.reason || "No reason provided.")}</p>
        ${actionButtons([
          { type: "inspect-preparation", label: "Inspect prep", primary: true, dataset: { recommendationId: item.id } },
          ...(item.recommendation_type === "suggested_draft" || item.recommendation_type === "follow_up_candidate"
            ? [{ type: "draft-preparation", label: "Draft wording", dataset: { recommendationId: item.id } }]
            : []),
          { type: "keep-personal", label: "Keep personal", dataset: { recommendationId: item.id } },
          { type: "promote-preparation", label: "Promote for review", dataset: { recommendationId: item.id } },
          ...(item.missing_inputs?.length ? [{ type: "ask-missing-info", label: "Ask missing info", dataset: { recommendationId: item.id } }] : []),
          { type: "mark-not-relevant", label: "Not relevant", dataset: { recommendationId: item.id } },
        ])}
      </li>
    `, "No prepared work is available.");
    const missingItems = [
      ...recommendations.flatMap((item) => (item.missing_inputs || []).map((missing) => ({ ...missing, recommendationId: item.id, packetTitle: item.title }))),
      ...obligations.map((item) => ({ id: item.id, label: item.title, reason: item.reason, severity: item.status === "blocked" ? "blocking" : "warning", recommendationId: item.workflow_id, packetTitle: item.title })),
    ];
    renderList(dom.obligationList, missingItems, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.label || item.packetTitle || item.id)}</strong>
          <span class="workbench-pill" data-tone="${item.severity === "blocking" ? "danger" : item.severity === "warning" ? "warning" : "success"}">${escapeHTML(item.severity || "info")}</span>
        </div>
        ${rowMeta([
          item.packetTitle && item.packetTitle !== item.label ? item.packetTitle : null,
          item.recommendationId ? `source ${item.recommendationId}` : null,
        ])}
        <p class="panel-state__body">${escapeHTML(item.reason || "No missing-input reason was recorded.")}</p>
      </li>
    `, "No missing prerequisites are active.");
    renderList(dom.workflowList, focusHints, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.title || item.id)}</strong>
          <span class="workbench-pill" data-tone="${item.readiness_status === "blocked" ? "danger" : item.readiness_status === "waiting_on_input" ? "warning" : "success"}">${escapeHTML(item.readiness_status || "ready_now")}</span>
        </div>
        ${rowMeta([
          item.workflow_id,
          item.score != null ? `score ${item.score}` : null,
          item.risk_level ? `risk ${item.risk_level}` : null,
        ])}
        <p class="panel-state__body">${escapeHTML((item.why_now || []).join(" ") || "No focus explanation recorded.")}</p>
        ${actionButtons(item.recommendation_id ? [{ type: "inspect-preparation", label: "Inspect prep", dataset: { recommendationId: item.recommendation_id } }] : [])}
      </li>
    `, "No focus hints are active.");
    renderList(dom.decisionHistoryList, decisions, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.decision_kind || "decision")}</strong>
          <span class="workbench-pill" data-tone="success">${escapeHTML(item.decision_value || "recorded")}</span>
        </div>
        ${rowMeta([item.source_type, item.source_id, formatDate(item.created_at)])}
        <p class="panel-state__body">${escapeHTML(item.rationale || "No rationale recorded.")}</p>
      </li>
    `, "No decision history has been recorded yet.");
    renderList(dom.promotionCandidatesList, candidates, (candidate) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(candidate.summary || candidate.value || candidate.key || candidate.id)}</strong>
          <span class="workbench-pill" data-tone="${candidate.provenance?.policy_safe ? "success" : "warning"}">${escapeHTML(candidate.provenance?.scope || candidate.scope || "workspace")}</span>
        </div>
        ${rowMeta([
          candidate.provenance?.workspace_slug,
          candidate.provenance?.policy_safe === false ? "manual review required" : "policy safe",
          candidate.ranking_explanation?.prior_approvals != null ? `approvals ${candidate.ranking_explanation.prior_approvals}` : null,
        ])}
        ${actionButtons([
          { type: "review-promotion", label: "Approve", primary: true, dataset: { memoryId: candidate.id, decision: "approved" } },
          { type: "review-promotion", label: "Personal only", dataset: { memoryId: candidate.id, decision: "personal_only" } },
          { type: "review-promotion", label: "Reject", dataset: { memoryId: candidate.id, decision: "rejected" } },
        ])}
      </li>
    `, governanceRole ? "No promotion candidates are waiting." : "Promotion review is restricted to governance roles.");
    renderList(dom.trainingExamplesList, examples, (example) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(example.source_type)} - ${escapeHTML(example.source_id)}</strong>
          <span class="workbench-pill" data-tone="${example.status === "approved" ? "success" : example.status === "rejected" ? "danger" : "warning"}">${escapeHTML(example.status)}</span>
        </div>
        ${rowMeta([example.workspace_slug, example.metadata?.data_class, example.approved_for_training ? "approved for training" : "not approved"])}
        ${actionButtons([
          { type: "review-example", label: "Approve", primary: true, dataset: { exampleId: example.id, status: "approved" } },
          { type: "review-example", label: "Reject", dataset: { exampleId: example.id, status: "rejected" } },
        ])}
      </li>
    `, governanceRole ? "No training examples are queued." : "Training example review is restricted to governance roles.");
    renderList(dom.trainingExportsList, exports, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.id || item.workspace_slug || "training export")}</strong>
          <span class="workbench-pill" data-tone="success">${escapeHTML(item.train_count != null ? `${item.train_count} train` : "ready")}</span>
        </div>
        ${rowMeta([
          item.workspace_slug,
          item.validation_count != null ? `${item.validation_count} validation` : null,
          item.compliance_filter_report ? `filtered personal ${item.compliance_filter_report.excluded_personal || 0}` : null,
        ])}
        ${actionButtons([{ type: "inspect-training-export", label: "Inspect", dataset: { exportId: item.id } }])}
      </li>
    `, governanceRole ? "No training exports have been generated." : "Training export review is restricted to governance roles.");
  }

  async function renderEvidence() {
    const [health, exportPayload, trainingPayload] = await Promise.all([
      fetchJson("/health"),
      hasRole("org_owner", "org_admin", "auditor") ? fetchJson("/compliance/data-exports") : Promise.resolve({ items: [] }),
      hasRole("org_owner", "org_admin", "auditor") ? fetchJson("/intelligence/training-exports") : Promise.resolve({ items: [] }),
    ]);
    const exports = exportPayload.items || exportPayload.data_exports || [];
    const trainingExports = trainingPayload.items || trainingPayload.training_exports || [];
    renderMetricGrid(dom.evidenceMetricGrid, [
      { label: "Health", value: String(health.status || "unknown"), detail: `${health.uptime_seconds || 0}s uptime` },
      { label: "Audit chain", value: health.audit_chain_ok ? "ok" : "review", detail: health.profile_locked ? "workspace locked" : "workspace unlocked" },
      { label: "Compliance exports", value: String(exports.length), detail: "Evidence-bearing manifests" },
      { label: "Training exports", value: String(trainingExports.length), detail: "Offline dataset packages" },
    ]);
    renderList(dom.evidenceManifestList, exports, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.workspace_slug || item.target_user_id || item.id)}</strong>
          <span class="workbench-pill" data-tone="${item.status === "completed" ? "success" : "warning"}">${escapeHTML(item.status || "requested")}</span>
        </div>
        ${rowMeta([item.workspace_slug ? "workspace export" : "subject export", formatDate(item.updated_at)])}
        ${actionButtons([{ type: "inspect-export", label: "Inspect", dataset: { exportId: item.id } }])}
      </li>
    `, "No compliance exports are available.");
    renderList(dom.evidenceTrainingManifestList, trainingExports, (item) => `
      <li>
        <div class="workbench-row-title">
          <strong>${escapeHTML(item.id || item.workspace_slug || "training export")}</strong>
          <span class="workbench-pill" data-tone="success">${escapeHTML(item.train_count != null ? `${item.train_count} train` : "ready")}</span>
        </div>
        ${rowMeta([item.workspace_slug, item.validation_count != null ? `${item.validation_count} validation` : null, formatDate(item.created_at)])}
        ${actionButtons([{ type: "inspect-training-export", label: "Inspect", dataset: { exportId: item.id } }])}
      </li>
    `, "No training dataset manifests are available.");
  }

  async function renderTab(tabName) {
    state.activeTab = tabName;
    try {
      if (tabName === "workspace") await renderWorkspace();
      if (tabName === "admin") await renderAdmin();
      if (tabName === "compliance") await renderCompliance();
      if (tabName === "intelligence") await renderIntelligence();
      if (tabName === "evidence") await renderEvidence();
    } catch (error) {
      const target = {
        workspace: dom.workspaceSummaryState,
        admin: dom.adminAccessState,
        compliance: dom.complianceStateCard,
        intelligence: dom.intelligenceStateCard,
        evidence: dom.evidenceStateCard,
      }[tabName] || dom.workspaceSummaryState;
      setStateCard(
        target,
        error.status === 403 ? "Not allowed in this workspace." : "Workbench request failed.",
        error.message || "The workbench could not load this surface.",
        "error"
      );
    }
  }

  async function runAction(action, dataset) {
    if (action === "approve-user") {
      await fetchJson(`/admin/users/${dataset.userId}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_slug: dom.adminUserWorkspace?.value || state.session.workspace_slug, role: "member" }),
      });
      await renderAdmin();
      return;
    }
    if (action === "suspend-user") {
      await fetchJson(`/admin/users/${dataset.userId}/suspend`, { method: "POST" });
      await renderAdmin();
      return;
    }
    if (action === "revoke-session") {
      await fetchJson(`/admin/sessions/${dataset.sessionId}/revoke`, { method: "POST" });
      await renderAdmin();
      return;
    }
    if (action === "execute-erasure") {
      await fetchJson(`/compliance/erasure-requests/${dataset.requestId}/execute`, { method: "POST" });
      await renderCompliance();
      return;
    }
    if (action === "finalize-regulated") {
      await fetchJson("/compliance/regulated-documents/finalize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_id: dataset.documentId, title: dataset.title }),
      });
      await renderCompliance();
      return;
    }
    if (action === "review-promotion") {
      await fetchJson(`/intelligence/promotion-candidates/${dataset.memoryId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: dataset.decision }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "review-example") {
      await fetchJson(`/intelligence/training-examples/${dataset.exampleId}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: dataset.status }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "inspect-export") {
      const detail = await fetchJson(`/compliance/data-exports/${dataset.exportId}`);
      setStateCard(
        dom.evidenceStateCard,
        detail.item?.workspace_slug || detail.item?.target_user_id || "Export detail",
        `${detail.item?.status || "unknown"} · ${detail.manifest?.created_at || detail.item?.updated_at || "no timestamp"} · ${detail.artifact?.path || "no artifact path"}`,
        detail.item?.status === "completed" ? "success" : "warning"
      );
      return;
    }
    if (action === "inspect-training-export") {
      const detail = await fetchJson(`/intelligence/training-exports/${dataset.exportId}`);
      const manifest = detail.manifest || {};
      setStateCard(
        dom.intelligenceStateCard,
        detail.item?.id || dataset.exportId,
        `${manifest.train_count || 0} train / ${manifest.validation_count || 0} validation · dedup ${manifest.dedup_count || 0} · personal excluded ${manifest.compliance_filter_report?.excluded_personal || 0}`,
        "success"
      );
      return;
    }
    if (action === "inspect-preparation") {
      const detail = await fetchJson(`/intelligence/preparation/${dataset.recommendationId}`);
      const packet = detail.item || detail.preparation_packet || {};
      const claims = packet.evidence_pack?.claims || [];
      const missing = packet.missing_inputs || [];
      const events = packet.event_refs || packet.evidence_pack?.event_refs || [];
      setStateCard(
        dom.intelligenceStateCard,
        packet.title || dataset.recommendationId,
        `${packet.summary || "No summary provided."} - ${packet.readiness_status || "ready_now"} - ${missing.length} missing input(s) - ${claims.length} claim(s) - ${events.length} event ref(s) - mode ${packet.generation_contract?.mode || "explain_only"} - scope ${packet.preparation_scope || packet.evidence_pack?.scope || "workspace"}`,
        packet.readiness_status === "blocked" ? "warning" : "success"
      );
      return;
    }
    if (action === "draft-preparation") {
      const detail = await fetchJson(`/intelligence/preparation/${dataset.recommendationId}/draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "llm_rewrite" }),
      });
      const draft = detail.item || detail.suggested_draft || {};
      setStateCard(
        dom.intelligenceStateCard,
        draft.subject || draft.title || "Prepared draft",
        `${detail.render_mode || draft.mode || "deterministic_scaffold"} - ${(draft.body || "No body generated.")}`.slice(0, 320),
        "success"
      );
      return;
    }
    if (action === "keep-personal") {
      await fetchJson("/intelligence/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          signal_type: "personal_preference",
          source_type: "preparation",
          source_id: dataset.recommendationId,
          metadata: { action: "keep_personal" },
        }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "promote-preparation") {
      await fetchJson("/intelligence/memory/promote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          signal_type: "promote_workspace",
          source_type: "preparation",
          source_id: dataset.recommendationId,
          metadata: { action: "promote_for_review" },
        }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "ask-missing-info") {
      const detail = await fetchJson(`/intelligence/preparation/${dataset.recommendationId}`);
      const packet = detail.item || detail.preparation_packet || {};
      const prompts = (packet.missing_inputs || []).map((item) => `${item.label}: ${item.reason}`);
      setStateCard(
        dom.intelligenceStateCard,
        packet.title || dataset.recommendationId,
        prompts.length ? `Ask for: ${prompts.join(" | ")}` : "No missing information is currently recorded.",
        prompts.length ? "warning" : "success"
      );
      return;
    }
    if (action === "mark-not-relevant") {
      await fetchJson("/intelligence/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          signal_type: "reject_pattern",
          source_type: "preparation",
          source_id: dataset.recommendationId,
          metadata: { action: "mark_not_relevant" },
        }),
      });
      await renderIntelligence();
      return;
    }
    if (action === "inspect-workflow") {
      const detail = await fetchJson(`/intelligence/workflows/${dataset.workflowId}`);
      const workflow = detail.item || detail.workflow || {};
      setStateCard(
        dom.intelligenceStateCard,
        workflow.workflow_type || dataset.workflowId,
        `${workflow.last_event || "No event"} · next ${workflow.next_expected_step || "none"} · ${detail.events?.length || 0} recorded event(s)`,
        workflow.status === "blocked" ? "warning" : "success"
      );
    }
  }

  function bindEvents() {
    document.getElementById("workspaceRefreshButton")?.addEventListener("click", () => renderWorkspace());
    document.getElementById("adminRefreshButton")?.addEventListener("click", () => renderAdmin());
    document.getElementById("adminCreateWorkspaceButton")?.addEventListener("click", async () => {
      await fetchJson("/admin/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug: dom.adminWorkspaceSlug?.value || "", title: dom.adminWorkspaceTitle?.value || "" }),
      });
      if (dom.adminWorkspaceSlug) dom.adminWorkspaceSlug.value = "";
      if (dom.adminWorkspaceTitle) dom.adminWorkspaceTitle.value = "";
      await renderAdmin();
      await renderWorkspace();
    });
    document.getElementById("adminCreateUserButton")?.addEventListener("click", async () => {
      await fetchJson("/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: dom.adminUserEmail?.value || "",
          display_name: dom.adminUserDisplayName?.value || "",
          role: dom.adminUserRole?.value || "member",
          workspace_slug: dom.adminUserWorkspace?.value || state.session.workspace_slug,
        }),
      });
      if (dom.adminUserEmail) dom.adminUserEmail.value = "";
      if (dom.adminUserDisplayName) dom.adminUserDisplayName.value = "";
      await renderAdmin();
    });
    document.getElementById("generateMyExportButton")?.addEventListener("click", async () => {
      if (!state.session?.user?.id) return;
      await fetchJson(`/compliance/exports/user/${state.session.user.id}/generate`, { method: "POST" });
      await renderCompliance();
      await renderEvidence();
    });
    document.getElementById("createSelfErasureButton")?.addEventListener("click", async () => {
      await fetchJson("/compliance/erasure-requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_user_id: state.session?.user?.id,
          workspace_slug: state.session?.workspace_slug,
          reason: dom.selfErasureReason?.value || "",
        }),
      });
      if (dom.selfErasureReason) dom.selfErasureReason.value = "";
      await renderCompliance();
    });
    document.getElementById("saveRetentionPolicyButton")?.addEventListener("click", async () => {
      await fetchJson("/compliance/retention-policies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          data_class: dom.retentionDataClass?.value,
          retention_days: Number(dom.retentionDays?.value || 0),
          legal_hold_enabled: !!dom.retentionLegalHoldEnabled?.checked,
        }),
      });
      await renderCompliance();
    });
    document.getElementById("createLegalHoldButton")?.addEventListener("click", async () => {
      await fetchJson("/compliance/legal-holds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workspace_slug: dom.legalHoldWorkspace?.value || state.session?.workspace_slug,
          reason: dom.legalHoldReason?.value || "",
        }),
      });
      if (dom.legalHoldReason) dom.legalHoldReason.value = "";
      await renderCompliance();
    });
    document.getElementById("generateWorkspaceExportButton")?.addEventListener("click", async () => {
      await fetchJson(`/compliance/exports/workspace/${state.session?.workspace_slug}/generate`, { method: "POST" });
      await renderCompliance();
      await renderEvidence();
    });
    document.getElementById("createTrainingExportButton")?.addEventListener("click", async () => {
      await fetchJson("/intelligence/training-exports", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_slug: state.session?.workspace_slug }),
      });
      await renderIntelligence();
      await renderEvidence();
    });
    dom.modal?.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-workbench-action]");
      if (!button) return;
      await runAction(button.dataset.workbenchAction, button.dataset);
    });
  }

  return {
    async init() {
      await refreshSession();
      bindEvents();
      await renderWorkspace();
    },
    async onTabActivated(tabName) {
      await renderTab(tabName);
    },
    async refreshActiveTab() {
      await renderTab(state.activeTab);
    },
  };
}
