(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__ || {};
  const React = SDK.React;
  const hooks = SDK.hooks || {};
  const components = SDK.components || {};
  const { useState, useEffect, useCallback } = hooks;
  const { Card, CardHeader, CardTitle, CardContent, Button } = components;
  const h = React ? React.createElement : function () { return null; };

  if (!React || !Card || !Button) {
    window.__HERMES_PLUGINS__.register("hermes-chat", function () {
      return h("div", { className: "p-4" }, "Hermes Chat dashboard could not load: SDK components are missing.");
    });
    return;
  }

  // Derive the correct API prefix. Try in order:
  //  1. SDK.apiPrefix  — Hermes may inject this directly
  //  2. SDK.pluginName — build prefix from the registered plugin name
  //  3. URL of a <script> tag whose src contains /plugins/<name>/
  //  4. Hard-coded fallback (correct after plugin is renamed)
  function _resolveApiPrefix() {
    if (SDK.apiPrefix) return SDK.apiPrefix;
    if (SDK.pluginName) return "/api/plugins/" + SDK.pluginName;
    try {
      const scripts = Array.from(document.querySelectorAll("script[src]"));
      for (const s of scripts) {
        const m = s.src.match(/\/plugins\/([^/]+)\//);
        if (m) return "/api/plugins/" + m[1];
      }
    } catch (_) {}
    return "/api/plugins/hermes-chat";
  }
  const API_PREFIX = _resolveApiPrefix();

  async function fetchJSON(path, opts) {
    const url = API_PREFIX + path;
    const res = await fetch(url, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
      credentials: "same-origin",
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  /** Hermes dashboard APIs (same origin as this tab — not the plugin prefix). */
  async function fetchHermesJSON(path, opts) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", ...(opts && opts.headers) },
      credentials: "same-origin",
    });
    if (!res.ok) {
      let detail = "HTTP " + res.status;
      try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }

  function isCatalogModelFree(provider, modelId) {
    const id = String(modelId || "");
    if (id.toLowerCase().endsWith(":free")) return true;
    const pricing = provider && provider.pricing;
    const entry = pricing && pricing[id];
    return !!(entry && entry.free === true);
  }

  function flattenHermesModelOptions(catalog, analytics) {
    const options = [];
    const models = (analytics && analytics.models) || [];
    for (const row of models) {
      const id = row && row.model;
      if (!id) continue;
      options.push({
        id,
        name: String(id).split("/").pop() || id,
        provider: row.provider || "",
        provider_name: "Hermes Profiles",
        is_profile: true,
        free: String(id).toLowerCase().endsWith(":free"),
      });
    }
    for (const provider of (catalog && catalog.providers) || []) {
      if (!provider) continue;
      const slug = provider.slug || provider.id || "";
      const providerName = provider.name || slug || "Unknown";
      for (const model of provider.models || []) {
        const id = (model && (model.id || model.model)) || String(model);
        if (!id) continue;
        const name = (model && model.name) || id;
        options.push({
          id,
          name,
          provider: slug,
          provider_name: providerName,
          is_profile: false,
          free: isCatalogModelFree(provider, id),
        });
      }
    }
    const seen = new Set();
    return options.filter((opt) => {
      const key = (opt.provider || "") + "|||" + opt.id;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function StatusPill({ running, healthy }) {
    const color = !running ? "bg-muted text-muted-foreground" : healthy ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-100" : "bg-destructive/15 text-destructive";
    const text = !running ? "Stopped" : healthy ? "Running" : "Unhealthy";
    return h("span", { className: "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium " + color }, text);
  }

  const { useRef } = hooks;

  function HermesChatDashboard() {
    const [status, setStatus] = useState(null);
    const [logs, setLogs] = useState("");
    const [logsPaused, setLogsPaused] = useState(false);
    const [users, setUsers] = useState([]);
    const [newUsername, setNewUsername] = useState("");
    const [newPassword, setNewPassword] = useState("");
    const [newFreeOnly, setNewFreeOnly] = useState(false);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [notice, setNotice] = useState(null);
    const [missingDeps, setMissingDeps] = useState([]);
    const [missingOptionalDeps, setMissingOptionalDeps] = useState([]);
    const [voiceEnabled, setVoiceEnabled] = useState(true);
    const [voiceStatus, setVoiceStatus] = useState({ tts: true, stt: true, ttsProvider: "", sttProvider: "", detail: "" });
    const [settingsHost, setSettingsHost] = useState("");
    const [settingsPort, setSettingsPort] = useState("");
    const [suggestionsEnabled, setSuggestionsEnabled] = useState(true);
    const [suggestionsInterval, setSuggestionsInterval] = useState("60");
    const [suggestionsPoolSize, setSuggestionsPoolSize] = useState("12");
    const [suggestionsShowCount, setSuggestionsShowCount] = useState("4");
    const [suggestionsModel, setSuggestionsModel] = useState("");
    const [suggestionsProvider, setSuggestionsProvider] = useState("");
    const [suggestionModelOptions, setSuggestionModelOptions] = useState([]);
    const [suggestionModelsError, setSuggestionModelsError] = useState(null);
    const [suggestionsPrompt, setSuggestionsPrompt] = useState("");
    const [suggestionsPromptDirty, setSuggestionsPromptDirty] = useState(false);
    const [pendingRestart, setPendingRestart] = useState(false);
    const [dashboardUrl, setDashboardUrl] = useState("");
    const [dashboardUrlSource, setDashboardUrlSource] = useState("");
    const [dashboardUrlVerify, setDashboardUrlVerify] = useState(null);
    const [dashboardUrlLoading, setDashboardUrlLoading] = useState(false);
    const logsPausedRef = useRef(false);
    const logsBoxRef = useRef(null);

    const loadStatus = useCallback(async () => {
      try {
        const data = await fetchJSON("/status");
        setStatus(data);
        setError(null);
        if (data.config) {
          setSettingsHost(prev => prev === "" ? (data.config.host || "") : prev);
          setSettingsPort(prev => prev === "" ? String(data.config.port || "") : prev);
        }
      } catch (err) {
        setError("Status failed: " + String(err));
      }
    }, []);

    const loadDeps = useCallback(async () => {
      try {
        const data = await fetchJSON("/deps");
        setMissingDeps(data.missing || []);
        setMissingOptionalDeps(data.missing_optional || []);
      } catch (err) {
        setError("Deps check failed: " + String(err));
      }
    }, []);

    const loadVoiceConfig = useCallback(async () => {
      try {
        const data = await fetchJSON("/voice-config");
        setVoiceEnabled(data.voice_enabled);
        setVoiceStatus({
          tts: !!data.tts_available,
          stt: !!data.stt_available,
          ttsProvider: data.tts_provider || "",
          sttProvider: data.stt_provider || "",
          detail: data.detail || "",
        });
      } catch (_) {}
    }, []);

    const loadSuggestionsConfig = useCallback(async () => {
      try {
        const cfg = await fetchJSON("/config");
        setSuggestionsEnabled(!!cfg.suggestions_enabled);
        setSuggestionsInterval(String(cfg.suggestions_interval_minutes ?? 60));
        setSuggestionsPoolSize(String(cfg.suggestions_pool_size ?? 12));
        setSuggestionsShowCount(String(cfg.suggestions_show_count ?? 4));
        setSuggestionsModel(cfg.suggestions_model || "");
        setSuggestionsProvider(cfg.suggestions_provider || "");
        const prompt = await fetchJSON("/suggestions/prompt");
        setSuggestionsPrompt(prompt.content || "");
        setSuggestionsPromptDirty(false);
      } catch (_) {}
      try {
        // Hit Hermes dashboard APIs directly (same catalog as Models page).
        // Plugin Python routes only remount on Hermes restart; these do not.
        const [catalog, analytics] = await Promise.all([
          fetchHermesJSON("/api/model/options?explicit_only=1").catch(() => null),
          fetchHermesJSON("/api/analytics/models?days=30").catch(() => null),
        ]);
        const options = flattenHermesModelOptions(catalog, analytics);
        setSuggestionModelOptions(options);
        setSuggestionModelsError(
          options.length
            ? null
            : "Hermes returned no models. Check provider auth on the Models page."
        );
      } catch (err) {
        setSuggestionModelOptions([]);
        setSuggestionModelsError(String(err));
      }
    }, []);

    const loadLogs = useCallback(async () => {
      try {
        const data = await fetchJSON("/logs?tail=100");
        if (!logsPausedRef.current) {
          setLogs(data.log);
        }
      } catch (err) {
        if (!logsPausedRef.current) setLogs("Failed to load logs: " + err);
      }
    }, []);

    const loadUsers = useCallback(async () => {
      try {
        const data = await fetchJSON("/users");
        setUsers(data.users || []);
      } catch (err) {
        setError("Users failed: " + String(err));
      }
    }, []);

    const loadDashboardUrl = useCallback(async () => {
      try {
        const data = await fetchJSON("/dashboard-url");
        setDashboardUrl(data.url || "");
        setDashboardUrlSource(data.source || "");
        setDashboardUrlVerify(data.verify || null);
      } catch (err) {
        setDashboardUrlVerify({ ok: false, error: String(err) });
      }
    }, []);

    useEffect(() => {
      loadStatus();
      loadDeps();
      loadLogs();
      loadUsers();
      loadDashboardUrl();
      loadVoiceConfig();
      loadSuggestionsConfig();
      const id = setInterval(() => { loadStatus(); loadLogs(); }, 5000);
      return () => clearInterval(id);
    }, [loadStatus, loadDeps, loadLogs, loadUsers, loadDashboardUrl, loadVoiceConfig, loadSuggestionsConfig]);

    useEffect(() => {
      if (!logsPaused && logsBoxRef.current) {
        logsBoxRef.current.scrollTop = logsBoxRef.current.scrollHeight;
      }
    }, [logs, logsPaused]);

    const toggleLogsPause = () => {
      const next = !logsPausedRef.current;
      logsPausedRef.current = next;
      setLogsPaused(next);
      if (!next) loadLogs();
    };

    async function act(promise, refresh) {
      setLoading(true);
      try {
        const result = await promise;
        if (refresh) await refresh();
        return result;
      } catch (err) {
        setError("Action failed: " + String(err));
      } finally {
        setLoading(false);
      }
    }

    const onStart = () => act(fetchJSON("/start", { method: "POST" }), loadStatus);
    const onStop = () => act(fetchJSON("/stop", { method: "POST" }), loadStatus);
    const onRestart = () => act(fetchJSON("/restart", { method: "POST" }), loadStatus);
    const onInstallDeps = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        const result = await fetchJSON("/install-deps", { method: "POST" });
        if (result.status === "error") {
          setError("Install failed: " + (result.message || "unknown error"));
        } else {
          const installed = result.installed && result.installed.length > 0
            ? "Installed: " + result.installed.join(", ") + "."
            : "All dependencies are already present.";
          setNotice(installed);
          await loadDeps();
          await loadStatus();
        }
      } catch (err) {
        setError("Install deps failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };
    const onRefreshLogs = () => act(loadLogs(), null);

    const onSaveVoiceSettings = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({ voice_enabled: voiceEnabled }),
        });
        setNotice("Voice settings saved.");
        await loadVoiceConfig();
      } catch (err) {
        setError("Save voice settings failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onSaveSuggestionsSettings = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        const interval = parseInt(suggestionsInterval, 10);
        const poolSize = parseInt(suggestionsPoolSize, 10);
        const showCount = parseInt(suggestionsShowCount, 10);
        if (isNaN(interval) || interval < 5) {
          setError("Refresh interval must be at least 5 minutes.");
          return;
        }
        if (isNaN(poolSize) || poolSize < 1 || poolSize > 32) {
          setError("Pool size must be between 1 and 32.");
          return;
        }
        if (isNaN(showCount) || showCount < 1 || showCount > 8) {
          setError("Chips shown must be between 1 and 8.");
          return;
        }
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({
            suggestions_enabled: suggestionsEnabled,
            suggestions_interval_minutes: interval,
            suggestions_pool_size: poolSize,
            suggestions_show_count: showCount,
            suggestions_model: suggestionsModel.trim(),
            suggestions_provider: suggestionsProvider.trim(),
          }),
        });
        setNotice("Suggestion settings saved. The background job picks them up on the next cycle.");
        await loadSuggestionsConfig();
      } catch (err) {
        setError("Save suggestion settings failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onSaveSuggestionsPrompt = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        await fetchJSON("/suggestions/prompt", {
          method: "PUT",
          body: JSON.stringify({ content: suggestionsPrompt }),
        });
        setSuggestionsPromptDirty(false);
        setNotice("Suggestion prompt saved.");
      } catch (err) {
        setError("Save suggestion prompt failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onGenerateSuggestionsNow = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        const result = await fetchJSON("/suggestions/refresh", { method: "POST", body: "{}" });
        if (result.status === "busy") {
          setNotice(result.message || "A refresh is already running.");
        } else {
          setNotice(
            result.message
              || ("Suggestion generation started for " + (result.users ?? "?") + " user(s). Check daemon logs for progress.")
          );
        }
      } catch (err) {
        setError("Generate suggestions failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onRestoreSuggestionsPrompt = async (source) => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        const result = await fetchJSON("/suggestions/prompt/restore", {
          method: "POST",
          body: JSON.stringify({ source }),
        });
        setSuggestionsPrompt(result.content || "");
        setSuggestionsPromptDirty(false);
        setNotice(
          result.warning
            ? result.warning
            : "Suggestion prompt restored from " + (result.source || source) + "."
        );
      } catch (err) {
        setError("Restore suggestion prompt failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onSaveSettings = async (e) => {
      e.preventDefault();
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        const port = parseInt(settingsPort, 10);
        if (isNaN(port) || port < 1 || port > 65535) {
          setError("Port must be a number between 1 and 65535.");
          return;
        }
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({ host: settingsHost.trim(), port }),
        });
        setPendingRestart(true);
        setNotice("Settings saved. Restart the daemon for changes to take effect.");
        await loadStatus();
      } catch (err) {
        setError("Save settings failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onRestartAfterSettings = async () => {
      await act(fetchJSON("/restart", { method: "POST" }), loadStatus);
      setPendingRestart(false);
      setNotice("Daemon restarted with new settings.");
    };

    const onCreateUser = async (e) => {
      e.preventDefault();
      await act(
        fetchJSON("/users", {
          method: "POST",
          body: JSON.stringify({
            username: newUsername,
            password: newPassword,
            free_models_only: newFreeOnly,
          }),
        }),
        loadUsers
      );
      setNewUsername("");
      setNewPassword("");
      setNewFreeOnly(false);
    };

    const onToggleFreeOnly = (userId, freeModelsOnly) =>
      act(
        fetchJSON("/users/" + userId, {
          method: "PATCH",
          body: JSON.stringify({ free_models_only: freeModelsOnly }),
        }),
        loadUsers
      );

    const onVerifyDashboardUrl = async () => {
      setDashboardUrlLoading(true);
      setError(null);
      try {
        const data = await fetchJSON("/dashboard-url/verify", {
          method: "POST",
          body: JSON.stringify({ url: dashboardUrl.trim() }),
        });
        setDashboardUrlVerify(data);
      } catch (err) {
        setDashboardUrlVerify({ ok: false, error: String(err) });
      } finally {
        setDashboardUrlLoading(false);
      }
    };

    const onSaveDashboardUrl = async (e) => {
      e.preventDefault();
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({ hermes_dashboard_url: dashboardUrl.trim() }),
        });
        setNotice("Dashboard URL saved. Restart the daemon for changes to take effect.");
        await loadDashboardUrl();
      } catch (err) {
        setError("Save dashboard URL failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onResetDashboardUrl = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      try {
        await fetchJSON("/config", {
          method: "POST",
          body: JSON.stringify({ hermes_dashboard_url: "" }),
        });
        setNotice("Dashboard URL override cleared. Auto-discovery will be used.");
        await loadDashboardUrl();
      } catch (err) {
        setError("Reset dashboard URL failed: " + String(err));
      } finally {
        setLoading(false);
      }
    };

    const onDeleteUser = (userId) =>
      act(fetchJSON("/users/" + userId, { method: "DELETE" }), loadUsers);

    return h("div", { className: "flex h-full min-h-0 w-full flex-col gap-4 p-4" },
      h("div", { className: "shrink-0 space-y-4 overflow-y-auto" },
        h("div", { className: "flex items-center justify-between" },
          h("div", null,
            h("h2", { className: "text-2xl font-bold tracking-tight" }, "Hermes Chat"),
            h("p", { className: "text-muted-foreground" }, "Control the Hermes Chat daemon.")
          ),
          h(StatusPill, { running: status?.running || false, healthy: status?.healthy || false })
        ),
        missingDeps.length > 0 && h("div", { className: "rounded-md border border-yellow-400 bg-yellow-50 dark:bg-yellow-950 dark:border-yellow-700 p-3 text-sm flex items-start justify-between gap-3" },
          h("div", null,
            h("span", { className: "font-semibold text-yellow-800 dark:text-yellow-200" }, "Missing dependencies: "),
            h("span", { className: "text-yellow-700 dark:text-yellow-300" }, missingDeps.join(", ")),
            h("p", { className: "text-yellow-600 dark:text-yellow-400 text-xs mt-1" }, "The daemon cannot start until these are installed.")
          ),
          h(Button, { onClick: onInstallDeps, disabled: loading, size: "sm", variant: "outline" }, loading ? "Installing..." : "Install Now")
        ),
        missingOptionalDeps.length > 0 && missingDeps.length === 0 && h("div", { className: "rounded-md border border-blue-400 bg-blue-50 dark:bg-blue-950 dark:border-blue-700 p-3 text-sm flex items-start justify-between gap-3" },
          h("div", null,
            h("span", { className: "font-semibold text-blue-800 dark:text-blue-200" }, "Optional dependencies missing: "),
            h("span", { className: "text-blue-700 dark:text-blue-300" }, missingOptionalDeps.map(d => d.requirement).join(", ")),
            h("p", { className: "text-blue-600 dark:text-blue-400 text-xs mt-1" }, missingOptionalDeps.map(d => d.feature).join(", "))
          ),
          h(Button, { onClick: onInstallDeps, disabled: loading, size: "sm", variant: "outline" }, loading ? "Installing..." : "Install")
        ),
        notice && h("div", { className: "rounded-md border border-green-400 bg-green-50 dark:bg-green-950 dark:border-green-700 p-3 text-sm flex items-center justify-between gap-3" },
          h("span", { className: "text-green-800 dark:text-green-200" }, "✓ " + notice),
          h("button", { onClick: () => setNotice(null), className: "text-green-600 dark:text-green-400 hover:opacity-70 text-xs" }, "✕")
        ),
        pendingRestart && h("div", { className: "rounded-md border border-orange-400 bg-orange-50 dark:bg-orange-950 dark:border-orange-700 p-3 text-sm flex items-center justify-between gap-3" },
          h("span", { className: "text-orange-800 dark:text-orange-200" }, "⚠ Restart required for network settings to take effect."),
          h(Button, { onClick: onRestartAfterSettings, disabled: loading, size: "sm" }, loading ? "Restarting..." : "Restart Now")
        ),
        error && h("div", { className: "rounded-md bg-destructive/15 p-3 text-destructive text-sm" }, error),
        h(Card, null,
          h(CardHeader, { className: "flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between" },
            h("div", null,
              h(CardTitle, null, "Daemon Status"),
              h("p", { className: "text-sm text-muted-foreground" }, "Current Hermes Chat process state and configuration.")
            ),
            h("div", { className: "flex flex-wrap gap-2" },
              h(Button, { onClick: onStart, disabled: loading || status?.running, size: "sm" }, "Start"),
              h(Button, { onClick: onStop, disabled: loading || !status?.running, size: "sm" }, "Stop"),
              h(Button, { onClick: onRestart, disabled: loading, size: "sm", variant: "outline" }, "Restart"),
              h(Button, { onClick: onInstallDeps, disabled: loading, size: "sm", variant: "outline" }, "Install Dependencies")
            )
          ),
          h(CardContent, null,
            h("div", { className: "grid grid-cols-2 md:grid-cols-4 gap-4 text-sm" },
              h("div", null,
                h("div", { className: "text-muted-foreground" }, "Running"),
                h("div", { className: "font-medium" }, status ? (status.running ? "Yes" : "No") : "—")
              ),
              h("div", null,
                h("div", { className: "text-muted-foreground" }, "Healthy"),
                h("div", { className: "font-medium" }, status ? (status.healthy ? "Yes" : "No") : "—")
              ),
              h("div", null,
                h("div", { className: "text-muted-foreground" }, "PID"),
                h("div", { className: "font-medium" }, status?.pid ?? "—")
              ),
              h("div", null,
                h("div", { className: "text-muted-foreground" }, "Port"),
                h("div", { className: "font-medium" }, status?.config?.port ?? "—")
              ),
              h("div", null,
                h("div", { className: "text-muted-foreground" }, "Bind Address"),
                h("div", { className: "font-medium" }, status?.config?.host ?? "—")
              ),
              h("div", null,
                h("div", { className: "text-muted-foreground" }, "Log Level"),
                h("div", { className: "font-medium" }, status?.config?.log_level ?? "—")
              ),
              h("div", null,
                h("div", { className: "text-muted-foreground" }, "Auto Start"),
                h("div", { className: "font-medium" }, status?.config?.auto_start ? "Yes" : "No")
              ),
              h("div", null,
                h("div", { className: "text-muted-foreground" }, "Debug"),
                h("div", { className: "font-medium" }, status?.config?.debug ? "Yes" : "No")
              )
            )
          )
        ),
        h("div", { className: "grid grid-cols-1 gap-4 lg:grid-cols-2" },
          h(Card, null,
            h(CardHeader, null,
              h(CardTitle, null, "Network Settings"),
              h("p", { className: "text-sm text-muted-foreground" }, "Bind address and port. Changes take effect after a daemon restart.")
            ),
            h(CardContent, null,
              h("form", { onSubmit: onSaveSettings, className: "flex flex-col sm:flex-row gap-3 items-end" },
                h("div", { className: "flex flex-col gap-1 flex-1" },
                  h("label", { className: "text-xs text-muted-foreground font-medium" }, "Bind Address"),
                  h("input", {
                    type: "text",
                    value: settingsHost,
                    onChange: (e) => setSettingsHost(e.target.value),
                    placeholder: "0.0.0.0",
                    className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
                  })
                ),
                h("div", { className: "flex flex-col gap-1 w-32" },
                  h("label", { className: "text-xs text-muted-foreground font-medium" }, "Port"),
                  h("input", {
                    type: "number",
                    value: settingsPort,
                    onChange: (e) => setSettingsPort(e.target.value),
                    placeholder: "6969",
                    min: "1",
                    max: "65535",
                    className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
                  })
                ),
                h(Button, { type: "submit", disabled: loading }, "Save")
              )
            )
          ),
          h(Card, null,
            h(CardHeader, null,
              h("div", { className: "flex items-center justify-between" },
                h(CardTitle, null, "Hermes Dashboard URL"),
                dashboardUrlSource && h("span", { className: "text-xs text-muted-foreground" }, "Source: " + dashboardUrlSource)
              ),
              h("p", { className: "text-sm text-muted-foreground" }, "Used to show your recently-used Hermes models in the chat model picker. Leave empty to auto-detect.")
            ),
            h(CardContent, { className: "space-y-3" },
              h("form", { onSubmit: onSaveDashboardUrl, className: "flex flex-col gap-3" },
                h("input", {
                  type: "text",
                  value: dashboardUrl,
                  onChange: (e) => setDashboardUrl(e.target.value),
                  placeholder: "http://127.0.0.1:9119",
                  className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
                }),
                h("div", { className: "flex flex-wrap gap-2" },
                  h(Button, { type: "submit", disabled: loading, size: "sm" }, "Save Override"),
                  h(Button, { type: "button", variant: "outline", size: "sm", onClick: onVerifyDashboardUrl, disabled: dashboardUrlLoading || !dashboardUrl.trim() }, dashboardUrlLoading ? "Verifying..." : "Verify"),
                  h(Button, { type: "button", variant: "ghost", size: "sm", onClick: onResetDashboardUrl, disabled: loading }, "Use Auto-Detect")
                )
              ),
              dashboardUrlVerify && (
                dashboardUrlVerify.ok
                  ? h("div", { className: "rounded-md border border-green-400 bg-green-50 dark:bg-green-950 dark:border-green-700 p-3 text-sm" }, "✓ Reached dashboard (" + (dashboardUrlVerify.model_count ?? "?") + " models found).")
                  : h("div", { className: "rounded-md border border-yellow-400 bg-yellow-50 dark:bg-yellow-950 dark:border-yellow-700 p-3 text-sm" },
                      h("div", { className: "font-semibold text-yellow-800 dark:text-yellow-200" }, "⚠ Could not verify dashboard URL"),
                      h("p", { className: "text-yellow-700 dark:text-yellow-300 text-xs mt-1" }, dashboardUrlVerify.error || "Unknown error"),
                      h("p", { className: "text-yellow-600 dark:text-yellow-400 text-xs mt-1" }, "The chat model picker will fall back to the full model catalog until this is fixed.")
                    )
              ),
              !dashboardUrlVerify && !dashboardUrl && h("div", { className: "rounded-md border border-orange-400 bg-orange-50 dark:bg-orange-950 dark:border-orange-700 p-3 text-sm" },
                h("span", { className: "text-orange-800 dark:text-orange-200" }, "⚠ No dashboard URL detected. Profile-style model switching in chat will not work until the Hermes dashboard URL is set or auto-detected.")
              )
            )
          )
        ),
        h("div", { className: "grid grid-cols-1 gap-4 lg:grid-cols-2" },
          h(Card, null,
            h(CardHeader, null,
              h(CardTitle, null, "Voice Settings"),
              h("p", { className: "text-sm text-muted-foreground" }, "Chat voice uses Hermes' own speech providers. Choose the voice and engine in ~/.hermes/config.yaml under tts. and stt.")
            ),
            h(CardContent, { className: "space-y-4" },
              !voiceStatus.tts && !voiceStatus.stt && h("div", { className: "rounded-md border border-yellow-400 bg-yellow-50 dark:bg-yellow-950 dark:border-yellow-700 p-3 text-sm text-yellow-800 dark:text-yellow-200" },
                "⚠ " + (voiceStatus.detail || "Hermes voice tools are unavailable. Install the Hermes voice extras (hermes-agent[voice]).")
              ),
              h("label", { className: "flex items-center justify-between rounded-md border px-4 py-3 cursor-pointer" },
                h("div", null,
                  h("div", { className: "text-sm font-medium" }, "Enable Voice"),
                  h("div", { className: "text-xs text-muted-foreground" }, voiceStatus.tts || voiceStatus.stt ? "Allow users to use voice input and output." : "Requires Hermes voice tools to be available.")
                ),
                h("input", {
                  type: "checkbox",
                  checked: voiceEnabled,
                  disabled: !voiceStatus.tts && !voiceStatus.stt,
                  onChange: (e) => setVoiceEnabled(e.target.checked),
                  className: "h-4 w-4 cursor-pointer"
                })
              ),
              h("div", { className: "rounded-md border px-4 py-3 space-y-1" },
                h("div", { className: "text-sm font-medium" }, "Hermes speech backends"),
                h("div", { className: "text-xs text-muted-foreground" },
                  "Speech to text: " + (voiceStatus.stt ? "Hermes" + (voiceStatus.sttProvider ? " (" + voiceStatus.sttProvider + ")" : "") : "unavailable")
                ),
                h("div", { className: "text-xs text-muted-foreground" },
                  "Text to speech: " + (voiceStatus.tts ? "Hermes" + (voiceStatus.ttsProvider ? " (" + voiceStatus.ttsProvider + ")" : "") : "unavailable")
                )
              ),
              h(Button, { onClick: onSaveVoiceSettings, disabled: loading, size: "sm" }, "Save Voice Settings")
            )
          ),
          h(Card, null,
            h(CardHeader, null,
              h(CardTitle, null, "Chat Users"),
              h("p", { className: "text-sm text-muted-foreground" }, "Add or remove users for the standalone chat UI. New users can log in immediately — no restart needed.")
            ),
            h(CardContent, { className: "space-y-4" },
              h("form", { onSubmit: onCreateUser, className: "flex flex-col gap-2" },
                h("div", { className: "flex flex-col sm:flex-row gap-2" },
                  h("input", {
                    type: "text",
                    placeholder: "Username",
                    value: newUsername,
                    onChange: (e) => setNewUsername(e.target.value),
                    required: true,
                    className: "flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
                  }),
                  h("input", {
                    type: "password",
                    placeholder: "Password",
                    value: newPassword,
                    onChange: (e) => setNewPassword(e.target.value),
                    required: true,
                    className: "flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm"
                  }),
                  h(Button, { type: "submit", disabled: loading }, "Add User")
                ),
                h("label", { className: "flex items-center gap-2 text-sm text-muted-foreground cursor-pointer" },
                  h("input", {
                    type: "checkbox",
                    checked: newFreeOnly,
                    onChange: (e) => setNewFreeOnly(e.target.checked),
                    className: "h-4 w-4 cursor-pointer"
                  }),
                  "Free models only (pricing.free or names ending in :free; profiles must end with :free)"
                )
              ),
              h("div", { className: "space-y-2" },
                users.length === 0 && h("p", { className: "text-sm text-muted-foreground" }, "No users yet."),
                users.map((u) => h("div", {
                  key: u.user_id,
                  className: "flex flex-col gap-2 rounded-md border px-3 py-2 text-sm sm:flex-row sm:items-center sm:justify-between"
                },
                  h("div", { className: "flex flex-wrap items-center gap-2 min-w-0" },
                    h("span", { className: "font-medium" }, u.username),
                    u.free_models_only && h("span", {
                      className: "rounded-sm bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400"
                    }, "Free only")
                  ),
                  h("div", { className: "flex items-center gap-2 shrink-0" },
                    h("label", { className: "flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer" },
                      h("input", {
                        type: "checkbox",
                        checked: !!u.free_models_only,
                        disabled: loading,
                        onChange: (e) => onToggleFreeOnly(u.user_id, e.target.checked),
                        className: "h-3.5 w-3.5 cursor-pointer"
                      }),
                      "Free only"
                    ),
                    h(Button, {
                      variant: "destructive",
                      size: "sm",
                      onClick: () => onDeleteUser(u.user_id),
                      disabled: loading
                    }, "Delete")
                  )
                ))
              )
            )
          )
        ),
        h(Card, null,
          h(CardHeader, null,
            h(CardTitle, null, "Starter Suggestions"),
            h("p", { className: "text-sm text-muted-foreground" },
              "Background job builds per-user suggestion pools. The chat UI samples chips instantly; static fallbacks are used when a pool is empty."
            )
          ),
          h(CardContent, { className: "space-y-4" },
            h("label", { className: "flex items-center justify-between rounded-md border px-4 py-3 cursor-pointer" },
              h("div", null,
                h("div", { className: "text-sm font-medium" }, "Enable background generation"),
                h("div", { className: "text-xs text-muted-foreground" }, "Uses the suggestion model (or Hermes default) on an interval per user.")
              ),
              h("input", {
                type: "checkbox",
                checked: suggestionsEnabled,
                onChange: (e) => setSuggestionsEnabled(e.target.checked),
                className: "h-4 w-4 cursor-pointer"
              })
            ),
            h("div", { className: "grid grid-cols-1 gap-3 sm:grid-cols-3" },
              h("div", { className: "flex flex-col gap-1" },
                h("label", { className: "text-xs text-muted-foreground font-medium" }, "Refresh interval (minutes)"),
                h("input", {
                  type: "number",
                  min: "5",
                  value: suggestionsInterval,
                  onChange: (e) => setSuggestionsInterval(e.target.value),
                  className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
                })
              ),
              h("div", { className: "flex flex-col gap-1" },
                h("label", { className: "text-xs text-muted-foreground font-medium" }, "Pool size"),
                h("input", {
                  type: "number",
                  min: "1",
                  max: "32",
                  value: suggestionsPoolSize,
                  onChange: (e) => setSuggestionsPoolSize(e.target.value),
                  className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
                })
              ),
              h("div", { className: "flex flex-col gap-1" },
                h("label", { className: "text-xs text-muted-foreground font-medium" }, "Chips shown"),
                h("input", {
                  type: "number",
                  min: "1",
                  max: "8",
                  value: suggestionsShowCount,
                  onChange: (e) => setSuggestionsShowCount(e.target.value),
                  className: "rounded-md border border-input bg-background px-3 py-2 text-sm"
                })
              )
            ),
            h("div", { className: "flex flex-col gap-2" },
              h("div", { className: "flex flex-wrap items-end justify-between gap-2" },
                h("div", { className: "flex flex-col gap-1 flex-1 min-w-[16rem]" },
                  h("label", { className: "text-xs text-muted-foreground font-medium" }, "Suggestion model"),
                  h("p", { className: "text-xs text-muted-foreground" },
                    "Same Hermes catalog as the chat model picker. Leave on default to use Hermes’s current model."
                  ),
                  (() => {
                    const encode = (model, provider) => (model ? `${provider || ""}|||${model}` : "");
                    const currentValue = encode(suggestionsModel, suggestionsProvider);
                    const groups = new Map();
                    for (const opt of suggestionModelOptions) {
                      const label = opt.provider_name || opt.provider || "Other";
                      if (!groups.has(label)) groups.set(label, []);
                      groups.get(label).push(opt);
                    }
                    const hasCurrent = !suggestionsModel || suggestionModelOptions.some(
                      (o) => o.id === suggestionsModel && (o.provider || "") === (suggestionsProvider || "")
                    );
                    const groupEntries = Array.from(groups.entries()).sort((a, b) => {
                      const aProf = a[0] === "Hermes Profiles" ? -1 : 0;
                      const bProf = b[0] === "Hermes Profiles" ? -1 : 0;
                      if (aProf !== bProf) return aProf - bProf;
                      return a[0].localeCompare(b[0]);
                    });
                    return h("select", {
                      value: currentValue,
                      onChange: (e) => {
                        const raw = e.target.value;
                        if (!raw) {
                          setSuggestionsModel("");
                          setSuggestionsProvider("");
                          return;
                        }
                        const sep = raw.indexOf("|||");
                        if (sep < 0) {
                          setSuggestionsModel(raw);
                          setSuggestionsProvider("");
                          return;
                        }
                        setSuggestionsProvider(raw.slice(0, sep));
                        setSuggestionsModel(raw.slice(sep + 3));
                      },
                      className: "rounded-md border border-input bg-background px-3 py-2 text-sm w-full max-w-xl"
                    },
                      h("option", { value: "" }, "Hermes default"),
                      !hasCurrent && suggestionsModel && h("option", {
                        value: currentValue
                      }, `${suggestionsModel}${suggestionsProvider ? " (" + suggestionsProvider + ")" : ""} — saved`),
                      groupEntries.map(([groupName, opts]) =>
                        h("optgroup", { key: groupName, label: groupName },
                          opts.map((opt) => h("option", {
                            key: `${opt.provider || ""}|||${opt.id}`,
                            value: encode(opt.id, opt.provider)
                          }, `${opt.name || opt.id}${opt.free ? " · free" : ""}`))
                        )
                      )
                    );
                  })()
                ),
                h(Button, {
                  type: "button",
                  variant: "outline",
                  size: "sm",
                  disabled: loading,
                  onClick: () => loadSuggestionsConfig()
                }, "Refresh models")
              ),
              suggestionModelsError && h("p", { className: "text-xs text-yellow-700 dark:text-yellow-300" },
                "Model list unavailable: " + suggestionModelsError + ". You can still keep the current saved model."
              ),
              suggestionsModel && h("p", { className: "text-xs text-muted-foreground" },
                "Selected: " + suggestionsModel + (suggestionsProvider ? " · provider " + suggestionsProvider : "")
              )
            ),
            h("div", { className: "flex flex-wrap gap-2" },
              h(Button, { onClick: onSaveSuggestionsSettings, disabled: loading, size: "sm" }, "Save Suggestion Settings"),
              h(Button, {
                type: "button",
                variant: "outline",
                size: "sm",
                disabled: loading,
                onClick: onGenerateSuggestionsNow
              }, loading ? "Working..." : "Generate now")
            ),
            h("p", { className: "text-xs text-muted-foreground" },
              "Generate now forces a fresh pool for every chat user (uses the selected suggestion model). Requires the Hermes Chat daemon to be running."
            ),
            h("div", { className: "flex flex-col gap-2 pt-2 border-t" },
              h("div", { className: "flex flex-wrap items-center justify-between gap-2" },
                h("div", null,
                  h("div", { className: "text-sm font-medium" }, "Generator prompt"),
                  h("p", { className: "text-xs text-muted-foreground" }, "Template file suggestions.md (placeholders: {{POOL_SIZE}}, {{MODE}}, {{MODE_INSTRUCTIONS}}, {{HISTORY}}).")
                ),
                h("div", { className: "flex flex-wrap gap-2" },
                  h(Button, {
                    type: "button",
                    variant: "outline",
                    size: "sm",
                    disabled: loading,
                    onClick: () => onRestoreSuggestionsPrompt("github")
                  }, "Restore from GitHub"),
                  h(Button, {
                    type: "button",
                    variant: "ghost",
                    size: "sm",
                    disabled: loading,
                    onClick: () => onRestoreSuggestionsPrompt("bundle")
                  }, "Restore bundled")
                )
              ),
              h("textarea", {
                value: suggestionsPrompt,
                onChange: (e) => {
                  setSuggestionsPrompt(e.target.value);
                  setSuggestionsPromptDirty(true);
                },
                rows: 12,
                className: "w-full rounded-md border border-input bg-background px-3 py-2 text-xs font-mono leading-relaxed"
              }),
              h(Button, {
                onClick: onSaveSuggestionsPrompt,
                disabled: loading || !suggestionsPromptDirty,
                size: "sm"
              }, "Save Prompt")
            )
          )
        )
      ),
      h(Card, { className: "flex min-h-0 flex-1 flex-col" },
        h(CardHeader, { className: "shrink-0" },
          h("div", { className: "flex items-center justify-between" },
            h(CardTitle, null, "Daemon Logs"),
            h("div", { className: "flex gap-2" },
              h(Button, {
                onClick: toggleLogsPause,
                variant: logsPaused ? "default" : "outline",
                size: "sm"
              }, logsPaused ? "▶ Resume" : "⏸ Pause"),
              h(Button, { onClick: onRefreshLogs, disabled: loading, size: "sm", variant: "outline" }, "Refresh"),
              h("a", {
                href: API_PREFIX + "/logs/download",
                download: "hermes-chat.log",
                className: "inline-flex items-center justify-center rounded-md border border-input bg-background px-3 py-1.5 text-xs font-medium shadow-sm hover:bg-accent hover:text-accent-foreground"
              }, "Download")
            )
          ),
          h("p", { className: "text-sm text-muted-foreground" },
            logsPaused
              ? "⏸ Log updates paused — scroll freely or copy."
              : "Last 100 lines from the Hermes Chat daemon log. Auto-scrolls to bottom."
          )
        ),
        h(CardContent, { className: "flex min-h-0 flex-1 flex-col", style: { padding: "0 1rem 1rem" } },
          h("pre", {
            ref: logsBoxRef,
            className: "h-full min-h-[16rem] flex-1 overflow-auto rounded-md bg-muted p-3 text-xs font-mono whitespace-pre-wrap break-all"
          },
            logs || "No logs yet."
          )
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("hermes-chat", HermesChatDashboard);
})();
