import { useState, useRef, useEffect } from "react";
import { Plus, MessageSquare, Trash2, Loader2, Pin, Pencil, Check, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { ChatSummary, ReviewFocus } from "@/lib/types";

export interface ReviewSidebarProps {
  dailySessions: ChatSummary[];
  activeKey: string | null;
  loading: boolean;
  error?: string | null;
  deletingKey?: string | null;
  onSelect: (key: string) => void;
  onNewTask: () => void;
  onDelete: (key: string) => void;
  onPin: (key: string) => void;
  onRename: (key: string, customTitle: string) => Promise<void>;
}

function formatRelativeTime(dateStr: string | null): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHr = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHr / 24);

  if (diffSec < 60) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffHr < 24) return `${diffHr}h ago`;
  if (diffDay < 7) return `${diffDay}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function shortReviewTarget(target: string | undefined): string {
  const trimmed = target?.trim();
  if (!trimmed) return "";
  try {
    const url = new URL(trimmed);
    if (url.hostname.toLowerCase().endsWith("github.com")) {
      const parts = url.pathname.split("/").filter(Boolean);
      if (parts.length >= 2) return `${parts[0]}/${parts[1]}`;
    }
    const tail = url.pathname.split(/[\\/]/).filter(Boolean).pop();
    return tail || url.hostname || trimmed;
  } catch {
    const normalized = trimmed.replace(/[\\/]+$/, "");
    return normalized.split(/[\\/]/).filter(Boolean).pop() || normalized;
  }
}

function reviewListField(metadata: Record<string, unknown> | undefined, key: string): string[] {
  const value = metadata?.[key];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

function reviewFocusLabel(focus: ReviewFocus[] | string[]): string {
  return focus.length > 0 ? focus.join(", ") : "";
}

function getDisplayTitle(task: ChatSummary): string {
  if (task.customTitle?.trim()) return task.customTitle.trim();
  const isReview = !!task.reviewTarget;
  if (isReview) return shortReviewTarget(task.reviewTarget) || "Review";
  return task.title || "Untitled Review";
}

function TaskItem({
  task,
  isActive,
  isDeleting,
  onSelect,
  onDelete,
  onPin,
  onRename,
}: {
  task: ChatSummary;
  isActive: boolean;
  isDeleting?: boolean;
  onSelect: () => void;
  onDelete: () => void;
  onPin: () => void;
  onRename: (customTitle: string) => Promise<void>;
}) {
  const isReview = !!task.reviewTarget;
  const reviewFocus = reviewListField(task.metadata, "review_focus");
  const reviewSource = [
    task.reviewAction,
    task.reviewMode,
    task.reviewTargetType,
    reviewFocusLabel(reviewFocus),
  ].filter(Boolean).join(" · ");
  const displayTitle = getDisplayTitle(task);
  const source = isReview ? reviewSource : "";
  const preview = isReview ? (task.reviewTarget || "") : (task.preview || "");

  const [menuOpen, setMenuOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(displayTitle);
  const [renameSubmitting, setRenameSubmitting] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const renameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [menuOpen]);

  useEffect(() => {
    if (renaming) {
      renameInputRef.current?.focus();
      renameInputRef.current?.select();
    }
  }, [renaming]);

  const submitRename = async () => {
    const trimmed = renameValue.trim();
    if (!trimmed || trimmed === displayTitle) {
      setRenaming(false);
      return;
    }
    setRenameSubmitting(true);
    try {
      await onRename(trimmed);
      setRenaming(false);
    } catch {
      // Error handled by parent
    } finally {
      setRenameSubmitting(false);
    }
  };

  return (
    <div
      className={cn(
        "group relative flex w-full min-w-0 items-start gap-1.5 rounded-md px-2 py-1.5 text-left transition-colors",
        "hover:bg-secondary/60",
        isActive && "bg-secondary",
      )}
    >
      {/* Active indicator bar */}
      {isActive && (
        <span className="absolute left-0 top-1/2 h-4 w-[2px] -translate-y-1/2 rounded-r-full bg-primary" />
      )}

      {/* Pinned indicator */}
      {task.pinned && (
        <Pin className="absolute right-1 top-1 h-2.5 w-2.5 text-primary/50" fill="currentColor" />
      )}

      {renaming ? (
        <div className="flex min-w-0 flex-1 items-center gap-1">
          <input
            ref={renameInputRef}
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); submitRename(); }
              if (e.key === "Escape") { setRenaming(false); setRenameValue(displayTitle); }
            }}
            disabled={renameSubmitting}
            className="min-w-0 flex-1 rounded border border-primary/30 bg-background px-1 py-0.5 text-xs font-medium text-foreground outline-none focus:border-primary"
            maxLength={200}
          />
          <button
            type="button"
            onClick={submitRename}
            disabled={renameSubmitting}
            className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-emerald-600 hover:bg-emerald-500/10"
            aria-label="Confirm rename"
          >
            {renameSubmitting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
          </button>
          <button
            type="button"
            onClick={() => { setRenaming(false); setRenameValue(displayTitle); }}
            disabled={renameSubmitting}
            className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-secondary"
            aria-label="Cancel rename"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      ) : (
        <>
          <button
            type="button"
            onClick={onSelect}
            title={displayTitle}
            className="min-w-0 flex-1 overflow-hidden text-left"
          >
            <span
              className={cn(
                "block w-full truncate text-xs font-medium leading-snug",
                isActive ? "text-foreground" : "text-foreground/80",
              )}
            >
              {displayTitle}
            </span>

            {source ? (
              <span className="block w-full truncate text-[11px] font-medium leading-relaxed text-muted-foreground">
                {source}
              </span>
            ) : null}

            <span className="block w-full truncate text-[11px] leading-relaxed text-muted-foreground">
              {preview}
            </span>

            {task.updatedAt ? (
              <span className="block text-[10px] text-muted-foreground/60">
                {formatRelativeTime(task.updatedAt)}
              </span>
            ) : null}
          </button>

          {/* Action menu trigger */}
          <div className="relative shrink-0" ref={menuRef}>
            <button
              type="button"
              disabled={isDeleting}
              onClick={(e) => {
                e.stopPropagation();
                if (isDeleting) return;
                setMenuOpen((v) => !v);
              }}
              className={cn(
                "inline-flex h-6 w-6 items-center justify-center rounded-md",
                "text-muted-foreground/80 transition-colors",
                "hover:bg-secondary hover:text-foreground",
                "disabled:cursor-not-allowed disabled:opacity-50",
                menuOpen && "bg-secondary text-foreground",
              )}
              aria-label="Task actions"
              title="Task actions"
            >
              {isDeleting ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="currentColor">
                  <circle cx="8" cy="3" r="1.5" />
                  <circle cx="8" cy="8" r="1.5" />
                  <circle cx="8" cy="13" r="1.5" />
                </svg>
              )}
            </button>

            {/* Dropdown menu */}
            {menuOpen && !isDeleting && (
              <div className="absolute right-0 top-7 z-50 w-32 rounded-md border bg-popover py-1 shadow-md">
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuOpen(false);
                    onPin();
                  }}
                  className="flex w-full items-center gap-2 px-2.5 py-1.5 text-xs text-foreground hover:bg-secondary"
                >
                  <Pin className="h-3 w-3" />
                  {task.pinned ? "取消置顶" : "置顶"}
                </button>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuOpen(false);
                    setRenameValue(displayTitle);
                    setRenaming(true);
                  }}
                  className="flex w-full items-center gap-2 px-2.5 py-1.5 text-xs text-foreground hover:bg-secondary"
                >
                  <Pencil className="h-3 w-3" />
                  重命名
                </button>
                <div className="my-0.5 h-px bg-border" />
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setMenuOpen(false);
                    onDelete();
                  }}
                  className="flex w-full items-center gap-2 px-2.5 py-1.5 text-xs text-destructive hover:bg-destructive/10"
                >
                  <Trash2 className="h-3 w-3" />
                  删除
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function SessionSection({
  title,
  tasks,
  activeKey,
  loading,
  emptyTitle,
  emptyDescription,
  deletingKey,
  onSelect,
  onDelete,
  onPin,
  onRename,
}: {
  title: string;
  tasks: ChatSummary[];
  activeKey: string | null;
  loading?: boolean;
  emptyTitle: string;
  emptyDescription: string;
  deletingKey?: string | null;
  onSelect: (key: string) => void;
  onDelete: (key: string) => void;
  onPin: (key: string) => void;
  onRename: (key: string, customTitle: string) => Promise<void>;
}) {
  return (
    <section className="mb-3">
      <div className="mb-1 flex items-center justify-between px-1.5">
        <h3 className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          {title}
        </h3>
      </div>
      {loading ? (
        <div className="flex items-center gap-1.5 px-2 py-2 text-[11px] text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          Loading...
        </div>
      ) : tasks.length === 0 ? (
        <div className="px-2 py-2 text-[11px] text-muted-foreground/60">
          <p className="font-medium">{emptyTitle}</p>
          <p className="mt-0.5 leading-relaxed">{emptyDescription}</p>
        </div>
      ) : (
        <div className="flex flex-col gap-0">
          {tasks.map((task) => (
            <TaskItem
              key={task.key}
              task={task}
              isActive={task.key === activeKey}
              isDeleting={task.key === deletingKey}
              onSelect={() => onSelect(task.key)}
              onDelete={() => onDelete(task.key)}
              onPin={() => onPin(task.key)}
              onRename={(customTitle) => onRename(task.key, customTitle)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

export function ReviewSidebar({
  dailySessions,
  activeKey,
  loading,
  error,
  onSelect,
  onNewTask,
  onDelete,
  onPin,
  onRename,
}: ReviewSidebarProps) {
  return (
    <aside className="flex h-full w-[220px] shrink-0 flex-col border-r bg-[hsl(var(--sidebar))]">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2">
        <h2 className="text-xs font-semibold tracking-tight text-[hsl(var(--sidebar-foreground))]">
          Review Tasks
        </h2>
        <div className="flex items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon"
            className="h-6 w-6"
            onClick={onNewTask}
            aria-label="New review task"
            title="New review task"
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {/* Divider */}
      <div className="mx-2 h-px bg-[hsl(var(--sidebar-border))]" />

      {/* Task list */}
      <ScrollArea className="flex-1 px-1.5 py-1">
        <div className="min-w-0">
          {error ? (
            <div className="px-3 py-4 text-xs text-destructive">
              {error}
            </div>
          ) : (
            <>
              <SessionSection
                title="日常任务"
                tasks={dailySessions}
                activeKey={activeKey}
                loading={loading}
                emptyTitle="No daily sessions"
                emptyDescription="Click + to start a manual review."
                onSelect={onSelect}
                onDelete={onDelete}
                onPin={onPin}
                onRename={onRename}
              />
              {!loading && dailySessions.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-10 text-muted-foreground/50">
                  <MessageSquare className="h-7 w-7 stroke-[1.5]" />
                </div>
              ) : null}
            </>
          )}
        </div>
      </ScrollArea>
    </aside>
  );
}
