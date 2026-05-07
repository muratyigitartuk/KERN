function escapeHTMLShell(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function readCookie(name) {
  const prefix = `${name}=`;
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
    ?.slice(prefix.length) || "";
}

function csrfHeaders() {
  const token = readCookie("kern_csrf_token");
  return token ? { "x-csrf-token": token } : {};
}

function persistWorkspaceCookie(slug) {
  if (!slug) return;
  // H-16: Add Secure flag to prevent cookie transmission over plain HTTP.
  const secureSuffix = location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `kern_workspace_slug=${encodeURIComponent(slug)}; path=/; samesite=lax${secureSuffix}`;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("Authentication required.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "Request failed.");
  }
  return payload;
}

function initialsFromIdentity(user) {
  const source = String(user?.display_name || user?.email || user?.user_email || "KERN").trim();
  const tokens = source
    .replace(/@.*$/, "")
    .split(/[\s._-]+/)
    .filter(Boolean);
  if (!tokens.length) return "KE";
  if (tokens.length === 1) return tokens[0].slice(0, 2).toUpperCase();
  return `${tokens[0][0] || ""}${tokens[1][0] || ""}`.toUpperCase();
}

function formatRole(user, session) {
  const roles = Array.isArray(session?.roles) ? session.roles : [];
  if (roles.includes("break_glass_admin")) return "Break-glass";
  if (roles.includes("org_owner")) return "Owner";
  if (roles.includes("org_admin")) return "Admin";
  if (roles.includes("auditor")) return "Auditor";
  return user?.email ? "Workspace" : "Local";
}

function closeWorkspaceMenu(button, menu) {
  button?.setAttribute("aria-expanded", "false");
  menu?.classList.add("hidden");
  menu?.setAttribute("aria-hidden", "true");
}

function openWorkspaceMenu(button, menu) {
  if (button?.disabled) {
    closeWorkspaceMenu(button, menu);
    return;
  }
  button?.setAttribute("aria-expanded", "true");
  menu?.classList.remove("hidden");
  menu?.setAttribute("aria-hidden", "false");
}

async function loadAuthShell() {
  const logout = document.getElementById("workspaceLogout");
  const switcherButton = document.getElementById("workspaceSwitcherButton");
  const switcherValue = document.getElementById("workspaceSwitcherValue");
  const switcherMenu = document.getElementById("workspaceSwitcherMenu");
  const userInitials = document.getElementById("workspaceUserInitials");
  const userName = document.getElementById("workspaceUserName");
  const userMeta = document.getElementById("workspaceUserMeta");
  const switcherShell = document.getElementById("workspaceAccessShell");

  if (!logout || !switcherButton || !switcherValue || !switcherMenu || !switcherShell) {
    return;
  }

  try {
    const [session, workspacesPayload] = await Promise.all([
      fetchJson("/auth/session"),
      fetchJson("/auth/session/workspaces"),
    ]);

    const workspaces = workspacesPayload.workspaces || workspacesPayload.items || [];
    const currentWorkspace = workspaces.find((workspace) => workspace.slug === session.workspace_slug) || workspaces[0] || null;
    const activeUser = session.user || {};
    const displayName = activeUser.display_name || activeUser.email || session.user_email || "KERN User";
    const metaLine = activeUser.email || formatRole(activeUser, session);

    if (userInitials) {
      userInitials.textContent = initialsFromIdentity(activeUser);
    }
    if (userName) {
      userName.textContent = displayName;
    }
    if (userMeta) {
      userMeta.textContent = metaLine;
      userMeta.title = metaLine;
    }
    switcherValue.textContent = currentWorkspace?.title || currentWorkspace?.slug || "Workspace";
    persistWorkspaceCookie(currentWorkspace?.slug || session.workspace_slug || "");
    switcherButton.disabled = workspaces.length <= 1;
    closeWorkspaceMenu(switcherButton, switcherMenu);

    switcherMenu.innerHTML = "";
    if (workspaces.length > 1) {
      for (const workspace of workspaces) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "workspace-switcher__option";
        option.setAttribute("role", "menuitemradio");
        option.setAttribute("aria-checked", workspace.slug === session.workspace_slug ? "true" : "false");
        option.dataset.workspaceSlug = workspace.slug;
        option.innerHTML = `
          <span class="workspace-switcher__option-title">${escapeHTMLShell(workspace.title || workspace.slug)}</span>
          <span class="workspace-switcher__option-meta">${escapeHTMLShell(workspace.slug === session.workspace_slug ? "active" : workspace.slug)}</span>
        `;
        option.addEventListener("click", async () => {
          if (workspace.slug === session.workspace_slug) {
            closeWorkspaceMenu(switcherButton, switcherMenu);
            return;
          }
          switcherButton.disabled = true;
          try {
            await fetchJson("/auth/session/select-workspace", {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                ...csrfHeaders(),
              },
              body: JSON.stringify({ workspace_slug: workspace.slug }),
            });
            persistWorkspaceCookie(workspace.slug);
            window.location.reload();
          } finally {
            switcherButton.disabled = false;
          }
        });
        switcherMenu.appendChild(option);
      }
    }

    switcherButton.addEventListener("click", () => {
      if (switcherButton.disabled) {
        closeWorkspaceMenu(switcherButton, switcherMenu);
        return;
      }
      const isOpen = switcherButton.getAttribute("aria-expanded") === "true";
      if (isOpen) {
        closeWorkspaceMenu(switcherButton, switcherMenu);
      } else {
        openWorkspaceMenu(switcherButton, switcherMenu);
      }
    });

    document.addEventListener("click", (event) => {
      if (switcherShell.contains(event.target)) {
        return;
      }
      closeWorkspaceMenu(switcherButton, switcherMenu);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeWorkspaceMenu(switcherButton, switcherMenu);
      }
    });

    logout.addEventListener("click", async () => {
      await fetchJson("/auth/logout", {
        method: "POST",
        headers: csrfHeaders(),
      });
      document.cookie = "kern_workspace_slug=; Max-Age=0; path=/; samesite=lax";
      window.location.href = "/login";
    });
  } catch (_error) {
    window.location.href = "/login";
  }
}

loadAuthShell();
