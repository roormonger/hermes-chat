import { useEffect, useMemo, useRef, useState, useCallback } from "react";
import {
  AssistantRuntimeProvider,
  useAui,
  useExternalStoreRuntime,
  CompositeAttachmentAdapter,
  SimpleImageAttachmentAdapter,
  type AppendMessage,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import { Thread } from "@/components/assistant-ui/thread";
import type { StarterSuggestion, VoicePhase } from "@/components/assistant-ui/thread";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Plus, Trash2, Pencil, Pin, MessageSquare, Check, X, LogOut, PanelLeftClose, PanelLeftOpen, Menu, Sun, Moon, ChevronUp, Download } from "lucide-react";
import { cn } from "@/lib/utils";
import { HermesFileAttachmentAdapter, isPdfAttachment } from "./lib/hermes-file-attachment-adapter";
import { apiFetch, getModels, getAnalyticsModels, getCurrentModel, getUsage, getActiveChatRun, getChatUsage, saveChatUsage, saveMessageUsage, setModel, startChatRun, streamChatRun, streamEvents, undoLastTurn, compressChat, branchChat, steerChat, speakText, getCommandsCatalog, catalogToSlashEntries, runSlashCommand, attachImage, attachPdf, attachFile, listHermesSessions, importHermesSession, type SseEvent, type SlashCommandEntry, type HermesSessionListItem } from "./api";
import { useAuth, AuthProvider, AuthGuard } from "./auth";
import { useAutoSpeak } from "./hooks/useAutoSpeak";
import { useVoiceCapabilities, type VoiceCapabilities } from "./hooks/useVoiceCapabilities";
import { useTheme } from "./hooks/useTheme";
import { useVoiceRecorder } from "./hooks/useVoiceRecorder";
import { startAudioPlayback, unlockAudioPlayback } from "./lib/audio-playback";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Chat = {
  chat_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  pinned?: number;
};

type Gate = {
  gateId: string;
  gateKind: string;
  options: string[];
  prompt: string;
};

type ToolStep = {
  id?: string;
  sourceId?: string;
  name: string;
  context?: string;
  summary?: string;
  durationS?: number;
  status: "running" | "done";
};

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  images?: string[];  // data URLs for user-attached images
  status?: "running" | "complete" | "error" | "cancelled";
  error?: string;
  activity?: "connecting" | "syncing" | "compacting" | "status" | "tool";
  activityText?: string;
  gate?: Gate | null;
  toolSteps?: ToolStep[];
  reasoning?: string;
  streamSeq?: number;
  createdAt?: number;
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
    cachedInputTokens?: number;
    reasoningTokens?: number;
    totalTokens?: number;
  };
};

const generateId = () => {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
};

/** Read `/chat/<id>` from the path; bare `/chat` → null. */
function chatIdFromPath(pathname = window.location.pathname): string | null {
  const match = pathname.match(/^\/chat\/([^/]+)\/?$/);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

function pathForChat(chatId: string | null): string {
  return chatId ? `/chat/${encodeURIComponent(chatId)}` : "/chat";
}

const IMAGE_URL_RE =
  /(?<![\!\[\]\(\)])https?:\/\/[^\s<>"{}|\\^\[\]]+\.(png|jpg|jpeg|gif|webp|svg|bmp|ico|tiff|avif)(?:\?[^\s<>"{}|\\^\[\]]*)?/gi;

function isInsideCode(text: string, offset: number): boolean {
  // Inline backticks: odd number of single backticks before offset means inside inline code.
  const before = text.slice(0, offset);
  const singleTicks = (before.match(/`/g) || []).length;
  if (singleTicks % 2 === 1) return true;

  // Triple-backtick code blocks: odd number of fences before offset means inside a block.
  const tripleFences = (before.match(/```/g) || []).length;
  return tripleFences % 2 === 1;
}

const formatAssistantText = (text: string): string => {
  // Convert explicit MEDIA: markers first.
  let out = text.replace(/MEDIA:\s*(https?:\/\/\S+)/g, "![image]($1)");

  // Convert standalone HTTP(S) image URLs to markdown images.
  out = out.replace(IMAGE_URL_RE, (match, _ext, offset, string) => {
    if (isInsideCode(string, offset)) return match;
    return `![image](${match})`;
  });

  return out;
};

const usageDelta = (before: ChatMessage["usage"], after: ChatMessage["usage"]): ChatMessage["usage"] | undefined => {
  if (!after) return undefined;
  const b = before ?? {};
  const a = after ?? {};
  const delta = {
    inputTokens: (a.inputTokens ?? 0) - (b.inputTokens ?? 0),
    outputTokens: (a.outputTokens ?? 0) - (b.outputTokens ?? 0),
    cachedInputTokens: (a.cachedInputTokens ?? 0) - (b.cachedInputTokens ?? 0),
    reasoningTokens: (a.reasoningTokens ?? 0) - (b.reasoningTokens ?? 0),
    totalTokens: 0,
  };
  delta.totalTokens = delta.inputTokens + delta.outputTokens + delta.cachedInputTokens + delta.reasoningTokens;
  if (delta.totalTokens <= 0) return undefined;
  return delta;
};

const normalizeUsage = (data: any): { usage: ChatMessage["usage"]; contextWindow?: number } => {
  const usage = data?.usage || {};
  const breakdown = data?.context_breakdown || {};
  const inputTokens = usage.input ?? usage.prompt ?? 0;
  const outputTokens = usage.output ?? 0;
  const totalTokens =
    usage.total ||
    inputTokens + outputTokens ||
    breakdown.context_used ||
    breakdown.estimated_total ||
    0;
  const normalized: ChatMessage["usage"] = {
    inputTokens,
    outputTokens,
    cachedInputTokens: usage.cache_read ?? usage.cached ?? 0,
    reasoningTokens: usage.reasoning ?? 0,
    totalTokens,
  };
  const contextWindow =
    breakdown.context_max ??
    breakdown.context_window ??
    usage.context_window ??
    0;
  return { usage: normalized, contextWindow };
};

const toThreadMessage = (msg: ChatMessage): ThreadMessageLike => ({
  id: msg.id,
  role: msg.role,
  content: [
    ...(msg.images ?? []).map((src) => ({ type: "image" as const, image: src })),
    ...(msg.reasoning?.trim()
      ? [{ type: "reasoning" as const, text: msg.reasoning }]
      : []),
    ...(msg.toolSteps ?? []).map((step, index) => ({
      type: "tool-call" as const,
      toolCallId: `${msg.id}-tool-${index}-${step.id || step.name}`,
      toolName: step.name,
      argsText: step.context ?? "",
      status:
        step.status === "running"
          ? ({ type: "running" } as const)
          : ({ type: "complete" } as const),
      result: step.summary ?? "",
      durationS: step.durationS,
    })),
    { type: "text", text: msg.content },
  ],
  status:
    msg.role === "assistant" && msg.status
      ? msg.status === "running"
        ? { type: "running" as const }
        : msg.status === "error"
          ? {
              type: "incomplete" as const,
              reason: "error" as const,
              error: msg.error || "An error occurred",
            }
          : msg.status === "cancelled"
            ? { type: "incomplete" as const, reason: "cancelled" as const }
            : { type: "complete" as const, reason: "stop" as const }
      : undefined,
  metadata: {
    custom: {
      ...(msg.gate ? { gate: msg.gate } : {}),
      ...(msg.activity ? { activity: msg.activity } : {}),
      ...(msg.activityText ? { activityText: msg.activityText } : {}),
      ...((msg.toolSteps ?? []).some((step) => step.status === "running")
        ? { runningTools: (msg.toolSteps ?? []).filter((step) => step.status === "running").map((step) => step.name) }
        : {}),
      createdAt: msg.createdAt ?? Date.now(),
      ...(msg.usage ? { usage: msg.usage } : {}),
    },
  },
});

const convertMessage = (msg: ThreadMessageLike): ThreadMessageLike => {
  if (msg.role !== "assistant" && msg.status) {
    const { status, ...rest } = msg;
    return rest;
  }
  return msg;
};

/** Empty-thread starter chips — static fallback when the server pool is empty. */
const STARTER_SUGGESTIONS: StarterSuggestion[] = [
  {
    title: "List files",
    label: "in the current directory",
    prompt: "What files are in the current directory?",
  },
  {
    title: "Running processes",
    label: "show what's active",
    prompt: "Show me running processes",
  },
  {
    title: "What can Hermes do?",
    label: "quick overview",
    prompt: "Summarize what Hermes can do",
  },
  {
    title: "System info",
    label: "OS, CPU, memory",
    prompt: "Give me a brief summary of this machine (OS, CPU, memory, disk).",
  },
];

const getAppendText = (msg: AppendMessage): string => {
  const parts = typeof msg.content === "string" ? [{ type: "text", text: msg.content }] : msg.content;
  return (parts as any[])
    .filter((p) => p.type === "text")
    .map((p) => p.text)
    .join("");
};

const getAppendImages = (msg: AppendMessage): Array<{ data: string; name: string }> => {
  const results: Array<{ data: string; name: string }> = [];
  for (const att of (msg.attachments ?? []) as any[]) {
    const name: string = att.name ?? "image";
    for (const part of (att.content ?? []) as any[]) {
      if (part.type === "image" && part.image) {
        results.push({ data: part.image as string, name });
      }
    }
  }
  return results;
};

/** Non-image composer attachments (PDF / file) carrying data URLs for Hermes upload. */
const getAppendFiles = (
  msg: AppendMessage,
): Array<{ dataUrl: string; name: string; mimeType: string; kind: "pdf" | "file" }> => {
  const results: Array<{ dataUrl: string; name: string; mimeType: string; kind: "pdf" | "file" }> = [];
  for (const att of (msg.attachments ?? []) as any[]) {
    const name: string = att.name ?? "attachment";
    const mimeType: string = att.contentType ?? "";
    for (const part of (att.content ?? []) as any[]) {
      if (part.type === "file" && part.data) {
        results.push({
          dataUrl: String(part.data),
          name: part.filename || name,
          mimeType: part.mimeType || mimeType,
          kind: isPdfAttachment(part.filename || name, part.mimeType || mimeType) ? "pdf" : "file",
        });
      }
    }
  }
  return results;
};

// ---------------------------------------------------------------------------
// Gate dialog
// ---------------------------------------------------------------------------

