import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Download, Shield } from "lucide-react";
import { ChatThread } from "@/components/chat/ChatThread";
import { CodePanel } from "@/components/code/CodePanel";
import { AutoTasksView } from "@/components/auto-tasks/AutoTasksView";
import { ReviewShell } from "@/components/layout/ReviewShell";
import type { SessionInfo } from "@/components/layout/SessionInfoBar";
import { NewReviewForm, type NewReviewSubmit } from "@/components/review/NewReviewForm";
import { ReportView } from "@/components/report/ReportView";
import { SettingsDialog, type ReviewSettings, type ThemeMode } from "@/components/settings/SettingsDialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";
import {
  sessionMessageToUIMessage,
  useReviewSession,
  type ChatMessage,
  type Finding,
  type ReviewTask,
} from "@/hooks/useReviewSession";
import { useReviewTasks } from "@/hooks/useReviewTasks";
import {
  clearSavedSecret,
  deriveWsUrl,
  fetchBootstrap,
  loadSavedSecret,
  saveSecret,
} from "@/lib/bootstrap";
import { fetchSessionMessages, fetchWebuiThread } from "@/lib/api";
import { NanobotClient } from "@/lib/nanobot-client";
import type {
  ChatSummary,
  ConnectionStatus,
  ReviewDepth,
  ReviewFocus,
  ReviewTargetType,
  UIMessage,
} from "@/lib/types";
import { ClientProvider, useClient } from "@/providers/ClientProvider";

type BootState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "auth"; failed?: boolean }
  | {
      status: "ready";
      client: NanobotClient;
      token: string;
      modelName: string | null;
      refreshAuth: () => Promise<string | null>;
    };

const THEME_STORAGE_KEY = "nanobot-review-webui.theme";

function loadSavedTheme(): ThemeMode {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === "light" || saved === "dark" || saved === "system") return saved;
  } catch { /* ignore */ }
  return "light";
}

function saveTheme(theme: ThemeMode): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch { /* ignore */ }
}

function applyThemeClass(theme: ThemeMode): void {
  const root = document.documentElement;
  if (theme === "system") {
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.classList.toggle("dark", prefersDark);
  } else {
    root.classList.toggle("dark", theme === "dark");
  }
}

const DEFAULT_SETTINGS: ReviewSettings = {
  defaultDepth: "full",
  defaultFocus: [],
  theme: "light",
};

