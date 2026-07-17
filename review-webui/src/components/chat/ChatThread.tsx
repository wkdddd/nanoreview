import { useRef, useEffect, useCallback, useMemo, useState } from "react";
import { MessageSquare, ChevronDown, ChevronRight, AlertTriangle, AlertCircle, AlertOctagon, Info, Loader2 } from "lucide-react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ChatMessage } from "./ChatMessage";
import { ChatInput } from "./ChatInput";
import { SubagentCards } from "./SubagentCards";
import { SeverityBadge } from "@/components/findings/SeverityBadge";
import { cn } from "@/lib/utils";
import type { Finding } from "@/hooks/useReviewSession";
import type { SubagentCard } from "@/hooks/useReviewSession";

export interface ChatMessageItem {
  id: string;
  role: "user" | "agent";
  type: "text" | "finding" | "report";
  content: string;
  timestamp: number;
  finding?: Finding;
  thinking?: string;
  streaming?: boolean;
}

interface ChatThreadProps {
  messages: ChatMessageItem[];
  phase?: string;
  onSend: (text: string) => void;
  disabled?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  onSelectFinding?: (finding: Finding) => void;
  onPause?: () => void;
  /** Parsed findings from the review report, forwarded to ChatMessage
   * so table-row / inline-code clicks carry the correct severity. */
  findings?: Finding[];
  /** Per-subagent status cards rendered at the bottom of the thread. */
  subagentCards?: SubagentCard[];
}

/** Group consecutive finding messages into batches */
function groupMessages(messages: ChatMessageItem[]) {
  const groups: Array<{ type: "single" | "finding-batch"; items: ChatMessageItem[] }> = [];
  let currentBatch: ChatMessageItem[] = [];

  for (const msg of messages) {
    if (msg.type === "finding") {
      currentBatch.push(msg);
    } else {
      if (currentBatch.length > 0) {
        groups.push({ type: "finding-batch", items: currentBatch });
        currentBatch = [];
      }
      groups.push({ type: "single", items: [msg] });
    }
  }
  if (currentBatch.length > 0) {
    groups.push({ type: "finding-batch", items: currentBatch });
  }
  return groups;
}

const SEVERITY_ORDER: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

const SEVERITY_GROUP_STYLES: Record<string, { border: string; bg: string; icon: typeof AlertOctagon; iconColor: string }> = {
  critical: { border: "border-red-200", bg: "bg-red-50/50 dark:bg-red-950/20", icon: AlertOctagon, iconColor: "text-red-500" },
  high: { border: "border-orange-200", bg: "bg-orange-50/50 dark:bg-orange-950/20", icon: AlertTriangle, iconColor: "text-orange-500" },
  medium: { border: "border-yellow-200", bg: "bg-yellow-50/50 dark:bg-yellow-950/20", icon: AlertCircle, iconColor: "text-yellow-600" },
  low: { border: "border-blue-200", bg: "bg-blue-50/50 dark:bg-blue-950/20", icon: Info, iconColor: "text-blue-500" },
};

/** Sub-group findings within a batch by severity */
function groupBySeverity(items: ChatMessageItem[]) {
  const groups: Record<string, ChatMessageItem[]> = {};
  for (const item of items) {
    const sev = item.finding?.severity?.toLowerCase() ?? "medium";
    if (!groups[sev]) groups[sev] = [];
    groups[sev].push(item);
  }
  return Object.entries(groups).sort(
    ([a], [b]) => (SEVERITY_ORDER[a] ?? 99) - (SEVERITY_ORDER[b] ?? 99),
  );
}

const BATCH_COLLAPSE_THRESHOLD = 3;

/** 流式加载中的 finding 骨架行 */
function FindingSkeletonRow() {
  return (
    <div className="flex items-start gap-2.5 w-full px-3.5 py-2.5">
      <div className="mt-1.5 w-1.5 h-1.5 rounded-full bg-muted-foreground/30 shrink-0" />
      <div className="flex-1 min-w-0 space-y-2">
        {/* 标题骨架 */}
        <div className="h-4 bg-muted/60 rounded-md w-3/4 animate-pulse" />
        {/* 元信息骨架 */}
        <div className="flex items-center gap-2">
          <div className="h-4 bg-muted/40 rounded w-24 animate-pulse" />
          <div className="h-4 bg-muted/40 rounded w-16 animate-pulse" />
        </div>
      </div>
    </div>
  );
}