type ModelOption = {
  id: string;
  name: string;
  provider: string;
  providerName: string;
  isProfile?: boolean;
};

function flattenModelOptions(payload: any): ModelOption[] {
  const providers = payload?.providers ?? [];
  const options: ModelOption[] = [];
  for (const provider of providers) {
    const models = provider?.models ?? [];
    for (const model of models) {
      const id = model?.id || model?.model || String(model);
      const name = model?.name || model?.id || String(model);
      options.push({
        id,
        name,
        provider: provider.slug || provider.id || "unknown",
        providerName: provider.name || provider.slug || "Unknown",
      });
    }
  }
  return options;
}

const PROFILE_PROVIDER_NAME = "Hermes Profiles";

function profileNameFromModel(model: string): string {
  return model.split("/").pop() || model;
}

function flattenAnalyticsModels(payload: any): ModelOption[] {
  const models = payload?.models ?? [];
  const options: ModelOption[] = [];
  for (const model of models) {
    const id = model?.model || "";
    const provider = model?.provider || "";
    if (!id) continue;
    options.push({
      id,
      name: profileNameFromModel(id),
      provider,
      providerName: PROFILE_PROVIDER_NAME,
      isProfile: true,
    });
  }
  return options;
}

const EXPANDED_STORAGE_KEY = "hermes-chat:expanded-providers";

function loadExpanded(): Set<string> {
  try {
    const raw = localStorage.getItem(EXPANDED_STORAGE_KEY);
    if (raw) return new Set(JSON.parse(raw));
  } catch {}
  return new Set();
}

function saveExpanded(expanded: Set<string>) {
  try {
    localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify(Array.from(expanded)));
  } catch {}
}