function exportMarkdown(reportMarkdown: string, target: string) {
  const blob = new Blob([reportMarkdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `review-report-${target.replace(/[^a-zA-Z0-9_-]/g, "_")}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

const GITHUB_TARGET_RE = /^(?:https?:\/\/)?(?:www\.)?github\.com\/[^/\s]+\/[^/\s]+(?:[/?#].*)?$/i;

function inferTargetType(target: string): Exclude<ReviewTargetType, "auto"> {
  return GITHUB_TARGET_RE.test(target.trim()) ? "github" : "local";
}

function reviewTaskFromSubmit(submit: NewReviewSubmit): ReviewTask {
  return {
    target: submit.target,
    targetType: inferTargetType(submit.target),
    action: submit.action,
    depth: submit.depth,
    focus: submit.focus,
  };
}

function taskFromHistory(session: ChatSummary | null, messages: UIMessage[]): ReviewTask | null {
  const review = messages.find((message) => message.review)?.review;
  if (review?.target) {
    return {
      target: review.target,
      targetType: review.target_type,
      action: review.action,
      depth: review.mode,
      focus: review.focus,
    };
  }
  const target = session?.title || session?.preview;
  return target ? { target } : null;
}

function historyTaskFallback(session: ChatSummary | null): ReviewTask | null {
  if (!session) return null;
  return { target: session.title || session.preview || session.chatId };
}

function hasAssistantContent(messages: ChatMessage[] | undefined): boolean {
  return !!messages?.some((message) =>
    message.role === "agent"
    && (message.content.trim().length > 0 || !!message.thinking?.trim())
  );
}

function AuthForm({
  failed,
  onSecret,
}: {
  failed: boolean;
  onSecret: (secret: string) => void;
}) {
  const [value, setValue] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const secret = value.trim();
    if (!secret) return;
    setSubmitting(true);
    onSecret(secret);
  };

  return (
    <div className="flex h-full w-full items-center justify-center bg-background px-6">
      <form onSubmit={handleSubmit} className="flex w-full max-w-sm flex-col gap-5">
        <div className="flex flex-col items-center gap-2 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <Shield className="h-6 w-6" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-foreground">
            Review Agent
          </h1>
          <p className="text-sm text-muted-foreground">
            Enter your workspace secret to continue
          </p>
        </div>
        {failed && (
          <p className="text-center text-sm text-destructive">
            Invalid secret. Please try again.
          </p>
        )}
        <Input
          type="password"
          placeholder="Workspace secret..."
          value={value}
          onChange={(event) => setValue(event.target.value)}
          disabled={submitting}
          autoFocus
        />
        <Button type="submit" className="w-full" disabled={!value.trim() || submitting}>
          {submitting ? "Authenticating..." : "Continue"}
        </Button>
      </form>
    </div>
  );
}

function LoadingScreen() {
  return (
    <div className="flex h-full w-full items-center justify-center bg-background">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <span className="relative flex h-2 w-2">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-foreground/40" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-foreground/60" />
        </span>
        Connecting...
      </div>
    </div>
  );
}

function ErrorScreen({ message }: { message: string }) {
  return (
    <div className="flex h-full w-full items-center justify-center bg-background px-4 text-center">
      <div className="flex max-w-md flex-col items-center gap-3">
        <p className="text-lg font-semibold">Connection Error</p>
        <p className="text-sm text-muted-foreground">{message}</p>
      </div>
    </div>
  );
}

export default function App() {
  const [state, setState] = useState<BootState>({ status: "loading" });
  const authRefreshRef = useRef<Promise<string | null> | null>(null);

  // Apply saved theme on mount (before React renders, to avoid flash)
  useEffect(() => {
    applyThemeClass(loadSavedTheme());
  }, []);

  const bootstrapWithSecret = useCallback((secret: string) => {
    let cancelled = false;
    (async () => {
      setState({ status: "loading" });
      try {
        const boot = await fetchBootstrap("", secret);
        if (cancelled) return;
        if (secret) saveSecret(secret);

        const url = deriveWsUrl(boot.ws_path, boot.token);
        let client: NanobotClient;
        const refreshAuth = async () => {
          if (authRefreshRef.current) return authRefreshRef.current;
          authRefreshRef.current = (async () => {
            try {
              const refreshed = await fetchBootstrap("", secret);
              const refreshedUrl = deriveWsUrl(refreshed.ws_path, refreshed.token);
              client.updateUrl(refreshedUrl);
              setState((current) =>
                current.status === "ready" && current.client === client
                  ? {
                      ...current,
                      token: refreshed.token,
                      modelName: refreshed.model_name ?? current.modelName,
                    }
                  : current,
              );
              return refreshed.token;
            } catch {
              return null;
            } finally {
              authRefreshRef.current = null;
            }
          })();
          return authRefreshRef.current;
        };
        const reauth = async () => {
          const refreshedToken = await refreshAuth();
          return refreshedToken ? deriveWsUrl(boot.ws_path, refreshedToken) : null;
        };

        client = new NanobotClient({ url, onReauth: reauth });
        client.connect();
        setState({
          status: "ready",
          client,
          token: boot.token,
          modelName: boot.model_name ?? null,
          refreshAuth,
        });
      } catch (error) {
        if (cancelled) return;
        const message = (error as Error).message;
        if (message.includes("HTTP 401") || message.includes("HTTP 403")) {
          setState({ status: "auth", failed: true });
        } else {
          setState({ status: "error", message });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => bootstrapWithSecret(loadSavedSecret()), [bootstrapWithSecret]);

  if (state.status === "loading") return <LoadingScreen />;
  if (state.status === "auth") {
    return <AuthForm failed={!!state.failed} onSecret={(secret) => bootstrapWithSecret(secret)} />;
  }
  if (state.status === "error") return <ErrorScreen message={state.message} />;

  const handleModelNameChange = (modelName: string | null) => {
    setState((current) =>
      current.status === "ready" ? { ...current, modelName } : current,
    );
  };
  const handleLogout = () => {
    state.client.close();
    clearSavedSecret();
    setState({ status: "auth" });
  };

  return (
    <ClientProvider
      client={state.client}
      token={state.token}
      modelName={state.modelName}
      refreshAuth={state.refreshAuth}
    >
      <ReviewAppShell onModelNameChange={handleModelNameChange} onLogout={handleLogout} />
    </ClientProvider>
  );
}

function ReviewAppShell({
  onModelNameChange,
  onLogout,
}: {
  onModelNameChange: (modelName: string | null) => void;
  onLogout: () => void;
}) {
  const { client, modelName, token, refreshAuth } = useClient();
  const { tasks, loading, error: tasksError, refresh, createTask, deleteTask, updateTask } = useReviewTasks();
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [selectedFinding, setSelectedFinding] = useState<Finding | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<ReviewSettings>(DEFAULT_SETTINGS);

  // Apply theme whenever settings.theme changes
  useEffect(() => {
    applyThemeClass(settings.theme);
    saveTheme(settings.theme);
  }, [settings.theme]);

  // Listen for system theme changes when in "system" mode
  useEffect(() => {
    if (settings.theme !== "system") return;
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => applyThemeClass("system");
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [settings.theme]);

  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [sidebarView, setSidebarView] = useState<"reviews" | "auto">("reviews");
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("connecting");
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const historyCacheRef = useRef<Map<string, { messages: UIMessage[]; task: ReviewTask | null; chatMessages?: ChatMessage[] }>>(new Map());
  const historyRequestRef = useRef(0);
  const restoredActiveKeyRef = useRef(false);
  const activeKeyInitializedRef = useRef(false);

  // Persist activeKey to sessionStorage so it survives page refresh.
  // On the initial render, activeKey is null, but we must NOT clear
  // sessionStorage — otherwise the auto-restore effect below can never
  // read the saved value.
  useEffect(() => {
    const KEY = "nanobot-review-webui.active-key";
    if (activeKey) {
      sessionStorage.setItem(KEY, activeKey);
    } else if (activeKeyInitializedRef.current) {
      sessionStorage.removeItem(KEY);
    }
    activeKeyInitializedRef.current = true;
  }, [activeKey]);

  const activeSession = useMemo(
    () => tasks.find((task) => task.key === activeKey) ?? null,
    [activeKey, tasks],
  );
  const autoTaskSessions = useMemo(
    () => tasks
      .filter((task) => !!task.autoTaskRunId)
      .sort((a, b) => {
        if (!!b.pinned !== !!a.pinned) return b.pinned ? 1 : -1;
        return 0;
      }),
    [tasks],
  );
  const dailySessions = useMemo(
    () => tasks
      .filter((task) => !task.autoTaskRunId)
      .sort((a, b) => {
        if (!!b.pinned !== !!a.pinned) return b.pinned ? 1 : -1;
        return 0;
      }),
    [tasks],
  );
  const activeChatId = activeSession?.chatId ?? (activeKey?.startsWith("websocket:") ? activeKey.slice(10) : null);
  const { state, reset, loadHistory, startReview, sendFollowUp, cancelTurn } = useReviewSession(
    client,
    activeChatId,
  );
  const apiAuth = useMemo(() => ({ token, refreshAuth }), [refreshAuth, token]);

  useEffect(() => client.onStatus(setConnectionStatus), [client]);
  useEffect(
    () => client.onRuntimeModelUpdate((nextModelName) => onModelNameChange(nextModelName)),
    [client, onModelNameChange],
  );

  const handleNewTask = useCallback(() => {
    historyRequestRef.current += 1;
    setSidebarView("reviews");
    setActiveKey(null);
    setSelectedFinding(null);
    setSessionError(null);
    reset();
  }, [reset]);

  const handleSelectTask = useCallback(
    async (key: string) => {
      const requestId = historyRequestRef.current + 1;
      historyRequestRef.current = requestId;
      const session = tasks.find((task) => task.key === key) ?? null;
      setSidebarView("reviews");
      if (activeKey && state.messages.length > 0) {
        const existing = historyCacheRef.current.get(activeKey);
        historyCacheRef.current.set(activeKey, {
          messages: existing?.messages ?? [],
          task: existing?.task ?? state.task,
          chatMessages: state.messages,
        });
      }
      setActiveKey(key);
      setSelectedFinding(null);
      setSessionError(null);
      const cached = historyCacheRef.current.get(key);
      if (cached) {
        loadHistory(
          cached.messages,
          cached.task ?? taskFromHistory(session, cached.messages) ?? historyTaskFallback(session),
          undefined,
          cached.chatMessages,
        );
      } else {
        reset();
      }
      try {
        let messages = (await fetchWebuiThread({ token, refreshAuth }, key))?.messages ?? [];
        if (messages.length === 0) {
          const sessionData = await fetchSessionMessages({ token, refreshAuth }, key);
          messages = (sessionData?.messages ?? [])
            .map(sessionMessageToUIMessage)
            .filter((message): message is UIMessage => message !== null);
        }
        if (historyRequestRef.current !== requestId) return;
        const task = taskFromHistory(session, messages) ?? historyTaskFallback(session);
        const existing = historyCacheRef.current.get(key);
        const cachedChatMessages = hasAssistantContent(existing?.chatMessages)
          ? existing?.chatMessages
          : undefined;
        historyCacheRef.current.set(key, { messages, task, chatMessages: cachedChatMessages });
        loadHistory(messages, task, undefined, cachedChatMessages);
      } catch (error) {
        if (historyRequestRef.current !== requestId) return;
        const message = error instanceof Error ? error.message : "Failed to load review session";
        console.error("Failed to load review session", error);
        setSessionError(message);
        loadHistory([], historyTaskFallback(session), message);
      }
    },
    [activeKey, loadHistory, refreshAuth, reset, state.messages, state.task, tasks, token],
  );

  // Auto-restore the last active session after page refresh.
  useEffect(() => {
    if (restoredActiveKeyRef.current || loading || tasks.length === 0 || activeKey) return;
    const savedKey = sessionStorage.getItem("nanobot-review-webui.active-key");
    if (savedKey && tasks.some((t) => t.key === savedKey)) {
      restoredActiveKeyRef.current = true;
      handleSelectTask(savedKey);
    }
  }, [loading, tasks, activeKey, handleSelectTask]);

  const handleOpenAutoTasks = useCallback(() => {
    historyRequestRef.current += 1;
    setSidebarView("auto");
    setActiveKey(null);
    setSelectedFinding(null);
    setSessionError(null);
    reset();
  }, [reset]);

  const handleDeleteTask = useCallback(
    async (key: string) => {
      const deletingActive = key === activeKey;
      setDeletingKey(key);
      setSessionError(null);
      try {
        await deleteTask(key);
        historyCacheRef.current.delete(key);
        if (deletingActive) handleNewTask();
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to delete review session";
        console.error("Failed to delete review session", error);
        setSessionError(message);
      } finally {
        setDeletingKey(null);
      }
    },
    [activeKey, deleteTask, handleNewTask],
  );

  const handlePinTask = useCallback(
    async (key: string) => {
      const task = tasks.find((t) => t.key === key);
      if (!task) return;
      try {
        await updateTask(key, { pinned: !task.pinned });
      } catch (error) {
        console.error("Failed to pin review session", error);
      }
    },
    [tasks, updateTask],
  );

  const handleRenameTask = useCallback(
    async (key: string, customTitle: string) => {
      try {
        await updateTask(key, { custom_title: customTitle });
      } catch (error) {
        console.error("Failed to rename review session", error);
        throw error;
      }
    },
    [updateTask],
  );

  const handleSubmitReview = useCallback(
    async (submit: NewReviewSubmit) => {
      const task = reviewTaskFromSubmit(submit);
      const chatId = activeChatId ?? (await createTask());
      const key = `websocket:${chatId}`;
      historyRequestRef.current += 1;
      historyCacheRef.current.delete(key);
      setActiveKey(key);
      setSelectedFinding(null);
      setSessionError(null);
      startReview(task);
      client.sendMessage(chatId, "", undefined, {
        review: {
          mode: task.depth ?? "full",
          target: task.target,
          target_type: task.targetType,
          action: task.action ?? "repo",
          focus: task.focus,
        },
      });
      void refresh();
    },
    [activeChatId, client, createTask, refresh, startReview],
  );

  const handleSendFollowUp = useCallback((text: string) => {
    sendFollowUp(text);
  }, [sendFollowUp]);

  const handleExport = useCallback(() => {
    if (state.reportMarkdown) {
      exportMarkdown(state.reportMarkdown, state.task?.target || "review");
    }
  }, [state.reportMarkdown, state.task?.target]);

  useKeyboardShortcuts({
    onNewTask: handleNewTask,
    onOpenSettings: () => setSettingsOpen(true),
    onToggleSidebar: () => setSidebarOpen((value) => !value),
    onToggleRightPanel: () => undefined,
    onEscape: () => {
      setSettingsOpen(false);
      setSelectedFinding(null);
    },
  });

  const sessionInfo: SessionInfo | null = state.task
    ? {
        target: state.task.target,
        depth: state.task.depth,
        dimensions: state.dimensions
          .filter((dimension) => dimension.status !== "skipped")
          .map((dimension) => dimension.dimension),
        status:
          state.phase === "completed"
            ? "completed"
            : state.phase === "error"
              ? "failed"
              : state.phase === "stopped"
                ? "stopped"
                : "running",
        findingCounts: {
          total: state.findings.length,
          critical: state.findings.filter((finding) => finding.severity.toLowerCase() === "critical").length,
          high: state.findings.filter((finding) => finding.severity.toLowerCase() === "high").length,
          medium: state.findings.filter((finding) => finding.severity.toLowerCase() === "medium").length,
          low: state.findings.filter((finding) => finding.severity.toLowerCase() === "low").length,
        },
      }
    : null;

  const showReviewForm = !activeKey;
  const followUpDisabled =
    state.phase !== "completed"
    && state.phase !== "error"
    && state.phase !== "history"
    && state.phase !== "stopped";
  const isSessionBusy = deletingKey !== null || state.phase === "submitting";

  return (
    <>
      <ReviewShell
        connectionStatus={connectionStatus}
        modelName={modelName}
        onOpenSettings={() => setSettingsOpen(true)}
        onLogout={onLogout}
        autoTaskSessions={autoTaskSessions}
        dailySessions={dailySessions}
        activeKey={activeKey}
        sidebarLoading={loading}
        sidebarError={tasksError}
        onTaskSelect={handleSelectTask}
        onNewTask={handleNewTask}
        onTaskDelete={handleDeleteTask}
        onTaskPin={handlePinTask}
        onTaskRename={handleRenameTask}
        onOpenAutoTasks={handleOpenAutoTasks}
        mainContent={
          sidebarView === "auto" ? (
            <AutoTasksView onSessionsChanged={refresh} />
          ) : showReviewForm ? (
            <div className="flex h-full min-h-0 items-center justify-center overflow-hidden p-3">
              <NewReviewForm
                defaultDepth={settings.defaultDepth}
                defaultFocus={settings.defaultFocus}
                onSubmit={handleSubmitReview}
                submitting={state.phase === "submitting"}
              />
            </div>
          ) : (
            <ChatThread
              messages={state.messages}
              phase={state.phase}
              onSend={handleSendFollowUp}
              disabled={isSessionBusy || followUpDisabled}
              emptyTitle={
                sessionError ? "Failed to load this session" : "No saved messages for this session"
              }
              emptyDescription={
                sessionError
                  ? sessionError
                  : "The session exists, but no replayable review transcript was found."
              }
              onSelectFinding={setSelectedFinding}
              onPause={() => cancelTurn()}
              findings={state.findings}
              subagentCards={state.subagentCards}
            />
          )
        }
        rightPanelContent={
          showReviewForm ? undefined : (
            <div className="space-y-4 p-4 pt-2">
              <CodePanel
                finding={selectedFinding}
                sessionKey={activeKey}
                auth={apiAuth}
                className={state.reportMarkdown ? "min-h-[280px]" : undefined}
              />
              {state.reportMarkdown && (
                <div>
                  <Button
                    variant="outline"
                    size="sm"
                    className="w-full gap-1.5 text-xs"
                    onClick={handleExport}
                  >
                    <Download className="h-3.5 w-3.5" />
                    Export Report
                  </Button>
                </div>
              )}
              {state.reportMarkdown && (
                <ReportView
                  summary={state.reportSummary}
                  findings={state.findings}
                  needsConfirmationCount={state.needsConfirmationCount}
                  rejectedCount={state.rejectedCount}
                />
              )}
            </div>
          )
        }
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen((value) => !value)}
        sessionInfo={sessionInfo}
      />
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        settings={settings}
        onSettingsChange={setSettings}
      />
    </>
  );
}