export function ChatThread({
  messages,
  phase,
  onSend,
  disabled = false,
  emptyTitle = "Start a review to see messages here",
  emptyDescription = "Your conversation with the review agent will appear in this thread.",
  onSelectFinding,
  onPause,
  findings,
  subagentCards,
}: ChatThreadProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const shouldScrollRef = useRef(true);
  const [collapsedBatches, setCollapsedBatches] = useState<Set<number>>(new Set());
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());

  const isStreaming = useMemo(
    () =>
      messages.some((msg) => msg.streaming)
      || phase === "submitting"
      || phase === "prefetching"
      || phase === "reviewing"
      || phase === "finalizing",
    [messages, phase],
  );

  const scrollToBottom = useCallback(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, []);

  useEffect(() => {
    if (shouldScrollRef.current) {
      scrollToBottom();
    }
  }, [messages, scrollToBottom]);

  const handleScroll = useCallback(() => {
    const viewport = scrollRef.current?.querySelector(
      "[data-radix-scroll-area-viewport]"
    );
    if (!viewport) return;
    const { scrollTop, scrollHeight, clientHeight } = viewport;
    const isNearBottom = scrollHeight - scrollTop - clientHeight < 50;
    shouldScrollRef.current = isNearBottom;
  }, []);

  const messageGroups = useMemo(() => groupMessages(messages), [messages]);
  const firstReportId = useMemo(
    () => messages.find((message) => message.type === "report")?.id,
    [messages],
  );

  const toggleBatch = useCallback((index: number) => {
    setCollapsedBatches((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }, []);

  const toggleSeverityGroup = useCallback((key: string) => {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const isEmpty = messages.length === 0;

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 min-h-0">
        <ScrollArea
          ref={scrollRef}
          className="h-full"
          onScrollCapture={handleScroll}
        >
          <div className="px-3 py-4 space-y-3">
            {isEmpty ? (
              <div className="flex flex-col items-center justify-center h-full py-16 text-muted-foreground">
                <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted mb-3">
                  <MessageSquare className="h-4 w-4" />
                </div>
                <p className="text-xs font-medium">{emptyTitle}</p>
                <p className="text-[11px] mt-0.5 text-muted-foreground/70">{emptyDescription}</p>
              </div>
            ) : (
              messageGroups.map((group, groupIdx) => {
                if (group.type === "finding-batch" && group.items.length >= BATCH_COLLAPSE_THRESHOLD) {
                  const isCollapsed = collapsedBatches.has(groupIdx);
                  const severityGroups = groupBySeverity(group.items);
                  // 检查 batch 中是否有正在流式加载的 finding
                  const hasStreaming = group.items.some((item) => item.streaming);

                  return (
                    <div key={`batch-${groupIdx}`} className="space-y-2">
                      {/* Batch header */}
                      <button
                        type="button"
                        onClick={() => toggleBatch(groupIdx)}
                        className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors px-1"
                        aria-expanded={!isCollapsed}
                      >
                        {isCollapsed ? (
                          <ChevronRight className="w-3 h-3" />
                        ) : (
                          <ChevronDown className="w-3 h-3" />
                        )}
                        {hasStreaming ? (
                          <>
                            <Loader2 className="w-3.5 h-3.5 animate-spin" />
                            <span className="font-medium">
                              正在分析 findings...
                            </span>
                          </>
                        ) : (
                          <>
                            <span className="font-medium">
                              {group.items.length} findings
                            </span>
                            {!isCollapsed && (
                              <span className="text-muted-foreground/60">
                                — grouped by severity
                              </span>
                            )}
                          </>
                        )}
                      </button>

                      {/* Severity-grouped cards */}
                      {!isCollapsed && (
                        <div className="space-y-2">
                          {severityGroups.map(([severity, items]) => {
                            const styles = SEVERITY_GROUP_STYLES[severity] ?? SEVERITY_GROUP_STYLES.medium;
                            const Icon = styles.icon;
                            const groupKey = `${groupIdx}-${severity}`;
                            const isGroupCollapsed = collapsedGroups.has(groupKey);
                            // 检查该 severity 组中是否有正在流式加载的 finding
                            const hasGroupStreaming = items.some((item) => item.streaming);

                            return (
                              <div
                                key={groupKey}
                                className={cn(
                                  "rounded-lg border overflow-hidden transition-all",
                                  styles.border,
                                  styles.bg,
                                  hasGroupStreaming && "border-dashed",
                                )}
                              >
                                {/* Severity card header */}
                                <button
                                  type="button"
                                  onClick={() => toggleSeverityGroup(groupKey)}
                                  className="flex items-center gap-2 w-full px-3 py-2 text-left hover:bg-black/[0.03] dark:hover:bg-white/[0.03] transition-colors"
                                  aria-expanded={!isGroupCollapsed}
                                >
                                  <Icon className={cn("w-3.5 h-3.5 shrink-0 opacity-70", styles.iconColor)} />
                                  <SeverityBadge severity={severity} />
                                  <span className="text-[11px] font-medium text-muted-foreground">
                                    {items.length} {items.length === 1 ? "issue" : "issues"}
                                  </span>
                                  {hasGroupStreaming && (
                                    <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
                                  )}
                                  <div className="ml-auto">
                                    {isGroupCollapsed ? (
                                      <ChevronRight className="w-3 h-3 text-muted-foreground" />
                                    ) : (
                                      <ChevronDown className="w-3 h-3 text-muted-foreground" />
                                    )}
                                  </div>
                                </button>

                                {/* Finding items inside the severity card */}
                                {!isGroupCollapsed && (
                                  <div className="border-t border-inherit/60">
                                    {items.map((message, idx) => {
                                      // 流式加载中的 finding 显示骨架行
                                      if (message.streaming) {
                                        return (
                                          <div
                                            key={message.id}
                                            className={cn(
                                              idx < items.length - 1 && "border-b border-inherit/40",
                                            )}
                                          >
                                            <FindingSkeletonRow />
                                          </div>
                                        );
                                      }

                                      const finding = message.finding!;
                                      return (
                                        <button
                                          key={message.id}
                                          type="button"
                                          onClick={() => onSelectFinding?.(finding)}
                                          className={cn(
                                            "flex items-start gap-2 w-full text-left px-3 py-2 transition-colors",
                                            "hover:bg-black/[0.03] dark:hover:bg-white/[0.03]",
                                            idx < items.length - 1 && "border-b border-inherit/40",
                                          )}
                                        >
                                          {/* Severity dot */}
                                          <div className="mt-1 w-1 h-1 rounded-full bg-current opacity-50 shrink-0" />
                                          <div className="flex-1 min-w-0">
                                            {/* Title */}
                                            <p className="text-xs font-medium text-foreground leading-snug">
                                              {finding.title}
                                            </p>
                                            {/* Meta line */}
                                            <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                                              <code className="text-[10px] font-mono text-muted-foreground bg-muted/60 px-1 py-0 rounded">
                                                {finding.file}{finding.line != null ? `:${finding.line}` : ""}
                                              </code>
                                              <span className="text-[9px] text-muted-foreground capitalize">
                                                {finding.dimension}
                                              </span>
                                            </div>
                                          </div>
                                        </button>
                                      );
                                    })}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                }

                return group.items.map((message) => (
                  <ChatMessage
                    key={message.id}
                    message={message}
                    onSelectFinding={onSelectFinding}
                    findings={findings}
                    afterThinking={
                      message.id === firstReportId && subagentCards && subagentCards.length > 0
                        ? <SubagentCards cards={subagentCards} />
                        : undefined
                    }
                  />
                ));
              })
            )}
            {!messages.some((message) => message.type === "report") && subagentCards && subagentCards.length > 0 && (
              <SubagentCards cards={subagentCards} />
            )}
            <div ref={bottomRef} />
          </div>
        </ScrollArea>
      </div>

      <ChatInput
        onSend={onSend}
        disabled={disabled}
        placeholder={disabled ? "Please wait..." : "Type a message..."}
        isStreaming={isStreaming}
        onPause={onPause}
      />
    </div>
  );
}