function ModelPicker({
  chatId,
  open,
  onOpenChange,
  onModelChange,
  freeModelsOnly = false,
}: {
  chatId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onModelChange?: (model: string, provider: string, isProfile: boolean) => void;
  freeModelsOnly?: boolean;
}) {
  const [catalog, setCatalog] = useState<any>(null);
  const [analytics, setAnalytics] = useState<any>(null);
  const [current, setCurrent] = useState<{ model?: string; provider?: string; gateway?: string; api_provider?: string }>({});
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pending, setPending] = useState<{ model: string; provider: string; isProfile: boolean; message: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(() => loadExpanded());

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError(null);
    setCatalog(null);
    setAnalytics(null);
    Promise.all([
      getAnalyticsModels().catch(() => null),
      getModels().catch(() => null),
      chatId ? getCurrentModel(chatId) : getCurrentModel(),
    ])
      .then(([analyticsData, catalogData, currentData]) => {
        const model = currentData?.model || "";
        const provider = currentData?.provider || "";
        setCurrent({
          model,
          provider,
          gateway: currentData?.gateway || "",
          api_provider: currentData?.api_provider || "",
        });
        setAnalytics(analyticsData);
        setCatalog(catalogData);
        // If the active model is one of our profiles, reflect that in the top bar.
        if (model && analyticsData?.models) {
          const match = analyticsData.models.find(
            (m: any) => m.model === model && (m.provider || "") === provider
          );
          if (match) {
            onModelChange?.(model, provider, true);
          }
        }
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [open, chatId]);

  const options = useMemo(() => {
    const profiles = flattenAnalyticsModels(analytics);
    const catalogOpts = flattenModelOptions(catalog);
    return [...profiles, ...catalogOpts];
  }, [analytics, catalog]);

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    if (!term) return options;
    return options.filter(
      (o) =>
        o.name.toLowerCase().includes(term) ||
        o.providerName.toLowerCase().includes(term) ||
        o.provider.toLowerCase().includes(term)
    );
  }, [options, search]);

  const grouped = useMemo(() => {
    const map = new Map<string, ModelOption[]>();
    for (const opt of filtered) {
      if (!map.has(opt.providerName)) map.set(opt.providerName, []);
      map.get(opt.providerName)!.push(opt);
    }
    const entries = Array.from(map.entries());
    // Keep Hermes Profiles at the top; sort the rest alphabetically.
    entries.sort((a, b) => {
      const aProfile = a[0] === PROFILE_PROVIDER_NAME ? -1 : 0;
      const bProfile = b[0] === PROFILE_PROVIDER_NAME ? -1 : 0;
      if (aProfile !== bProfile) return aProfile - bProfile;
      return a[0].localeCompare(b[0]);
    });
    return entries;
  }, [filtered]);

  const toggleProvider = (providerName: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(providerName)) next.delete(providerName);
      else next.add(providerName);
      saveExpanded(next);
      return next;
    });
  };

  const handleSelect = async (opt: ModelOption) => {
    if (!chatId) return;
    setError(null);
    try {
      const result = await setModel(chatId, opt.id, opt.provider);
      if (result?.confirm_required) {
        setPending({ model: opt.id, provider: opt.provider, isProfile: !!opt.isProfile, message: result.confirm_message || result.warning || "Confirm model switch?" });
        setConfirmOpen(true);
        return;
      }
      setCurrent({ model: opt.id, provider: opt.provider });
      onModelChange?.(opt.id, opt.provider, !!opt.isProfile);
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleConfirm = async () => {
    if (!pending || !chatId) return;
    try {
      await setModel(chatId, pending.model, pending.provider, true);
      setCurrent({ model: pending.model, provider: pending.provider });
      onModelChange?.(pending.model, pending.provider, pending.isProfile);
      setConfirmOpen(false);
      setPending(null);
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message);
      setConfirmOpen(false);
    }
  };

  const currentProviderLabel = current.gateway || current.api_provider || current.provider || "";
  const isProfileCurrent = useMemo(() => {
    if (!current.model || !analytics?.models) return false;
    return analytics.models.some(
      (m: any) => m.model === current.model && (m.provider || "") === current.provider
    );
  }, [analytics, current]);
  const currentLabel = current.model
    ? isProfileCurrent
      ? `hermes/profile/${profileNameFromModel(current.model)}`
      : `${currentProviderLabel ? `${currentProviderLabel} / ` : ""}${current.model}`
    : "Loading…";

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-md max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>Switch Model</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">Current: {currentLabel}</p>
          <p className="text-xs text-muted-foreground/70">
            {freeModelsOnly
              ? "This account is limited to free models. Hermes Profiles appear only when their name ends with :free."
              : "Hermes Profiles are shown at the top. Expand any provider to see its models."}
          </p>
          <Input
            placeholder="Search models…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="mt-2"
          />
          {error && <p className="text-sm text-destructive mt-2">{error}</p>}
          <div className="flex-1 overflow-y-auto mt-2 space-y-2 pr-1 min-h-[200px]">
            {loading && <p className="text-sm text-muted-foreground">Loading models…</p>}
            {!loading && grouped.length === 0 && (
              <p className="text-sm text-muted-foreground">No models found.</p>
            )}
            {grouped.map(([providerName, items]) => {
              const isExpanded = expanded.has(providerName);
              return (
                <div key={providerName} className="rounded-md border border-border overflow-hidden">
                  <button
                    type="button"
                    onClick={() => toggleProvider(providerName)}
                    className="w-full flex items-center justify-between px-3 py-2 text-xs font-semibold text-muted-foreground uppercase tracking-wide bg-muted/40 hover:bg-muted/60 transition-colors"
                  >
                    <span>{providerName}</span>
                    <span className="text-muted-foreground/70">{isExpanded ? "▼" : "▶"}</span>
                  </button>
                  {isExpanded && (
                    <div className="p-2 space-y-1 bg-background">
                      {items.map((opt) => (
                        <button
                          key={`${opt.provider}:${opt.id}`}
                          onClick={() => handleSelect(opt)}
                          disabled={!chatId}
                          className={cn(
                            "w-full text-left px-2 py-1.5 rounded-md text-sm hover:bg-accent hover:text-accent-foreground transition-colors",
                            current.model === opt.id && current.provider === opt.provider
                              ? "bg-accent text-accent-foreground"
                              : ""
                          )}
                        >
                          {opt.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm Model Switch</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground whitespace-pre-wrap">{pending?.message}</p>
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleConfirm}>Switch</Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}


// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

function formatGroup(date: Date): string {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const startOfDate = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  const daysAgo = Math.floor((startOfToday.getTime() - startOfDate.getTime()) / (1000 * 60 * 60 * 24));

  if (daysAgo <= 0) return "Today";
  if (daysAgo === 1) return "Yesterday";
  if (daysAgo >= 2 && daysAgo <= 6) return DAY_NAMES[startOfDate.getDay()];
  if (daysAgo >= 7 && daysAgo <= 13) return "Last Week";
  if (daysAgo >= 14 && daysAgo <= 30) return "Last Month";
  return "Older";
}

function groupChats(chats: Chat[]): { label: string; items: Chat[]; pinned?: boolean }[] {
  const pinned = chats.filter((c) => c.pinned);
  const rest = chats.filter((c) => !c.pinned);

  const groups = new Map<string, Chat[]>();
  for (const chat of rest) {
    const label = formatGroup(new Date(chat.updated_at || chat.created_at));
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label)!.push(chat);
  }

  const dayOrder: string[] = [];
  const today = new Date();
  for (let i = 2; i <= 6; i++) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    d.setDate(d.getDate() - i);
    dayOrder.push(DAY_NAMES[d.getDay()]);
  }

  const order = ["Today", "Yesterday", ...dayOrder, "Last Week", "Last Month", "Older"];
  const dateGroups = order
    .filter((label) => groups.has(label))
    .map((label) => ({ label, items: groups.get(label)! }));

  return [
    ...(pinned.length > 0 ? [{ label: "Pinned", items: pinned, pinned: true }] : []),
    ...dateGroups,
  ];
}

function formatChatDate(value: string | number | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatHermesSessionWhen(startedAt: number): string {
  if (!startedAt) return "";
  // Hermes may return seconds or milliseconds.
  const ms = startedAt > 1e12 ? startedAt : startedAt * 1000;
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function sourceLabel(source: string): string {
  const map: Record<string, string> = {
    tui: "TUI",
    api_server: "API",
    cli: "CLI",
    desktop: "Desktop",
    acp: "ACP",
  };
  return map[source] || source || "Hermes";
}

function ImportHermesSessionsDialog({
  open,
  onOpenChange,
  onImported,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onImported: (chatId: string) => void;
}) {
  const [sessions, setSessions] = useState<HermesSessionListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [importingId, setImportingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setQuery("");
    (async () => {
      try {
        const data = await listHermesSessions(150);
        if (!cancelled) setSessions(data.sessions ?? []);
      } catch (e) {
        if (!cancelled) setError((e as Error).message || "Failed to list Hermes sessions");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(
      (s) =>
        s.title.toLowerCase().includes(q) ||
        s.preview.toLowerCase().includes(q) ||
        s.source.toLowerCase().includes(q) ||
        s.id.toLowerCase().includes(q),
    );
  }, [sessions, query]);

  const handleImport = async (session: HermesSessionListItem) => {
    if (session.imported && session.chat_id) {
      onImported(session.chat_id);
      onOpenChange(false);
      return;
    }
    setImportingId(session.id);
    setError(null);
    try {
      const result = await importHermesSession(session.id);
      onImported(result.chat_id);
      onOpenChange(false);
    } catch (e) {
      setError((e as Error).message || "Import failed");
    } finally {
      setImportingId(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg max-h-[85vh] flex flex-col gap-3">
        <DialogHeader>
          <DialogTitle>Import Hermes session</DialogTitle>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          Resume a TUI, CLI, or desktop conversation into Hermes Chat.
        </p>
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search sessions…"
          className="h-8 text-sm"
        />
        {error && (
          <p className="text-sm text-destructive break-words">{error}</p>
        )}
        <div className="flex-1 min-h-0 overflow-y-auto -mx-1 px-1 space-y-1 max-h-[50vh]">
          {loading && (
            <p className="text-sm text-muted-foreground px-2 py-4">Loading sessions…</p>
          )}
          {!loading && filtered.length === 0 && (
            <p className="text-sm text-muted-foreground px-2 py-4">
              {query.trim() ? "No matching sessions." : "No importable Hermes sessions found."}
            </p>
          )}
          {filtered.map((session) => (
            <button
              key={session.id}
              type="button"
              disabled={importingId !== null}
              onClick={() => void handleImport(session)}
              className={cn(
                "w-full text-left rounded-lg border px-3 py-2.5 transition-colors",
                "hover:bg-accent/50 disabled:opacity-60",
                session.imported && "opacity-75",
              )}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="font-medium text-sm truncate">{session.title}</div>
                  {session.preview && (
                    <div className="text-xs text-muted-foreground line-clamp-2 mt-0.5">
                      {session.preview}
                    </div>
                  )}
                  <div className="flex flex-wrap gap-x-2 gap-y-0.5 mt-1 text-[11px] text-muted-foreground">
                    <span>{sourceLabel(session.source)}</span>
                    <span>·</span>
                    <span>{session.message_count} msgs</span>
                    {formatHermesSessionWhen(session.started_at) && (
                      <>
                        <span>·</span>
                        <span>{formatHermesSessionWhen(session.started_at)}</span>
                      </>
                    )}
                  </div>
                </div>
                <span className="shrink-0 text-xs font-medium text-primary pt-0.5">
                  {importingId === session.id
                    ? "…"
                    : session.imported
                      ? "Open"
                      : "Import"}
                </span>
              </div>
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ChatSidebar({
  chats,
  currentChatId,
  onSelect,
  onNew,
  onImportSession,
  onRename,
  onDelete,
  onPin,
  username,
  onLogout,
  theme,
  onToggleTheme,
  voiceCaps,
  collapsed,
  onToggleCollapse,
  mobileOpen,
  onMobileClose,
}: {
  chats: Chat[];
  currentChatId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  onImportSession: (chatId: string) => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onPin: (id: string, pinned: boolean) => void;
  username: string;
  onLogout: () => void;
  theme: "light" | "dark";
  onToggleTheme: () => void;
  voiceCaps: VoiceCapabilities;
  collapsed: boolean;
  onToggleCollapse: () => void;
  mobileOpen: boolean;
  onMobileClose: () => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [profileOpen, setProfileOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const filteredChats = search.trim()
    ? chats.filter((c) => c.title.toLowerCase().includes(search.toLowerCase()))
    : chats;
  const groups = groupChats(filteredChats);

  return (
    <>
      {/* Mobile backdrop */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 md:hidden"
          onClick={onMobileClose}
        />
      )}
      <div
        className={cn(
          "bg-card flex flex-col h-full transition-all duration-200",
          /* Desktop: inline, collapsible */
          "hidden md:flex border-r",
          collapsed ? "w-16 items-center" : "w-72",
          /* Mobile: fixed slide-over */
          mobileOpen && "!flex fixed inset-y-0 left-0 z-50 w-72 shadow-xl",
        )}
      >
      <div className={cn("w-full flex items-center border-b p-2", collapsed ? "justify-center" : "justify-between")}>
        {!collapsed && (
          <div className="flex items-center gap-2 overflow-hidden">
            <img
              src="/static/hermes-logo.png"
              alt="Hermes"
              className="size-8 rounded-md object-contain shrink-0"
            />
            <span className="font-semibold text-sm truncate">Hermes Chat</span>
          </div>
        )}
        {collapsed && (
          <img
            src="/static/hermes-logo.png"
            alt="Hermes"
            className="size-8 rounded-md object-contain"
          />
        )}
        {mobileOpen ? (
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            onClick={onMobileClose}
            title="Close sidebar"
          >
            <X className="size-4" />
          </Button>
        ) : !collapsed ? (
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            onClick={onToggleCollapse}
            title="Collapse sidebar"
          >
            <PanelLeftClose className="size-4" />
          </Button>
        ) : (
          <Button
            variant="ghost"
            size="icon"
            className="size-8 shrink-0"
            onClick={onToggleCollapse}
            title="Expand sidebar"
          >
            <PanelLeftOpen className="size-4" />
          </Button>
        )}
      </div>

      <div className="p-2 w-full flex flex-col gap-2">
        <Button
          variant="outline"
          className={cn("h-10", collapsed ? "w-10 px-0 justify-center" : "w-full justify-start gap-2")}
          onClick={onNew}
          title="New Chat (Ctrl/Cmd+N)"
        >
          <Plus className="size-4" />
          {!collapsed && "New Chat"}
        </Button>
        <Button
          variant="ghost"
          className={cn("h-9", collapsed ? "w-10 px-0 justify-center" : "w-full justify-start gap-2")}
          onClick={() => setImportOpen(true)}
          title="Import Hermes session"
        >
          <Download className="size-4" />
          {!collapsed && "Import session"}
        </Button>
        {!collapsed && (
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search chats…"
            className="h-8 text-sm"
          />
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2 space-y-4 w-full">
        {groups.length === 0 && !collapsed && (
          <p className="text-sm text-muted-foreground px-2">
            {search.trim() ? "No matching chats." : "No chats yet."}
          </p>
        )}
        {groups.map(({ label, items }) => (
          <div key={label} className={cn("space-y-1", collapsed && "space-y-2")}>
            {!collapsed && (
              <h3 className="px-2 text-xs font-semibold text-muted-foreground uppercase tracking-wide">
                {label}
              </h3>
            )}
            {items.map((chat) => (
              <Tooltip key={chat.chat_id}>
                <TooltipTrigger asChild>
                  <div
                    className={cn(
                      "group relative flex items-center rounded-lg text-sm cursor-pointer transition-colors",
                      collapsed ? "justify-center size-10 p-0 mx-auto" : "gap-3 px-2 py-2",
                      currentChatId === chat.chat_id
                        ? "bg-accent text-accent-foreground"
                        : "hover:bg-muted"
                    )}
                    onClick={() => {
                      setConfirmDeleteId(null);
                      onSelect(chat.chat_id);
                    }}
                  >
                    <MessageSquare className="size-4 shrink-0 text-muted-foreground" />
                    {!collapsed && (
                  <>
                    {editingId === chat.chat_id ? (
                      <div className="flex flex-1 items-center gap-1">
                        <Input
                          value={editTitle}
                          onChange={(e) => setEditTitle(e.target.value)}
                          className="h-7 flex-1"
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              onRename(chat.chat_id, editTitle);
                              setEditingId(null);
                            }
                          }}
                          onClick={(e) => e.stopPropagation()}
                          autoFocus
                        />
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-6"
                          onClick={(e) => {
                            e.stopPropagation();
                            onRename(chat.chat_id, editTitle);
                            setEditingId(null);
                          }}
                        >
                          <Check className="size-3" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-6"
                          onClick={(e) => {
                            e.stopPropagation();
                            setEditingId(null);
                          }}
                        >
                          <X className="size-3" />
                        </Button>
                      </div>
                    ) : confirmDeleteId === chat.chat_id ? (
                      <div
                        className="flex flex-1 items-center gap-1"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <span className="flex-1 truncate text-xs text-destructive font-medium">
                          Delete?
                        </span>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7 text-destructive hover:text-destructive"
                          title="Confirm delete"
                          onClick={(e) => {
                            e.stopPropagation();
                            setConfirmDeleteId(null);
                            onDelete(chat.chat_id);
                          }}
                        >
                          <Check className="size-3" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-7"
                          title="Cancel"
                          onClick={(e) => {
                            e.stopPropagation();
                            setConfirmDeleteId(null);
                          }}
                        >
                          <X className="size-3" />
                        </Button>
                      </div>
                    ) : (
                      <>
                        <span className="flex-1 truncate">{chat.title}</span>
                        <div
                          className={cn(
                            "flex items-center gap-0.5",
                            currentChatId === chat.chat_id ? "opacity-100" : "opacity-0 group-hover:opacity-100"
                          )}
                        >
                          <Button
                            variant="ghost"
                            size="icon"
                            className={cn("size-7", chat.pinned ? "text-primary" : "")}
                            title={chat.pinned ? "Unpin" : "Pin"}
                            onClick={(e) => {
                              e.stopPropagation();
                              setConfirmDeleteId(null);
                              onPin(chat.chat_id, !chat.pinned);
                            }}
                          >
                            <Pin className="size-3" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7"
                            onClick={(e) => {
                              e.stopPropagation();
                              setConfirmDeleteId(null);
                              setEditingId(chat.chat_id);
                              setEditTitle(chat.title);
                            }}
                          >
                            <Pencil className="size-3" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="size-7 text-destructive hover:text-destructive"
                            title="Delete"
                            onClick={(e) => {
                              e.stopPropagation();
                              setEditingId(null);
                              setConfirmDeleteId(chat.chat_id);
                            }}
                          >
                            <Trash2 className="size-3" />
                          </Button>
                        </div>
                      </>
                    )}
                  </>
                )}
                  </div>
                </TooltipTrigger>
                <TooltipContent side="right" className="text-xs">
                  {formatChatDate(chat.updated_at || chat.created_at)}
                </TooltipContent>
              </Tooltip>
            ))}
          </div>
        ))}
      </div>

      <div className={cn("border-t w-full relative", collapsed ? "p-2 flex justify-center" : "p-3")}>
        {/* Upward popover panel */}
        {profileOpen && !collapsed && (
          <div className="absolute bottom-full left-3 right-3 mb-1 rounded-xl border bg-card shadow-lg z-50 overflow-hidden">
            <div className="p-3 border-b">
              <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Theme</p>
              <button
                onClick={onToggleTheme}
                className="flex items-center gap-2 w-full rounded-lg px-3 py-2 text-sm hover:bg-muted transition-colors"
              >
                {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
                {theme === "dark" ? "Switch to Light" : "Switch to Dark"}
              </button>
            </div>
            {(voiceCaps.ttsAvailable || voiceCaps.sttAvailable) && (
            <div className="p-3 border-b">
              <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-2">Voice</p>
              <p className="text-xs text-muted-foreground">
                Speech runs on Hermes
                {voiceCaps.sttProvider || voiceCaps.ttsProvider
                  ? ` (${[voiceCaps.sttProvider && `stt: ${voiceCaps.sttProvider}`, voiceCaps.ttsProvider && `tts: ${voiceCaps.ttsProvider}`].filter(Boolean).join(", ")})`
                  : ""}
                . Change the voice or engine in <code className="text-[11px]">~/.hermes/config.yaml</code>.
              </p>
            </div>
            )}
            <div className="p-3">
              <button
                onClick={() => { setProfileOpen(false); onLogout(); }}
                className="flex items-center gap-2 w-full rounded-lg px-3 py-2 text-sm text-destructive hover:bg-destructive/10 transition-colors"
              >
                <LogOut className="size-4" />
                Log out
              </button>
            </div>
          </div>
        )}
        <div
          className={cn(
            "flex items-center rounded-lg hover:bg-muted cursor-pointer",
            collapsed ? "justify-center size-10 p-0" : "justify-between gap-2 px-2 py-2"
          )}
          onClick={() => collapsed ? onLogout() : setProfileOpen((v) => !v)}
          title={collapsed ? "Logout" : "Account"}
        >
          <div className="flex items-center gap-2 min-w-0">
            <div className="size-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
              <span className="text-sm font-medium text-primary">
                {username.slice(0, 1).toUpperCase()}
              </span>
            </div>
            {!collapsed && <span className="text-sm font-medium truncate">{username}</span>}
          </div>
          {!collapsed && <ChevronUp className={cn("size-4 shrink-0 text-muted-foreground transition-transform", !profileOpen && "rotate-180")} />}
        </div>
      </div>
      </div>
      <ImportHermesSessionsDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        onImported={(chatId) => {
          onImportSession(chatId);
          onMobileClose();
        }}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Main app
// ---------------------------------------------------------------------------

function ChatApp() {
  const { user, logout } = useAuth();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [chats, setChats] = useState<Chat[]>([]);
  const [currentChatId, setCurrentChatId] = useState<string | null>(() => chatIdFromPath());
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const { autoSpeak, toggleAutoSpeak, enableAutoSpeak } = useAutoSpeak();
  const voiceCaps = useVoiceCapabilities();
  const { theme, toggle: toggleTheme } = useTheme();
  const [voiceMode, setVoiceMode] = useState(false);
  const [voicePhase, setVoicePhase] = useState<VoicePhase>("idle");
  const autoSpeakRef = useRef(autoSpeak);
  autoSpeakRef.current = autoSpeak;
  const voiceCapsRef = useRef(voiceCaps);
  voiceCapsRef.current = voiceCaps;
  const voiceModeRef = useRef(voiceMode);
  voiceModeRef.current = voiceMode;
  const speakReplyRef = useRef<(text: string) => void>(() => {});
  const [pendingGate, setPendingGate] = useState<Gate | null>(null);
  const [recoveryCandidate, setRecoveryCandidate] = useState<{ chatId: string; message: ChatMessage } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<
    { id: string; key?: string; text: string; level: string }[]
  >([]);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [currentModelDisplay, setCurrentModelDisplay] = useState<string>("");
  const [profileModels, setProfileModels] = useState<Set<string>>(new Set());
  const [sessionInfo, setSessionInfo] = useState<{
    model?: string;
    provider?: string;
    reasoning_effort?: string;
    service_tier?: string;
    fast?: boolean;
    yolo?: boolean;
  }>({});
  const [contextWindow, setContextWindow] = useState<number>(0);
  const [threadUsage, setThreadUsage] = useState<{
    inputTokens?: number;
    outputTokens?: number;
    cachedInputTokens?: number;
    reasoningTokens?: number;
    totalTokens?: number;
  }>({});
  const [starterSuggestions, setStarterSuggestions] = useState<StarterSuggestion[]>([...STARTER_SUGGESTIONS]);
  const [slashCommands, setSlashCommands] = useState<SlashCommandEntry[]>([]);
  const [composerDraft, setComposerDraft] = useState<string | null>(null);

  const assistantIdRef = useRef<string | null>(null);
  const assistantContentRef = useRef("");
  const assistantReasoningRef = useRef("");
  const assistantToolStepsRef = useRef<ToolStep[]>([]);
  const streamSeqRef = useRef(0);
  const runIdRef = useRef<string | null>(null);
  const currentChatIdRef = useRef(currentChatId);
  const abortControllerRef = useRef<AbortController | null>(null);
  const threadUsageRef = useRef<ChatMessage["usage"]>({});
  const usageBeforeRef = useRef<ChatMessage["usage"]>({});
  const usageKnownRef = useRef(false);
  const rafPendingRef = useRef(false);

  const selectChat = useCallback((chatId: string | null, opts?: { replace?: boolean }) => {
    setCurrentChatId(chatId);
    const next = pathForChat(chatId);
    if (window.location.pathname === next) return;
    if (opts?.replace) {
      window.history.replaceState(null, "", next);
    } else {
      window.history.pushState(null, "", next);
    }
  }, []);

  useEffect(() => {
    currentChatIdRef.current = currentChatId;
  }, [currentChatId]);

  useEffect(() => {
    const onPopState = () => {
      setCurrentChatId(chatIdFromPath());
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    const next = pathForChat(currentChatId);
    if (window.location.pathname !== next) {
      window.history.replaceState(null, "", next);
    }
  }, [currentChatId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiFetch("/v1/suggestions");
        const items = Array.isArray(data?.suggestions) ? data.suggestions : [];
        const normalized: StarterSuggestion[] = items
          .filter((s: any) => s && typeof s.prompt === "string" && s.prompt.trim())
          .map((s: any) => ({
            title: String(s.title || "").trim() || "Suggestion",
            label: String(s.label || "").trim(),
            prompt: String(s.prompt).trim(),
          }));
        if (!cancelled && normalized.length) {
          setStarterSuggestions(normalized);
        }
      } catch {
        /* keep static fallback */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const catalog = await getCommandsCatalog(currentChatId);
        if (cancelled) return;
        const entries = catalogToSlashEntries(catalog);
        // Always surface /new locally even if Hermes catalog omits it.
        if (!entries.some((e) => e.command === "/new")) {
          entries.unshift({ command: "/new", description: "Start a new chat" });
        }
        if (!entries.some((e) => e.command === "/clear")) {
          entries.unshift({ command: "/clear", description: "Clear and start a new conversation" });
        }
        setSlashCommands(entries);
      } catch {
        if (!cancelled) {
          setSlashCommands([
            { command: "/new", description: "Start a new chat" },
            { command: "/clear", description: "Clear and start a new conversation" },
            { command: "/help", description: "Show available slash commands" },
            { command: "/model", description: "Switch model (e.g. /model gpt-4o)" },
            { command: "/usage", description: "Show token usage for this session" },
            { command: "/compress", description: "Compress context (optionally: /compress <topic>)" },
          ]);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [currentChatId]);

  const loadChats = useCallback(async () => {
    try {
      const data = await apiFetch("/api/chats");
      setChats(data);
      const fromUrl = chatIdFromPath();
      if (fromUrl && !data.some((c: Chat) => c.chat_id === fromUrl)) {
        selectChat(null, { replace: true });
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, [selectChat]);

  const loadMessages = useCallback(async (chatId: string) => {
    try {
      const data = await apiFetch(`/api/chats/${chatId}/messages`);
      const loadedMessages: ChatMessage[] = data.map((m: any) => ({
        id: String(m.id),
        role: m.role,
        content: m.role === "assistant" ? formatAssistantText(m.content) : m.content,
        images: m.images?.length ? m.images : undefined,
        status: m.role === "assistant" ? ("complete" as const) : undefined,
        toolSteps: m.tool_steps?.length ? m.tool_steps : undefined,
        reasoning: m.reasoning?.trim() ? m.reasoning : undefined,
        streamSeq: Number(m.stream_seq || 0),
        createdAt: m.created_at ? m.created_at * 1000 : Date.now(),
        usage: m.usage || undefined,
      }));
      setMessages(loadedMessages);
      const latest = loadedMessages.at(-1);
      setRecoveryCandidate(
        latest?.role === "assistant" ? { chatId, message: latest } : null,
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const refreshUsage = useCallback(async (chatId?: string) => {
    const cid = chatId || currentChatIdRef.current;
    if (!cid) return;
    try {
      const data = await getUsage(cid);
      const { usage, contextWindow } = normalizeUsage(data);
      if (contextWindow) setContextWindow(contextWindow);
      const hasUsage = (usage.totalTokens ?? 0) > 0 || (usage.inputTokens ?? 0) > 0 || (usage.outputTokens ?? 0) > 0;
      if (!hasUsage) return;
      const currentTotal = threadUsageRef.current?.totalTokens ?? 0;
      if ((usage.totalTokens ?? 0) >= currentTotal) {
        setThreadUsage(usage);
        threadUsageRef.current = usage;
        usageKnownRef.current = true;
        saveChatUsage(cid, usage, contextWindow || undefined).catch(() => {});
      }
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    loadChats();
  }, [loadChats]);

  useEffect(() => {
    usageKnownRef.current = false;
    setRecoveryCandidate(null);
    setPendingGate(null);
    runIdRef.current = null;
    if (currentChatId) {
      loadMessages(currentChatId);
      setSessionInfo({});
      setContextWindow(0);
      setThreadUsage({});
      (async () => {
        try {
          const data = await getChatUsage(currentChatId);
          const usage = (data as any)?.usage;
          const savedContextWindow = (data as any)?.context_window;
          if (savedContextWindow) setContextWindow(savedContextWindow);
          if (usage && ((usage.totalTokens ?? 0) > 0 || (usage.inputTokens ?? 0) > 0 || (usage.outputTokens ?? 0) > 0)) {
            threadUsageRef.current = usage;
            setThreadUsage(usage);
            usageKnownRef.current = true;
          }
        } catch {
          // ignore
        } finally {
          refreshUsage(currentChatId);
        }
      })();
    } else {
      setMessages([]);
      setSessionInfo({});
      setContextWindow(0);
      setThreadUsage({});
    }
  }, [currentChatId, loadMessages]);

  useEffect(() => {
    if (!currentChatId) {
      setCurrentModelDisplay("");
      return;
    }
    Promise.all([getCurrentModel(currentChatId), getAnalyticsModels().catch(() => null)])
      .then(([data, analyticsData]) => {
        const model = data?.model || "";
        const provider = data?.gateway || data?.api_provider || data?.provider || "";
        const match = analyticsData?.models?.find(
          (m: any) => m.model === model && (m.provider || "") === (data?.provider || "")
        );
        if (match) {
          setProfileModels((prev) => new Set(prev).add(model));
          setCurrentModelDisplay(`hermes/profile/${profileNameFromModel(model)}`);
        } else {
          setCurrentModelDisplay(provider ? `${provider} / ${model}` : model);
        }
      })
      .catch(() => setCurrentModelDisplay(""));
  }, [currentChatId]);

  useEffect(() => {
    if (!currentChatId || isRunning) return;
    const interval = setInterval(() => {
      refreshUsage(currentChatId);
    }, 3000);
    return () => clearInterval(interval);
  }, [currentChatId, isRunning, refreshUsage]);

  const createChat = async () => {
    try {
      const data = await apiFetch("/api/chats", {
        method: "POST",
        body: JSON.stringify({ title: "New chat" }),
      });
      setChats((prev) => [data, ...prev]);
      selectChat(data.chat_id);
      setMobileSidebarOpen(false);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleImportedSession = useCallback(async (chatId: string) => {
    try {
      await loadChats();
      selectChat(chatId);
      setMobileSidebarOpen(false);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [loadChats, selectChat]);

  const createChatRef = useRef(createChat);
  createChatRef.current = createChat;

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
      if (e.key.toLowerCase() !== "n") return;
      e.preventDefault();
      void createChatRef.current();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const renameChat = async (chatId: string, title: string) => {
    try {
      await apiFetch(`/api/chats/${chatId}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      setChats((prev) =>
        prev.map((c) => (c.chat_id === chatId ? { ...c, title } : c))
      );
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const pinChat = async (chatId: string, pinned: boolean) => {
    try {
      await apiFetch(`/api/chats/${chatId}`, {
        method: "PATCH",
        body: JSON.stringify({ pinned }),
      });
      setChats((prev) =>
        prev.map((c) => (c.chat_id === chatId ? { ...c, pinned: pinned ? 1 : 0 } : c))
      );
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const deleteChat = async (chatId: string) => {
    try {
      await apiFetch(`/api/chats/${chatId}`, { method: "DELETE" });
      setChats((prev) => prev.filter((c) => c.chat_id !== chatId));
      if (currentChatId === chatId) {
        selectChat(null, { replace: true });
        setMessages([]);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleUndo = async () => {
    const chatId = currentChatIdRef.current;
    if (!chatId || isRunning) return;
    try {
      const result = await undoLastTurn(chatId);
      await loadMessages(chatId);
      const draft = (result?.prefill || "").trim();
      if (draft) setComposerDraft(draft);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleCompress = async () => {
    const chatId = currentChatIdRef.current;
    if (!chatId || isRunning) return;
    try {
      const result = await compressChat(chatId);
      await loadMessages(chatId);
      void refreshUsage(chatId);
      const before = result?.before_tokens ?? result?.before_messages;
      const after = result?.after_tokens ?? result?.after_messages;
      const summary = (result?.summary || "").toString().trim();
      const toastId = `compress-${Date.now()}`;
      const text =
        before != null && after != null
          ? `Compressed ${before} → ${after}${summary ? `: ${summary.slice(0, 120)}` : ""}`
          : summary || "Context compressed";
      setToasts((prev) => [...prev, { id: toastId, text, level: "info" }]);
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== toastId));
      }, 5000);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleBranch = async () => {
    const chatId = currentChatIdRef.current;
    if (!chatId || isRunning) return;
    try {
      const result = await branchChat(chatId);
      await loadChats();
      if (result?.chat_id) {
        selectChat(result.chat_id);
        const toastId = `branch-${Date.now()}`;
        setToasts((prev) => [
          ...prev,
          { id: toastId, text: `Branched → ${result.title || "new chat"}`, level: "info" },
        ]);
        setTimeout(() => {
          setToasts((prev) => prev.filter((t) => t.id !== toastId));
        }, 4000);
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const createBackendMessage = async (
    chatId: string,
    role: "user" | "assistant",
    content: string,
    images?: string[],
    toolSteps?: ToolStep[]
  ): Promise<string> => {
    const data = await apiFetch(`/api/chats/${chatId}/messages`, {
      method: "POST",
      body: JSON.stringify({ role, content, images: images ?? [], tool_steps: toolSteps ?? [] }),
    });
    return String(data.id);
  };

  const updateBackendMessage = async (
    chatId: string,
    messageId: string,
    content: string,
    toolSteps?: ToolStep[],
    reasoning?: string,
  ) => {
    await apiFetch(`/api/chats/${chatId}/messages/${messageId}`, {
      method: "PUT",
      body: JSON.stringify({
        content,
        tool_steps: toolSteps ?? [],
        reasoning: reasoning ?? null,
      }),
    });
  };

  const handleStreamEvent = useCallback(
    (event: SseEvent, chatId: string, assistantId: string) => {
      if (event.seq !== undefined) {
        if (event.seq <= streamSeqRef.current) return;
        streamSeqRef.current = event.seq;
      }
      setMessages((prev) => {
        const activeMessage = prev.find((message) => message.id === assistantId);
        if (!activeMessage?.activity && !activeMessage?.activityText) return prev;
        return prev.map((message) =>
          message.id === assistantId
            ? { ...message, activity: undefined, activityText: undefined }
            : message
        );
      });
      if (event.type === "text") {
        assistantContentRef.current = formatAssistantText(
          assistantContentRef.current + event.text
        );
        if (!rafPendingRef.current) {
          rafPendingRef.current = true;
          requestAnimationFrame(() => {
            rafPendingRef.current = false;
            const content = assistantContentRef.current;
            const id = assistantIdRef.current;
            if (!id) return;
            setMessages((prev) =>
              prev.map((m) => m.id === id ? { ...m, content } : m)
            );
          });
        }
      } else if (event.type === "reasoning") {
        if (event.replace) {
          assistantReasoningRef.current = event.text;
        } else {
          assistantReasoningRef.current += event.text;
        }
        const reasoning = assistantReasoningRef.current;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId ? { ...m, reasoning } : m
          )
        );
      } else if (event.type === "tool_generating") {
        const name = event.name || "tool";
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  activity: "tool",
                  activityText: `Calling ${name}…`,
                }
              : m
          )
        );
      } else if (event.type === "tool_start") {
        const sourceId = event.tool_id || event.name;
        const toolId = `${assistantId}-tool-${assistantToolStepsRef.current.length}-${sourceId}`;
        assistantToolStepsRef.current = [
          ...(assistantToolStepsRef.current ?? []),
          { id: toolId, sourceId, name: event.name, context: event.context, status: "running" as const },
        ];
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, toolSteps: assistantToolStepsRef.current, activity: undefined, activityText: undefined }
              : m
          )
        );
      } else if (event.type === "tool_progress") {
        const steps = [...(assistantToolStepsRef.current ?? [])];
        const sourceId = event.tool_id || event.name;
        const idxBySource = steps.findLastIndex(
          (step) => step.status === "running" && step.sourceId === sourceId,
        );
        const idx = idxBySource !== -1
          ? idxBySource
          : steps.findLastIndex((step) => step.status === "running" && step.name === event.name);
        if (idx !== -1 && event.text) {
          steps[idx] = { ...steps[idx], context: event.text };
          assistantToolStepsRef.current = steps;
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId ? { ...m, toolSteps: steps } : m
            )
          );
        }
      } else if (event.type === "tool_complete") {
        // eslint-disable-next-line no-console
        console.log("[tool_complete]", event.name, "result", event.result, "artifact", event.artifact);
        const steps = [...(assistantToolStepsRef.current ?? [])];
        const sourceId = event.tool_id || event.name;
        const idxBySource = steps.findLastIndex(
          (step) => step.status === "running" && step.sourceId === sourceId,
        );
        const idx = idxBySource !== -1
          ? idxBySource
          : steps.findLastIndex((step) => step.status === "running" && step.name === event.name);
        if (idx !== -1) {
          steps[idx] = {
            ...steps[idx],
            summary: event.summary,
            durationS: event.duration_s,
            status: "done" as const,
          };
        }
        assistantToolStepsRef.current = steps;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId ? { ...m, toolSteps: steps } : m
          )
        );
      } else if (event.type === "gate_interrupt") {
        const gate: Gate = {
          gateId: event.gate_id,
          gateKind: event.gate_kind || "approval",
          options: event.options || [],
          prompt: event.prompt || "",
        };
        assistantContentRef.current +=
          "\n\n🚦 Hermes needs your input: " + (event.prompt || "");
        setPendingGate(gate);
        // A decision is the user's to make by hand; don't keep the mic open.
        setVoicePhase("idle");
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: assistantContentRef.current, status: "running", gate }
              : m
          )
        );
        updateBackendMessage(chatId, assistantId, assistantContentRef.current, assistantToolStepsRef.current, assistantReasoningRef.current).catch(() => {});
      } else if (event.type === "turn_complete") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? { ...m, content: assistantContentRef.current, status: "complete", gate: null }
              : m
          )
        );
        setIsRunning(false);
        updateBackendMessage(chatId, assistantId, assistantContentRef.current, assistantToolStepsRef.current, assistantReasoningRef.current).catch(() => {});
        speakReplyRef.current(assistantContentRef.current);
      } else if (event.type === "status_update") {
        const kind = (event.kind || "").toLowerCase();
        const text = (event.text || "").trim();
        if (!kind && !text) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? { ...m, activity: undefined, activityText: undefined }
                : m
            )
          );
        } else {
          const activity =
            kind === "compacting" || kind.includes("compact")
              ? ("compacting" as const)
              : ("status" as const);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? {
                    ...m,
                    activity,
                    activityText: text || (activity === "compacting" ? "Compacting context…" : kind),
                  }
                : m
            )
          );
        }
      } else if (event.type === "notification") {
        if (event.text?.trim()) {
          const id = event.id || event.key || `n-${Date.now()}`;
          setToasts((prev) => {
            const without = prev.filter((t) => t.id !== id && (!event.key || t.key !== event.key));
            return [
              ...without,
              {
                id,
                key: event.key,
                text: event.text.trim(),
                level: event.level || "info",
              },
            ];
          });
          window.setTimeout(() => {
            setToasts((prev) => prev.filter((t) => t.id !== id));
          }, 8000);
        }
      } else if (event.type === "notification_clear") {
        setToasts((prev) =>
          prev.filter((t) => {
            if (event.id && t.id === event.id) return false;
            if (event.key && t.key === event.key) return false;
            return true;
          })
        );
      } else if (event.type === "session_title") {
        if (event.title) {
          setChats((prev) =>
            prev.map((c) => (c.chat_id === chatId ? { ...c, title: event.title } : c))
          );
          apiFetch(`/api/chats/${chatId}`, {
            method: "PATCH",
            body: JSON.stringify({ title: event.title }),
          }).catch(() => {});
        }
      } else if (event.type === "session_info") {
        setSessionInfo({
          model: event.model,
          provider: event.gateway || event.api_provider || event.provider,
          reasoning_effort: event.reasoning_effort,
          service_tier: event.service_tier,
          fast: event.fast,
          yolo: event.yolo,
        });
        if (event.context_window) {
          setContextWindow(event.context_window);
        }
        const nextUsage = {
          inputTokens: event.input_tokens,
          outputTokens: event.output_tokens,
          cachedInputTokens: event.cache_read_tokens,
          reasoningTokens: event.reasoning_tokens,
          totalTokens: event.total_tokens || (event.input_tokens ?? 0) + (event.output_tokens ?? 0),
        };
        setThreadUsage(nextUsage);
        threadUsageRef.current = nextUsage;
        if (event.model) {
          const provider = event.gateway || event.api_provider || event.provider || "";
          const providerLower = provider.toLowerCase();
          const modelLower = event.model.toLowerCase();
          if (profileModels.has(event.model)) {
            setCurrentModelDisplay(`hermes/profile/${profileNameFromModel(event.model)}`);
          } else {
            // Hermes sometimes reports the model vendor ("deepseek") as provider
            // while the real gateway ("openrouter") lives elsewhere. Avoid
            // overwriting the top-bar gateway label with a vendor prefix.
            const isVendorOnly = providerLower && modelLower.startsWith(`${providerLower}/`);
            if (!isVendorOnly) {
              setCurrentModelDisplay(provider ? `${provider} / ${event.model}` : event.model);
            }
          }
        }
      } else if (event.type === "process_exit" || event.type === "error") {
        const failure = event.type === "error" ? event.message : "Hermes stopped before returning a response.";
        setError(failure);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: assistantContentRef.current,
                  status: "error" as const,
                  error: failure,
                  gate: null,
                  activity: undefined,
                }
              : m
          )
        );
        setIsRunning(false);
        updateBackendMessage(chatId, assistantId, assistantContentRef.current, assistantToolStepsRef.current, assistantReasoningRef.current).catch(() => {});
      }
    },
    [setChats]
  );

  useEffect(() => {
    if (!recoveryCandidate || recoveryCandidate.chatId !== currentChatId) return;

    const ac = new AbortController();
    const recover = async () => {
      try {
        const status = await getActiveChatRun(recoveryCandidate.chatId);
        const run = status.run;
        if (ac.signal.aborted || !status.active || !run) return;

        const assistantId = recoveryCandidate.message.id;
        assistantIdRef.current = assistantId;
        assistantContentRef.current = recoveryCandidate.message.content;
        assistantReasoningRef.current = recoveryCandidate.message.reasoning ?? "";
        assistantToolStepsRef.current = recoveryCandidate.message.toolSteps ?? [];
        streamSeqRef.current = recoveryCandidate.message.streamSeq ?? 0;
        runIdRef.current = run.run_id;
        abortControllerRef.current = ac;
        setIsRunning(true);

        const recoveredGate = run.pending_gate
          ? {
              gateId: run.pending_gate.gate_id,
              gateKind: run.pending_gate.gate_kind || "approval",
              options: run.pending_gate.options || [],
              prompt: run.pending_gate.prompt || "",
            }
          : null;
        if (recoveredGate) setPendingGate(recoveredGate);
        setMessages((prev) => prev.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                status: "running",
                activity: recoveredGate ? undefined : "syncing",
                gate: recoveredGate ?? message.gate,
              }
            : message,
        ));
        await streamChatRun(
          run.run_id,
          streamSeqRef.current,
          (event) => handleStreamEvent(event, recoveryCandidate.chatId, assistantId),
          ac.signal,
        );
      } catch (e) {
        if ((e as Error).name !== "AbortError") {
          const failure = (e as Error).message || "Failed to resume the active run.";
          const failedAssistantId = recoveryCandidate.message.id;
          setError(failure);
          setMessages((prev) =>
            prev.map((message) =>
              message.id === failedAssistantId
                ? {
                    ...message,
                    status: "error" as const,
                    error: failure,
                    activity: undefined,
                  }
                : message,
            ),
          );
        }
      } finally {
        if (!ac.signal.aborted) setIsRunning(false);
        if (abortControllerRef.current === ac) abortControllerRef.current = null;
      }
    };

    recover();
    return () => ac.abort();
  }, [currentChatId, handleStreamEvent, recoveryCandidate]);

  const streamAssistant = async (chatId: string, userText: string) => {
    setIsRunning(true);
    setError(null);
    assistantContentRef.current = "";
    assistantReasoningRef.current = "";
    assistantToolStepsRef.current = [];
    streamSeqRef.current = 0;
    runIdRef.current = null;

    const assistantId = await createBackendMessage(chatId, "assistant", "", [], []);
    assistantIdRef.current = assistantId;
    setMessages((prev) => [
      ...prev,
      { id: assistantId, role: "assistant", content: "", status: "running", activity: "connecting", toolSteps: [], createdAt: Date.now() },
    ]);
    let beforeUsage: ChatMessage["usage"] = {};
    try {
      const beforeData = await getUsage(chatId);
      const { usage, contextWindow } = normalizeUsage(beforeData);
      beforeUsage = usage;
      if (contextWindow) setContextWindow(contextWindow);
    } catch {}
    const hasBefore =
      (beforeUsage.totalTokens ?? 0) > 0 ||
      (beforeUsage.inputTokens ?? 0) > 0 ||
      (beforeUsage.outputTokens ?? 0) > 0;
    usageKnownRef.current = usageKnownRef.current || hasBefore;
    usageBeforeRef.current = beforeUsage;


    const ac = new AbortController();
    abortControllerRef.current = ac;
    try {
      try {
        const run = await startChatRun(chatId, userText, assistantId);
        runIdRef.current = run.run_id;
        await streamChatRun(
          run.run_id,
          streamSeqRef.current,
          (event) => handleStreamEvent(event, chatId, assistantId),
          ac.signal,
        );
      } catch (e) {
        const message = (e as Error).message;
        if (runIdRef.current || (!message.includes("Durable chat runs require") && !message.includes("Not Found"))) {
          throw e;
        }
        await streamEvents(
          "/v1/chat",
          { chat_id: chatId, message: userText },
          (event) => handleStreamEvent(event, chatId, assistantId),
          ac.signal,
        );
      }
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        setIsRunning(false);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: assistantContentRef.current,
                  status: "cancelled" as const,
                  gate: null,
                  activity: undefined,
                }
              : m
          )
        );
        return;
      }
      const failure = (e as Error).message || "Hermes failed before returning a response.";
      setError(failure);
      setIsRunning(false);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                content: assistantContentRef.current,
                status: "error" as const,
                error: failure,
                gate: null,
                activity: undefined,
              }
            : m
        )
      );
      updateBackendMessage(chatId, assistantId, assistantContentRef.current, assistantToolStepsRef.current, assistantReasoningRef.current).catch(() => {});
    }

    let afterUsage: ChatMessage["usage"] = {};
    try {
      const afterData = await getUsage(chatId);
      const { usage, contextWindow } = normalizeUsage(afterData);
      afterUsage = usage;
      if (contextWindow) setContextWindow(contextWindow);
    } catch {}

    const delta = usageDelta(beforeUsage, afterUsage);
    if (delta) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, usage: delta } : m
        )
      );
      saveMessageUsage(chatId, assistantId, delta).catch(() => {});

      const current = threadUsageRef.current ?? {};
      const next = {
        inputTokens: (current.inputTokens ?? 0) + (delta.inputTokens ?? 0),
        outputTokens: (current.outputTokens ?? 0) + (delta.outputTokens ?? 0),
        cachedInputTokens: (current.cachedInputTokens ?? 0) + (delta.cachedInputTokens ?? 0),
        reasoningTokens: (current.reasoningTokens ?? 0) + (delta.reasoningTokens ?? 0),
        totalTokens: 0,
      };
      next.totalTokens = next.inputTokens + next.outputTokens + next.cachedInputTokens + next.reasoningTokens;
      threadUsageRef.current = next;
      setThreadUsage(next);
      saveChatUsage(chatId, next, contextWindow || undefined).catch(() => {});
      usageKnownRef.current = true;
    } else if (hasBefore) {
      const currentTotal = threadUsageRef.current?.totalTokens ?? 0;
      if ((afterUsage.totalTokens ?? 0) >= currentTotal) {
        threadUsageRef.current = afterUsage;
        setThreadUsage(afterUsage);
        saveChatUsage(chatId, afterUsage, contextWindow || undefined).catch(() => {});
      }
    }
    setIsRunning(false);
  };

  const handleGateChoice = async (choice: string): Promise<boolean> => {
    const chatId = currentChatIdRef.current;
    if (!chatId || !pendingGate || !choice.trim()) return false;
    const gate = pendingGate;
    setError(null);

    try {
      await apiFetch("/v1/gate/resolve", {
        method: "POST",
        body: JSON.stringify({
          chat_id: chatId,
          gate_id: gate.gateId,
          gate_kind: gate.gateKind || "approval",
          choice: choice.trim(),
        }),
      });
      setPendingGate(null);
      const assistantId = assistantIdRef.current;
      if (assistantId) {
        setMessages((prev) => prev.map((message) =>
          message.id === assistantId ? { ...message, gate: null, status: "running" } : message,
        ));
      }
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  };

  const threadMessages = useMemo(
    () => messages.map(toThreadMessage),
    [messages]
  );

  const attachmentAdapter = useMemo(
    () => new CompositeAttachmentAdapter([
      new SimpleImageAttachmentAdapter(),
      new HermesFileAttachmentAdapter(),
    ]),
    []
  );

  const runtime = useExternalStoreRuntime({
    messages: threadMessages,
    isRunning,
    convertMessage,
    adapters: { attachments: attachmentAdapter },
    onCancel: async () => {
      abortControllerRef.current?.abort();
      abortControllerRef.current = null;
      const chatId = currentChatIdRef.current;
      if (chatId) {
        try {
          await apiFetch("/v1/chat/cancel", {
            method: "POST",
            body: JSON.stringify({ chat_id: chatId }),
          });
        } catch (e) {
          console.warn("cancel request failed:", e);
        }
      }
      const assistantId = assistantIdRef.current;
      setIsRunning(false);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId
            ? {
                ...m,
                status: "cancelled" as const,
                gate: null,
                activity: undefined,
              }
            : m
        )
      );
      if (assistantId && chatId) {
        updateBackendMessage(chatId, assistantId, assistantContentRef.current, assistantToolStepsRef.current, assistantReasoningRef.current).catch(() => {});
      }
    },
    onNew: async (message: AppendMessage) => {
      const text = getAppendText(message);
      if (pendingGate) {
        await handleGateChoice(text);
        return;
      }
      if (isRunning) {
        const chatId = currentChatIdRef.current;
        const trimmed = text.trim();
        if (!chatId || !trimmed) return;
        try {
          const result = await steerChat(chatId, trimmed);
          const status = (result as any)?.status || "queued";
          if (status === "rejected") {
            setError("Hermes rejected the mid-turn steer. Try again after the next tool step.");
            return;
          }
          const preview = trimmed.length > 80 ? `${trimmed.slice(0, 80)}…` : trimmed;
          const toastId = `steer-${Date.now()}`;
          setToasts((prev) => [
            ...prev,
            { id: toastId, text: `Steered: ${preview}`, level: "info" },
          ]);
          window.setTimeout(() => {
            setToasts((prev) => prev.filter((t) => t.id !== toastId));
          }, 6000);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantIdRef.current
                ? { ...m, activity: "status", activityText: "Steering Hermes…" }
                : m
            )
          );
        } catch (e) {
          setError((e as Error).message || "Failed to steer");
        }
        return;
      }
      const images = getAppendImages(message);
      const files = getAppendFiles(message);
      if (!text.trim() && images.length === 0 && files.length === 0) return;

      const trimmed = text.trim();
      // Slash commands: local UI actions or Hermes slash.exec / command.dispatch.
      if (trimmed.startsWith("/") && images.length === 0 && files.length === 0) {
        const cmdName = trimmed.slice(1).split(/\s+/)[0]?.toLowerCase() || "";
        if (cmdName === "new" || cmdName === "clear") {
          await createChatRef.current();
          return;
        }

        let chatId = currentChatIdRef.current;
        if (!chatId) {
          const data = await apiFetch("/api/chats", {
            method: "POST",
            body: JSON.stringify({ title: "New chat" }),
          });
          setChats((prev) => [data, ...prev]);
          selectChat(data.chat_id);
          chatId = data.chat_id;
        }

        await createBackendMessage(chatId, "user", trimmed);
        const now = Date.now();
        setMessages((prev) => [
          ...prev,
          { id: generateId(), role: "user", content: trimmed, createdAt: now },
        ]);

        try {
          const result = await runSlashCommand(chatId, trimmed);
          const notice = (result.notice || "").trim();
          if (notice) {
            const noticeId = await createBackendMessage(chatId, "assistant", notice);
            setMessages((prev) => [
              ...prev,
              {
                id: noticeId,
                role: "assistant",
                content: notice,
                status: "complete",
                createdAt: Date.now(),
              },
            ]);
          }

          if (result.action === "output") {
            const out = (result.text || "(no output)").trim() || "(no output)";
            const outId = await createBackendMessage(chatId, "assistant", out);
            setMessages((prev) => [
              ...prev,
              {
                id: outId,
                role: "assistant",
                content: out,
                status: "complete",
                createdAt: Date.now(),
              },
            ]);
            return;
          }

          if (result.action === "prefill") {
            const draft = (result.message || "").trim();
            if (draft) setComposerDraft(draft);
            return;
          }

          if (result.action === "skill" || result.action === "send") {
            const prompt = (result.message || "").trim();
            if (!prompt) {
              setError(`/${cmdName}: empty message`);
              return;
            }
            if (result.action === "skill" && result.name) {
              const tip = `⚡ loading skill: ${result.name}`;
              const tipId = await createBackendMessage(chatId, "assistant", tip);
              setMessages((prev) => [
                ...prev,
                {
                  id: tipId,
                  role: "assistant",
                  content: tip,
                  status: "complete",
                  createdAt: Date.now(),
                },
              ]);
            }
            await streamAssistant(chatId, prompt);
            return;
          }

          setError(`/${cmdName}: unrecognized command response`);
        } catch (e) {
          const errText = (e as Error).message || "Slash command failed";
          setError(errText);
          const errId = await createBackendMessage(chatId, "assistant", `error: ${errText}`);
          setMessages((prev) => [
            ...prev,
            {
              id: errId,
              role: "assistant",
              content: `error: ${errText}`,
              status: "error",
              error: errText,
              createdAt: Date.now(),
            },
          ]);
        }
        return;
      }

      let chatId = currentChatIdRef.current;
      if (!chatId) {
        const data = await apiFetch("/api/chats", {
          method: "POST",
          body: JSON.stringify({ title: "New chat" }),
        });
        setChats((prev) => [data, ...prev]);
        selectChat(data.chat_id);
        chatId = data.chat_id;
      }

      // Upload attachments to Hermes before prompt.submit.
      // Images/PDFs → vision tiles; other files → @file: refs inserted into the prompt.
      for (const img of images) {
        try {
          await attachImage(chatId, img.data, img.name);
        } catch (e) {
          setError(`Failed to attach image: ${(e as Error).message}`);
          return;
        }
      }

      const fileRefs: string[] = [];
      for (const file of files) {
        try {
          if (file.kind === "pdf") {
            const pdfResult = await attachPdf(chatId, file.dataUrl, file.name);
            // When pdftoppm is missing the backend falls back to file.attach.
            if (pdfResult?.ref_text) {
              fileRefs.push(String(pdfResult.ref_text));
            }
          } else {
            const result = await attachFile(chatId, file.dataUrl, file.name);
            if (result?.ref_text) {
              fileRefs.push(String(result.ref_text));
            } else {
              setError(`Failed to attach ${file.name}: no @file: ref returned`);
              return;
            }
          }
        } catch (e) {
          setError(`Failed to attach ${file.name}: ${(e as Error).message}`);
          return;
        }
      }

      const promptText = [fileRefs.join(" "), text].filter((s) => s.trim()).join("\n\n");
      if (!promptText.trim() && images.length === 0) return;

      await createBackendMessage(chatId, "user", promptText || text, images.map((img) => img.data));
      const now = Date.now();
      setMessages((prev) => [
        ...prev,
        {
          id: generateId(),
          role: "user",
          content: promptText || text,
          images: images.map((img) => img.data),
          createdAt: now,
        },
      ]);
      await streamAssistant(chatId, promptText || text);
    },
    onEdit: async (message: AppendMessage) => {
      const chatId = currentChatIdRef.current;
      const sourceId = (message as any).sourceId;
      if (!chatId || !sourceId) return;
      const text = getAppendText(message);
      const existing = messages.find((m) => m.id === sourceId);
      const existingToolSteps = existing?.toolSteps;
      try {
        await updateBackendMessage(chatId, sourceId, text, existingToolSteps, existing?.reasoning);
        setMessages((prev) =>
          prev.map((m) =>
            m.id === sourceId ? { ...m, content: text } : m
          )
        );
      } catch (e) {
        setError((e as Error).message);
      }
    },
    onReload: async (parentId: string | null) => {
      const chatId = currentChatIdRef.current;
      if (!chatId || isRunning) return;

      // session.undo only rewinds the latest turn — only regenerate the last reply.
      let parentIndex = parentId
        ? messages.findIndex((m) => m.id === parentId)
        : -1;
      if (parentIndex < 0) {
        for (let i = messages.length - 1; i >= 0; i--) {
          if (messages[i].role === "user") {
            parentIndex = i;
            break;
          }
        }
      }
      if (parentIndex < 0) return;

      const parent = messages[parentIndex];
      if (parent.role !== "user") return;

      let lastUserIndex = -1;
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i].role === "user") {
          lastUserIndex = i;
          break;
        }
      }
      if (parentIndex !== lastUserIndex) {
        setError("Can only regenerate the latest response.");
        return;
      }

      const userText = parent.content;
      const images = parent.images ?? [];

      try {
        await undoLastTurn(chatId);
        setMessages([
          ...messages.slice(0, parentIndex),
          {
            id: generateId(),
            role: "user",
            content: userText,
            images,
            createdAt: Date.now(),
          },
        ]);
        await createBackendMessage(chatId, "user", userText, images);

        for (const img of images) {
          try {
            await apiFetch("/v1/image/attach", {
              method: "POST",
              body: JSON.stringify({
                chat_id: chatId,
                content_base64: img,
                filename: "reload-attachment.png",
              }),
            });
          } catch (e) {
            setError(`Failed to re-attach image: ${(e as Error).message}`);
            await loadMessages(chatId);
            return;
          }
        }

        await streamAssistant(chatId, userText);
      } catch (e) {
        setError((e as Error).message);
        try {
          await loadMessages(chatId);
        } catch {
          /* ignore recovery failure */
        }
      }
    },
  });

  // ---------------------------------------------------------------------------
  // Hands-free voice mode
  //
  // Mirrors Hermes' own voice mode: listen, submit on silence, speak the reply,
  // listen again. The loop lives here rather than in the composer because
  // re-arming the mic depends on the turn lifecycle and on playback finishing.
  // ---------------------------------------------------------------------------
  const voiceAudioRef = useRef<HTMLAudioElement | null>(null);

  const {
    recording: voiceRecording,
    transcribing: voiceTranscribing,
    start: startVoiceCapture,
    cancel: cancelVoiceCapture,
  } = useVoiceRecorder(
    (text) => {
      setVoicePhase("thinking");
      runtime.thread.append({ role: "user", content: [{ type: "text", text }] });
    },
    {
      autoStopOnSilence: true,
      onIdle: () => setVoicePhase("idle"),
      onError: (e) => {
        setError((e as Error)?.message || "Voice input failed.");
        voiceModeRef.current = false;
        setVoiceMode(false);
        setVoicePhase("idle");
      },
    }
  );

  // Listen again as soon as nothing else owns the conversation. Driven off
  // state rather than callbacks so a missed hand-off can't leave the loop
  // stalled; gates stay manual on purpose.
  useEffect(() => {
    if (!voiceMode || isRunning || pendingGate) return;
    if (voiceRecording || voiceTranscribing || voicePhase === "speaking") return;
    setVoicePhase("listening");
    void startVoiceCapture();
  }, [
    voiceMode,
    isRunning,
    pendingGate,
    voicePhase,
    voiceRecording,
    voiceTranscribing,
    startVoiceCapture,
  ]);

  useEffect(() => {
    if (voiceTranscribing) setVoicePhase("transcribing");
  }, [voiceTranscribing]);

  const speakReply = useCallback((text: string) => {
    const body = text.trim();
    // Voice mode always speaks — auto-speak is only for typed turns outside voice mode.
    const inVoice = voiceModeRef.current;
    const shouldSpeak =
      Boolean(body) &&
      voiceCapsRef.current.ttsAvailable &&
      (inVoice || autoSpeakRef.current);
    if (!shouldSpeak) {
      setVoicePhase("idle");
      return;
    }
    if (inVoice) setVoicePhase("speaking");
    // Stop any previous reply before starting a new one.
    voiceAudioRef.current?.pause();
    voiceAudioRef.current = null;
    speakText(body)
      .then(async (url) => {
        try {
          const audio = await startAudioPlayback(url);
          voiceAudioRef.current = audio;
          const done = () => {
            URL.revokeObjectURL(url);
            if (voiceAudioRef.current === audio) voiceAudioRef.current = null;
            setVoicePhase("idle");
          };
          audio.onended = done;
          audio.onerror = () => {
            done();
            if (voiceModeRef.current) setError("Audio playback failed.");
          };
        } catch (e) {
          URL.revokeObjectURL(url);
          if (voiceModeRef.current || autoSpeakRef.current) {
            setError((e as Error).message || "Could not play reply audio.");
          }
          setVoicePhase("idle");
        }
      })
      .catch((e) => {
        if (voiceModeRef.current) setError((e as Error).message);
        setVoicePhase("idle");
      });
  }, []);
  speakReplyRef.current = speakReply;

  const toggleVoiceMode = useCallback(() => {
    const next = !voiceModeRef.current;
    // handleStreamEvent may read this before the next render.
    voiceModeRef.current = next;
    setVoiceMode(next);
    if (next) {
      // Unlock autoplay on this click so the later reply can actually play,
      // and fold auto-speak into voice mode so a separate toggle isn't required.
      unlockAudioPlayback();
      enableAutoSpeak();
      autoSpeakRef.current = true;
      return;
    }
    cancelVoiceCapture();
    voiceAudioRef.current?.pause();
    voiceAudioRef.current = null;
    setVoicePhase("idle");
  }, [cancelVoiceCapture, enableAutoSpeak]);

  // Empty aui root — starter chips are rendered from React state via Thread.
  const aui = useAui({});

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background">
      <ChatSidebar
        chats={chats}
        currentChatId={currentChatId}
        onSelect={(id) => { selectChat(id); setMobileSidebarOpen(false); }}
        onNew={() => { void createChat(); }}
        onImportSession={(chatId) => { void handleImportedSession(chatId); }}
        onRename={renameChat}
        onDelete={deleteChat}
        onPin={pinChat}
        username={user?.username || ""}
        onLogout={logout}
        theme={theme}
        onToggleTheme={toggleTheme}
        voiceCaps={voiceCaps}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed((v) => !v)}
        mobileOpen={mobileSidebarOpen}
        onMobileClose={() => setMobileSidebarOpen(false)}
      />
      <div className="flex flex-1 flex-col h-full relative min-w-0">
        {/* Mobile top bar */}
        <div className="md:hidden flex items-center gap-2 border-b px-3 py-2 bg-card shrink-0">
          <Button
            variant="ghost"
            size="icon"
            className="size-8"
            onClick={() => setMobileSidebarOpen(true)}
          >
            <Menu className="size-4" />
          </Button>
          <div className="flex items-center gap-2 flex-1 min-w-0">
            <img src="/static/hermes-logo.png" alt="Hermes" className="size-6 rounded-md object-contain shrink-0" />
            <span className="font-semibold text-sm truncate">
              {chats.find((c) => c.chat_id === currentChatId)?.title ?? "Hermes Chat"}
            </span>
          </div>
          {currentChatId && (
            <Button
              variant="ghost"
              size="sm"
              className="shrink-0 text-xs truncate max-w-[140px]"
              onClick={() => setModelPickerOpen(true)}
              disabled={isRunning}
              title="Switch model"
            >
              {currentModelDisplay || "Model"}
            </Button>
          )}
        </div>

        {/* Desktop header */}
        <div className="hidden md:flex items-center justify-between border-b px-4 py-2 bg-card shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            <span className="font-semibold text-sm truncate">
              {chats.find((c) => c.chat_id === currentChatId)?.title ?? "Hermes Chat"}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {currentChatId && (
              <Button
                variant="outline"
                size="sm"
                className="text-xs truncate max-w-[240px]"
                onClick={() => setModelPickerOpen(true)}
                disabled={isRunning}
                title={[
                  currentModelDisplay || "Model",
                  sessionInfo.reasoning_effort && `reasoning: ${sessionInfo.reasoning_effort}`,
                  sessionInfo.service_tier && `tier: ${sessionInfo.service_tier}`,
                  sessionInfo.fast && "fast",
                  sessionInfo.yolo && "yolo",
                ].filter(Boolean).join(" · ")}
              >
                {currentModelDisplay || "Model"}
              </Button>
            )}
            {currentChatId && sessionInfo.reasoning_effort && (
              <span className="hidden md:inline-flex items-center rounded-full border border-border/50 bg-muted/60 px-2 py-0.5 text-[10px] text-muted-foreground uppercase tracking-wide">
                {sessionInfo.reasoning_effort}
              </span>
            )}
            {currentChatId && sessionInfo.service_tier && (
              <span className="hidden md:inline-flex items-center rounded-full border border-border/50 bg-muted/60 px-2 py-0.5 text-[10px] text-muted-foreground uppercase tracking-wide">
                {sessionInfo.service_tier}
              </span>
            )}
            {currentChatId && (sessionInfo.fast || sessionInfo.yolo) && (
              <span className="hidden md:inline-flex items-center gap-1 rounded-full border border-border/50 bg-muted/60 px-2 py-0.5 text-[10px] text-muted-foreground uppercase tracking-wide">
                {sessionInfo.fast && <span>fast</span>}
                {sessionInfo.yolo && <span>yolo</span>}
              </span>
            )}
          </div>
        </div>

        {(error || toasts.length > 0) && (
          <div className="absolute top-2 right-2 z-50 flex max-w-sm flex-col gap-2">
            {error && (
              <div className="rounded-md bg-destructive px-4 py-2 text-sm text-destructive-foreground shadow">
                {error}
              </div>
            )}
            {toasts.map((toast) => (
              <div
                key={toast.id}
                className={cn(
                  "rounded-md border px-4 py-2 text-sm shadow",
                  toast.level === "error" || toast.level === "danger"
                    ? "border-destructive/40 bg-destructive text-destructive-foreground"
                    : toast.level === "warning" || toast.level === "warn"
                      ? "border-amber-500/40 bg-amber-500/15 text-foreground"
                      : "border-border bg-card text-foreground",
                )}
              >
                {toast.text}
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-col flex-1 min-h-0 overflow-hidden">
          <AssistantRuntimeProvider aui={aui} runtime={runtime}>
            <Thread onUndo={handleUndo} onCompress={handleCompress} onBranch={handleBranch} contextWindow={contextWindow} threadUsage={threadUsage as any} autoSpeak={autoSpeak} onAutoSpeakToggle={toggleAutoSpeak} voiceCaps={voiceCaps} voiceMode={voiceMode} voicePhase={voicePhase} onVoiceModeToggle={toggleVoiceMode} gatePending={Boolean(pendingGate)} onGateChoice={handleGateChoice} starterSuggestions={starterSuggestions} slashCommands={slashCommands} composerDraft={composerDraft} onComposerDraftConsumed={() => setComposerDraft(null)} />
          </AssistantRuntimeProvider>
        </div>
        <ModelPicker
          chatId={currentChatId}
          open={modelPickerOpen}
          onOpenChange={setModelPickerOpen}
          freeModelsOnly={!!user?.free_models_only}
          onModelChange={(model, provider, isProfile) => {
            if (isProfile) {
              setProfileModels((prev) => new Set(prev).add(model));
              setCurrentModelDisplay(`hermes/profile/${profileNameFromModel(model)}`);
            } else {
              setCurrentModelDisplay(provider ? `${provider} / ${model}` : model);
            }
          }}
        />
      </div>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AuthGuard>
        <ChatApp />
      </AuthGuard>
    </AuthProvider>
  );
}
