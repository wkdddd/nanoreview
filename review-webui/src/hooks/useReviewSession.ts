import { useState, useCallback, useRef, useEffect } from "react";
import type { InboundEvent } from "@/lib/types";
import type { NanobotClient } from "@/lib/nanobot-client";
import type {
  OutboundReviewContext,
  ReviewAction,
  ReviewDepth,
  ReviewFocus,
  ReviewTargetType,
  ToolProgressEvent,
  UIMessage,
} from "@/lib/types";

export type ReviewPhase =
  | "idle"
  | "configuring"
  | "submitting"
  | "planning"
  | "prefetching"
  | "reviewing"
  | "validating"
  | "finalizing"
  | "history"
  | "completed"
  | "stopped"
  | "error";

export interface ReviewTask {
  target: string;
  targetType?: ReviewTargetType;
  action?: ReviewAction;
  depth?: ReviewDepth;
  focus?: ReviewFocus[];
}

export interface DimensionResult {
  dimension: string;
  status: string;
  acceptedCount: number;
  rejectedCount: number;
  uncertainCount: number;
}

export interface Finding {
  severity: string;
  dimension: string;
  file: string;
  line: number | null;
  title: string;
  impact: string;
  recommendation: string;
  confidence?: string;
  evidence?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "agent";
  type: "text" | "finding" | "report";
  content: string;
  timestamp: number;
  finding?: Finding;
  thinking?: string;
  streaming?: boolean;
}

export interface SubagentCard {
  id: string;
  label: string;
  status: "running" | "completed" | "error";
  thinking: string;
  thinkingStreaming: boolean;
  startedAt: number;
}

export interface ReviewSessionState {
  phase: ReviewPhase;
  task: ReviewTask | null;
  dimensions: DimensionResult[];
  findings: Finding[];
  reportSummary: string;
  recommendations: string[];
  needsConfirmationCount: number;
  rejectedCount: number;
  reportMarkdown: string;
  logs: string[];
  error: string | null;
  messages: ChatMessage[];
  subagentCards: SubagentCard[];
}

const INITIAL_STATE: ReviewSessionState = {
  phase: "idle",
  task: null,
  dimensions: [],
  findings: [],
  reportSummary: "",
  recommendations: [],
  needsConfirmationCount: 0,
  rejectedCount: 0,
  reportMarkdown: "",
  logs: [],
  error: null,
  messages: [],
  subagentCards: [],
};

function labelValue(value: string | undefined, fallback = "auto"): string {
  const trimmed = value?.trim();
  return trimmed || fallback;
}

function formatReviewFocus(focus: ReviewFocus[] | undefined): string {
  return focus && focus.length > 0 ? focus.join(", ") : "all";
}

function isReviewTask(review: UIMessage["review"] | ReviewTask): review is ReviewTask {
  if (!review) return false;
  return "targetType" in review || "depth" in review;
}

function formatReviewRequestContent(
  content: string,
  review: UIMessage["review"] | ReviewTask | undefined,
): string {
  if (!review) return content;
  const task = isReviewTask(review);
  const target = review.target;
  const targetType = task ? review.targetType : review.target_type;
  const mode = task ? review.depth : review.mode;
  const action = review.action;
  const focus = review.focus;
  const title = content.trim() || "Review";
  const lines = [
    title,
    `Target: ${labelValue(target, "(not set)")}`,
    `Type: ${labelValue(targetType)}`,
    `Mode: ${labelValue(mode, "full")}`,
    `Action: ${labelValue(action, "repo")}`,
    `Focus: ${formatReviewFocus(focus)}`,
  ];
  return lines.join("\n");
}

function isLikelyReviewReport(content: string): boolean {
  const text = content.trim();
  if (!text) return false;
  return (
    /^#{1,3}\s+.*(?:review|审查|报告)/im.test(text) ||
    /(?:^|\n)\s*\|\s*(?:severity|严重级别|file|文件|finding|问题)\s*\|/i.test(text) ||
    /(?:^|\n)\s*(?:findings|审查发现|recommendations|建议)\s*[:：]?/i.test(text)
  );
}

interface ParsedReviewReport {
  summary: string;
  dimensions: DimensionResult[];
  findings: Finding[];
  recommendations: string[];
  needsConfirmationCount: number;
  rejectedCount: number;
}

function normalizeHeading(value: string): string {
  return value
    .trim()
    .replace(/[*_`]/g, "")
    .replace(/[:：]\s*$/, "")
    .toLowerCase();
}

function splitMarkdownSections(markdown: string): Array<{ heading: string; body: string }> {
  const sections: Array<{ heading: string; body: string }> = [];
  const headingRe = /^(#{1,6})\s+(.+?)\s*$/gm;
  const matches = [...markdown.matchAll(headingRe)];
  for (let i = 0; i < matches.length; i += 1) {
    const match = matches[i];
    const next = matches[i + 1];
    const heading = normalizeHeading(match[2] ?? "");
    const start = (match.index ?? 0) + match[0].length;
    const end = next?.index ?? markdown.length;
    const body = markdown.slice(start, end).trim();
    if (heading) sections.push({ heading, body });
  }
  return sections;
}

function sectionBody(sections: Array<{ heading: string; body: string }>, names: string[]): string {
  return sections.find((section) =>
    names.some((name) => section.heading.includes(name))
  )?.body.trim() ?? "";
}

function cleanListItem(line: string): string {
  return line
    .trim()
    .replace(/^[-*+]\s+/, "")
    .replace(/^\d+[.)]\s+/, "")
    .replace(/^\[[ xX-]\]\s+/, "")
    .replace(/\\([\\`*_{}\[\]()#+\-.!|])/g, "$1")
    .trim();
}

function parseRecommendations(body: string): string[] {
  if (!body.trim()) return [];
  return body
    .split(/\r?\n/)
    .map(cleanListItem)
    .filter((line) => line && !/^no (?:priority )?(?:fixes|recommendations)/i.test(line));
}

function countListItems(body: string): number {
  if (!body.trim()) return 0;
  return body
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => /^[-*+]\s+/.test(line) || /^\d+[.)]\s+/.test(line))
    .length;
}

function parseDimensions(body: string): DimensionResult[] {
  if (!body.trim()) return [];
  return body
    .split(/\r?\n/)
    .map(cleanListItem)
    .map((line) => {
      const match = line.match(/^(?:\*\*)?([^*\u2014-]+?)(?:\*\*)?\s*[\u2014-]\s*(.+)$/);
      if (!match) return null;
      const dimension = match[1].trim();
      const rawStatus = match[2].trim();
      const noFindings = /no[_\s-]?findings|无发现/i.test(rawStatus);
      return {
        dimension,
        status: noFindings ? "done" : rawStatus,
        acceptedCount: noFindings ? 0 : 1,
        rejectedCount: 0,
        uncertainCount: 0,
      };
    })
    .filter((item): item is DimensionResult => item !== null);
}

function splitMarkdownTableRow(line: string): string[] {
  const trimmed = line.trim();
  if (!trimmed.startsWith("|")) return [];
  const content = trimmed.replace(/^\|/, "").replace(/\|\s*$/, "");
  const cells: string[] = [];
  let current = "";
  let escaped = false;
  for (const char of content) {
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === "\\") {
      escaped = true;
      continue;
    }
    if (char === "|") {
      cells.push(current.trim());
      current = "";
      continue;
    }
    current += char;
  }
  if (escaped) current += "\\";
  cells.push(current.trim());
  return cells;
}

function isTableSeparator(cells: string[]): boolean {
  return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function parseLocation(value: string): { file: string; line: number | null } | null {
  const clean = cleanListItem(value).replace(/^`|`$/g, "").trim();
  if (!clean || clean === "-") return null;
  const match = clean.match(/^(.+):(\d+)$/);
  if (!match) return { file: clean, line: null };
  return {
    file: match[1].trim(),
    line: Number.parseInt(match[2], 10),
  };
}

function normalizeSeverity(value: string): string | null {
  const text = normalizeHeading(value);
  if (text.includes("critical")) return "critical";
  if (text.includes("high")) return "high";
  if (text.includes("medium")) return "medium";
  if (text.includes("low")) return "low";
  return null;
}

function parseFindingRecommendations(markdown: string): Map<number, string> {
  const recommendations = new Map<number, string>();
  let activeIndex: number | null = null;
  for (const line of markdown.split(/\r?\n/)) {
    const detail = line.match(/^\s*(\d+)\.\s+\*\*.+?\*\*\s+\(`.+?`\)/);
    if (detail) {
      activeIndex = Number.parseInt(detail[1], 10);
      continue;
    }
    const recommendation = line.match(/^\s*[-*+]\s*Recommendation:\s*(.+)$/i);
    if (recommendation && activeIndex !== null) {
      recommendations.set(activeIndex, cleanListItem(recommendation[1]));
    }
  }
  return recommendations;
}

function parseFindings(markdown: string): Finding[] {
  const recommendations = parseFindingRecommendations(markdown);
  const findings: Finding[] = [];
  let inFindings = false;
  let currentSeverity = "medium";

  for (const line of markdown.split(/\r?\n/)) {
    const section = line.match(/^#{3}\s+(.+?)\s*$/);
    if (section) {
      const heading = normalizeHeading(section[1]);
      inFindings = heading === "findings" || heading === "审查发现";
      continue;
    }

    if (!inFindings) continue;

    const severityHeading = line.match(/^#{4,6}\s+(.+?)\s*$/);
    if (severityHeading) {
      currentSeverity = normalizeSeverity(severityHeading[1]) ?? currentSeverity;
      continue;
    }

    const cells = splitMarkdownTableRow(line);
    if (cells.length < 4 || isTableSeparator(cells) || !/^\d+$/.test(cells[0])) {
      continue;
    }
    const location = parseLocation(cells[1]);
    if (!location) continue;
    const index = Number.parseInt(cells[0], 10);
    findings.push({
      severity: currentSeverity,
      dimension: "report",
      file: location.file,
      line: location.line,
      title: cleanListItem(cells[2]) || `Finding ${index}`,
      impact: cleanListItem(cells[3]),
      recommendation: recommendations.get(index) ?? "",
    });
  }

  return findings;
}

function parseReviewReport(markdown: string): ParsedReviewReport {
  const sections = splitMarkdownSections(markdown);
  const summary = sectionBody(sections, ["executive summary", "summary", "审查总结", "总结"]);
  const checks = sectionBody(sections, ["checks performed", "review dimensions", "审查维度"]);
  const recommendations = parseRecommendations(
    sectionBody(sections, ["recommendations", "建议"]),
  );
  const needsConfirmationCount = countListItems(
    sectionBody(sections, ["needs confirmation", "待确认"]),
  );
  const rejectedCount = countListItems(
    sectionBody(sections, ["rejected/skipped summary", "rejected", "skipped", "拒绝", "跳过"]),
  );
  return {
    summary,
    dimensions: parseDimensions(checks),
    findings: parseFindings(markdown),
    recommendations,
    needsConfirmationCount,
    rejectedCount,
  };
}

export function uiMessageToChatMessage(message: UIMessage): ChatMessage | null {
  if (message.kind === "trace") return null;
  if (message.role !== "user" && message.role !== "assistant") return null;
  const content = message.role === "user"
    ? formatReviewRequestContent(message.content, message.review)
    : message.content;
  const thinking = message.role === "assistant" ? message.reasoning : undefined;
  if (message.role === "assistant" && !content.trim() && !thinking?.trim()) {
    return null;
  }
  const type = message.role === "assistant" && isLikelyReviewReport(message.content) ? "report" : "text";
  return {
    id: message.id,
    role: message.role === "user" ? "user" : "agent",
    type,
    content,
    timestamp: message.createdAt,
    thinking,
    streaming: message.role === "assistant" ? message.reasoningStreaming : undefined,
  };
}

function extractContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  return value
    .map((part) => {
      if (typeof part === "string") return part;
      if (!part || typeof part !== "object") return "";
      const text = (part as { text?: unknown }).text;
      if (typeof text === "string") return text;
      const type = (part as { type?: unknown }).type;
      if (typeof type === "string" && type !== "text") return `[${type}]`;
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function numberFromTimestamp(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 10_000_000_000 ? value : Math.round(value * 1000);
  }
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function reviewFromMetadata(metadata: unknown): UIMessage["review"] | undefined {
  if (!metadata || typeof metadata !== "object") return undefined;
  const data = metadata as Record<string, unknown>;
  if (data.review && typeof data.review === "object") {
    return data.review as UIMessage["review"];
  }
  const target = typeof data.review_target === "string" ? data.review_target : undefined;
  const targetType = typeof data.review_target_type === "string"
    ? data.review_target_type as OutboundReviewContext["target_type"]
    : undefined;
  const mode = typeof data.review_mode_variant === "string"
    ? data.review_mode_variant as OutboundReviewContext["mode"]
    : undefined;
  const action = typeof data.review_action === "string"
    ? data.review_action as OutboundReviewContext["action"]
    : undefined;
  const focus = Array.isArray(data.review_focus)
    ? data.review_focus.filter((item): item is ReviewFocus => typeof item === "string")
    : undefined;
  if (!target && !targetType && !mode && !action && !focus) {
    return undefined;
  }
  return {
    target,
    target_type: targetType,
    mode,
    action,
    focus,
  };
}

function stringFromUnknown(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export function sessionMessageToUIMessage(
  message: Record<string, unknown>,
  index: number,
): UIMessage | null {
  const role = message.role;
  if (role !== "user" && role !== "assistant") return null;
  const content = extractContent(message.content);
  const reasoning = stringFromUnknown(
    message.reasoning
      ?? message.reasoning_content
      ?? message.thinking
      ?? message.thinking_content,
  );
  if (!content.trim() && !(role === "assistant" && reasoning?.trim())) return null;
  const createdAt = numberFromTimestamp(
    message.createdAt ?? message.created_at ?? message.timestamp,
    Date.now() + index,
  );
  const review = reviewFromMetadata(message.metadata);
  return {
    id: typeof message.id === "string" ? message.id : `history-${index}`,
    role,
    content,
    kind: "message",
    createdAt,
    review,
    ...(role === "assistant" && reasoning ? { reasoning } : {}),
  };
}

function generateId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function appendThinking(message: ChatMessage, text: string, streaming = true): ChatMessage {
  const nextThinking = message.thinking ? `${message.thinking}${text}` : text;
  return {
    ...message,
    thinking: nextThinking,
    streaming: message.streaming || streaming,
  };
}

function appendProgressLine(message: ChatMessage, text: string): ChatMessage {
  const trimmed = text.trim();
  if (!trimmed) return message;
  const prefix = message.thinking && message.thinking.trim() ? "\n" : "";
  return appendThinking(message, `${prefix}${trimmed}\n`, true);
}

function appendProgressLines(message: ChatMessage, lines: string[]): ChatMessage {
  return lines.reduce((next, line) => appendProgressLine(next, line), message);
}

function formatElapsed(ms: unknown): string {
  return typeof ms === "number" && Number.isFinite(ms) ? `${(ms / 1000).toFixed(1)}s` : "";
}

function formatReviewPrefetchEvent(event: ToolProgressEvent): string | null {
  if (event.name !== "review_prefetch") return null;
  const args = event.arguments && typeof event.arguments === "object"
    ? event.arguments as Record<string, unknown>
    : {};
  const action = typeof args.action === "string" ? args.action : "repo";
  const targetType = typeof args.target_type === "string" ? args.target_type : "auto";
  const metadata = event.metadata && typeof event.metadata === "object"
    ? event.metadata as Record<string, unknown>
    : {};
  if (event.phase === "start") {
    const scopeKind = typeof metadata.scope_kind === "string" && metadata.scope_kind
      ? ` / ${metadata.scope_kind}`
      : "";
    return `Preparing review context: ${action} / ${targetType}${scopeKind}`;
  }
  if (event.phase === "progress") {
    const current = typeof metadata.current === "number" ? metadata.current : null;
    const total = typeof metadata.total === "number" ? metadata.total : null;
    if (current !== null && total !== null && total > 0) {
      return `Preparing review context: ${current}/${total}`;
    }
    const batch = typeof metadata.batch === "number" ? metadata.batch : null;
    const batches = typeof metadata.batches === "number" ? metadata.batches : null;
    if (batch !== null && batches !== null && batches > 0) {
      return `Embedding review context: ${batch}/${batches}`;
    }
    return "Preparing review context...";
  }
  if (event.phase === "end") {
    const elapsed = formatElapsed(metadata.elapsed_ms);
    if (event.result === "ok" || event.result === "no_summary") {
      const rawChars = typeof metadata.raw_chars === "number" ? metadata.raw_chars : null;
      const chars = rawChars !== null ? `, ${rawChars} chars` : "";
      return `Review context ready${elapsed ? ` in ${elapsed}` : ""}${chars}`;
    }
    const reason = typeof event.error === "string" && event.error.trim()
      ? `: ${event.error.trim()}`
      : "";
    return `Review context failed${elapsed ? ` after ${elapsed}` : ""}${reason}`;
  }
  if (event.phase === "skip") {
    const reason = typeof event.error === "string" && event.error.trim()
      ? ` (${event.error.trim()})`
      : "";
    return `Review context prefetch skipped${reason}`;
  }
  return null;
}

function progressLinesFromEvent(ev: InboundEvent): string[] {
  if (ev.event !== "message" || ev.kind !== "progress") return [];
  const lines: string[] = [];
  if (ev.text?.trim()) {
    lines.push(ev.text.trim());
  }
  if (Array.isArray(ev.tool_events)) {
    for (const toolEvent of ev.tool_events) {
      const line = formatReviewPrefetchEvent(toolEvent);
      if (line) lines.push(line);
    }
  }
  return lines;
}

function markLastStreamingComplete(messages: ChatMessage[]): ChatMessage[] {
  if (messages.length === 0) return messages;
  const updated = [...messages];
  for (let i = updated.length - 1; i >= 0; i -= 1) {
    const item = updated[i];
    if (item.role === "agent" && item.streaming) {
      updated[i] = { ...item, streaming: false };
      break;
    }
  }
  return updated;
}

function markStreamingCompleteById(messages: ChatMessage[], id: string | null): ChatMessage[] {
  if (!id) return messages;
  let changed = false;
  const updated = messages.map((message) => {
    if (message.id !== id || !message.streaming) return message;
    changed = true;
    return { ...message, streaming: false };
  });
  return changed ? updated : messages;
}

function findAssistantCarrierId(messages: ChatMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role === "user") break;
    if (message.role !== "agent") continue;
    if (message.type === "finding") continue;
    return message.id;
  }
  return null;
}

function hasMessage(messages: ChatMessage[], id: string | null): id is string {
  return !!id && messages.some((message) => message.id === id);
}

function stateWithReport(
  state: ReviewSessionState,
  reportMarkdown: string,
): ReviewSessionState {
  const parsed = parseReviewReport(reportMarkdown);
  return {
    ...state,
    reportMarkdown,
    reportSummary: parsed.summary,
    dimensions: parsed.dimensions,
    findings: parsed.findings,
    recommendations: parsed.recommendations,
    needsConfirmationCount: parsed.needsConfirmationCount,
    rejectedCount: parsed.rejectedCount,
  };
}

function isStopAckEvent(ev: InboundEvent): boolean {
  if (ev.event !== "message" || ev.kind || typeof ev.text !== "string") return false;
  return /^Stopped \d+ task\(s\)\.$/.test(ev.text) || ev.text === "No active task to stop.";
}

function markAllStreamingComplete(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((message) =>
    message.streaming ? { ...message, streaming: false } : message
  );
}

function hasVisibleAgentFinal(messages: ChatMessage[]): boolean {
  return messages.some((message) =>
    message.role === "agent" && message.content.trim().length > 0
  );
}

function hasThinkingOnlyAgent(messages: ChatMessage[]): boolean {
  return messages.some((message) =>
    message.role === "agent"
    && message.content.trim().length === 0
    && !!message.thinking?.trim()
  );
}

function completedOrStoppedPhase(messages: ChatMessage[]): ReviewPhase {
  if (hasVisibleAgentFinal(messages)) return "completed";
  if (hasThinkingOnlyAgent(messages)) return "stopped";
  return "history";
}

function isBusyPhase(phase: ReviewPhase): boolean {
  return phase === "submitting"
    || phase === "prefetching"
    || phase === "reviewing"
    || phase === "finalizing";
}

export function useReviewSession(client: NanobotClient, chatId: string | null) {
  const [state, setState] = useState<ReviewSessionState>(INITIAL_STATE);
  const reportBufferRef = useRef("");
  const assistantCarrierRef = useRef<string | null>(null);
  const reportMessageRef = useRef<string | null>(null);
  const textMessageRef = useRef<string | null>(null);
  const thinkingBufferRef = useRef("");
  const cancellingRef = useRef(false);
  const reviewInProgressRef = useRef(false);

  const reset = useCallback(() => {
    setState(INITIAL_STATE);
    reportBufferRef.current = "";
    assistantCarrierRef.current = null;
    reportMessageRef.current = null;
    textMessageRef.current = null;
    thinkingBufferRef.current = "";
    cancellingRef.current = false;
    reviewInProgressRef.current = false;
  }, []);

  const loadHistory = useCallback((
    messages: UIMessage[],
    task: ReviewTask | null = null,
    error: string | null = null,
    preConverted?: ChatMessage[],
  ) => {
    let chatMessages = preConverted ?? messages
      .map(uiMessageToChatMessage)
      .filter((message): message is ChatMessage => message !== null);

    // If a review is in progress and there's no agent message, add a placeholder
    // so the user sees a streaming indicator after page refresh. The
    // "Preparing review context" text is a transient progress event that is
    // not replayable from the transcript, so we show a generic placeholder.
    let placeholderId: string | null = null;
    if (reviewInProgressRef.current && !error) {
      const hasAgent = chatMessages.some(
        (m) => m.role === "agent" && (m.content.trim() || m.thinking?.trim()),
      );
      if (!hasAgent) {
        placeholderId = generateId();
        chatMessages = [...chatMessages, {
          id: placeholderId,
          role: "agent",
          type: "text",
          content: "",
          timestamp: Date.now(),
          thinking: "Review in progress...\n",
          streaming: true,
        }];
      }
    }

    const reportMarkdown = [...chatMessages].reverse().find((message) => message.type === "report")?.content ?? "";
    const inferredPhase = error ? "error" : completedOrStoppedPhase(chatMessages);
    const phase = reviewInProgressRef.current && !error ? "reviewing" : inferredPhase;
    setState(stateWithReport({
      ...INITIAL_STATE,
      phase,
      task,
      error,
      messages: chatMessages,
    }, reportMarkdown));
    reportBufferRef.current = "";
    assistantCarrierRef.current = placeholderId;
    reportMessageRef.current = null;
    textMessageRef.current = null;
    thinkingBufferRef.current = "";
  }, []);

  const startReview = useCallback((task: ReviewTask) => {
    cancellingRef.current = false;
    setState((prev) => ({
      ...prev,
      phase: "submitting",
      task,
      error: null,
      logs: [],
      dimensions: [],
      findings: [],
      reportSummary: "",
      recommendations: [],
      needsConfirmationCount: 0,
      rejectedCount: 0,
      reportMarkdown: "",
      subagentCards: [],
      messages: [
        ...prev.messages,
        {
          id: generateId(),
          role: "user",
          type: "text",
          content: formatReviewRequestContent("审查", task),
          timestamp: Date.now(),
        },
      ],
    }));
    reportBufferRef.current = "";
    assistantCarrierRef.current = null;
    reportMessageRef.current = null;
    textMessageRef.current = null;
    thinkingBufferRef.current = "";
  }, []);

  const sendFollowUp = useCallback(
    (text: string) => {
      if (!chatId) return;
      cancellingRef.current = false;
      const userMsg: ChatMessage = {
        id: generateId(),
        role: "user",
        type: "text",
        content: text,
        timestamp: Date.now(),
      };
      setState((prev) => ({
        ...prev,
        subagentCards: [],
        messages: [...prev.messages, userMsg],
      }));
      client.sendMessage(chatId, text);
      assistantCarrierRef.current = null;
      reportMessageRef.current = null;
      textMessageRef.current = null;
      reportBufferRef.current = "";
      thinkingBufferRef.current = "";
    },
    [client, chatId]
  );

  const cancelTurn = useCallback(() => {
    if (!chatId) return;
    cancellingRef.current = true;
    client.sendMessage(chatId, "/stop");
    setState((prev) => ({
      ...prev,
      phase: "stopped",
      messages: markAllStreamingComplete(prev.messages),
    }));
    assistantCarrierRef.current = null;
    reportMessageRef.current = null;
    textMessageRef.current = null;
  }, [client, chatId]);

  // Reset review-in-progress flag when chatId changes to avoid carrying
  // over state from a previous session (e.g. switching tasks in the sidebar).
  useEffect(() => {
    reviewInProgressRef.current = false;
  }, [chatId]);

  useEffect(() => {
    if (!chatId || !client) return;

    const unsub = client.onChat(chatId, (ev: InboundEvent) => {
      if (cancellingRef.current) {
        if (
          ev.event === "turn_end"
          || (ev.event === "goal_status" && ev.status === "idle")
          || isStopAckEvent(ev)
        ) {
          cancellingRef.current = false;
        }
        return;
      }
      setState((prev) => {
        if (ev.event === "subagent_status") {
          if (ev.status === "running") {
            if (prev.subagentCards.some((c) => c.id === ev.subagent_id)) {
              return prev;
            }
            return {
              ...prev,
              subagentCards: [
                ...prev.subagentCards,
                {
                  id: ev.subagent_id,
                  label: ev.label,
                  status: "running" as const,
                  thinking: "",
                  thinkingStreaming: false,
                  startedAt: Date.now(),
                },
              ],
            };
          }
          return {
            ...prev,
            subagentCards: prev.subagentCards.map((card) =>
              card.id === ev.subagent_id
                ? { ...card, status: ev.status, thinkingStreaming: false }
                : card
            ),
          };
        }

        if (ev.event === "delta") {
          if (ev.subagent_id) {
            // Subagent content deltas are not displayed as regular messages;
            // the final result is announced by the main agent.
            return prev;
          }
          const text = ev.text || "";
          const kind = ev.kind;
          if (kind === "review_thinking") {
            thinkingBufferRef.current += text;
            const existingId = hasMessage(prev.messages, assistantCarrierRef.current)
              ? assistantCarrierRef.current
              : findAssistantCarrierId(prev.messages);
            if (existingId) {
              assistantCarrierRef.current = existingId;
              const updated = prev.messages.map((message) =>
                message.id === existingId ? appendThinking(message, text, true) : message
              );
              return { ...prev, messages: updated, phase: "reviewing" };
            }
            const id = generateId();
            assistantCarrierRef.current = id;
            return {
              ...prev,
              phase: "reviewing",
              messages: [
                ...prev.messages,
                {
                  id,
                  role: "agent",
                  type: "text",
                  content: "",
                  timestamp: Date.now(),
                  thinking: text,
                  streaming: true,
                },
              ],
            };
          }

          const isReport = kind === "review_report" || reportBufferRef.current.length > 0;
          if (isReport) {
            reportBufferRef.current += text;
            const existingId = hasMessage(prev.messages, reportMessageRef.current)
              ? reportMessageRef.current
              : hasMessage(prev.messages, assistantCarrierRef.current)
                ? assistantCarrierRef.current
                : findAssistantCarrierId(prev.messages);
            if (existingId) {
              reportMessageRef.current = existingId;
              assistantCarrierRef.current = existingId;
              const updated = prev.messages.map((message) =>
                message.id === existingId
                  ? {
                      ...message,
                      type: "report" as const,
                      content: message.content + text,
                      thinking: message.thinking || thinkingBufferRef.current || undefined,
                      streaming: true,
                    }
                  : message
              );
              return stateWithReport({
                ...prev,
                phase: "finalizing",
                messages: updated,
              }, reportBufferRef.current);
            }
            const id = generateId();
            reportMessageRef.current = id;
            assistantCarrierRef.current = id;
            return stateWithReport({
              ...prev,
              phase: "finalizing",
              messages: [
                ...prev.messages,
                {
                  id,
                  role: "agent",
                  type: "report",
                  content: text,
                  timestamp: Date.now(),
                  thinking: thinkingBufferRef.current,
                  streaming: true,
                },
              ],
            }, reportBufferRef.current);
          }

          const existingId = hasMessage(prev.messages, textMessageRef.current)
            ? textMessageRef.current
            : null;
          if (existingId) {
            const updated = prev.messages.map((message) =>
              message.id === existingId && message.type === "text"
                ? { ...message, content: message.content + text, streaming: true }
                : message
            );
            return { ...prev, messages: updated };
          }

          const id = generateId();
          textMessageRef.current = id;
          assistantCarrierRef.current = id;
          return {
            ...prev,
            messages: [
              ...prev.messages,
              {
                id,
                role: "agent",
                type: "text",
                content: text,
                timestamp: Date.now(),
                streaming: true,
              },
            ],
          };
        }

        if (ev.event === "reasoning_delta") {
          if (ev.subagent_id) {
            return {
              ...prev,
              subagentCards: prev.subagentCards.map((card) =>
                card.id === ev.subagent_id
                  ? {
                      ...card,
                      thinking: card.thinking + (ev.text || ""),
                      thinkingStreaming: true,
                    }
                  : card
              ),
            };
          }
          const text = ev.text || "";
          thinkingBufferRef.current += text;
          const existingId = hasMessage(prev.messages, assistantCarrierRef.current)
            ? assistantCarrierRef.current
            : findAssistantCarrierId(prev.messages);
          if (existingId) {
            assistantCarrierRef.current = existingId;
            const updated = prev.messages.map((message) =>
              message.id === existingId ? appendThinking(message, text, true) : message
            );
            return { ...prev, phase: "reviewing", messages: updated };
          }
          const id = generateId();
          assistantCarrierRef.current = id;
          return {
            ...prev,
            phase: "reviewing",
            messages: [
              ...prev.messages,
              {
                id,
                role: "agent",
                type: "text",
                content: "",
                timestamp: Date.now(),
                thinking: text,
                streaming: true,
              },
            ],
          };
        }

        if (ev.event === "reasoning_end") {
          if (ev.subagent_id) {
            return {
              ...prev,
              subagentCards: prev.subagentCards.map((card) =>
                card.id === ev.subagent_id
                  ? { ...card, thinkingStreaming: false }
                  : card
              ),
            };
          }
          return prev;
        }

        if (ev.event === "stream_end") {
          if (ev.subagent_id) {
            return prev;
          }
          if (ev.kind === "review_thinking") {
            return prev;
          }
          if (ev.kind === "review_report") {
            const reportId = reportMessageRef.current;
            reportMessageRef.current = null;
            textMessageRef.current = null;
            return { ...prev, messages: markStreamingCompleteById(prev.messages, reportId) };
          }
          const textId = textMessageRef.current;
          textMessageRef.current = null;
          if (textId) {
            return { ...prev, messages: markStreamingCompleteById(prev.messages, textId) };
          } else {
            return { ...prev, messages: markLastStreamingComplete(prev.messages) };
          }
        }

        if (ev.event === "message") {
          const progressLines = progressLinesFromEvent(ev);
          const newLogs = [...prev.logs];
          if (ev.kind === "tool_hint" || ev.kind === "progress") {
            if (ev.text?.trim()) newLogs.push(ev.text.trim());
            newLogs.push(...progressLines.filter((line) => line !== ev.text?.trim()));
          }

          // 处理 agent_ui blob 中的 finding 流式事件
          if (ev.agent_ui) {
            const uiBlob = ev.agent_ui;

            if (uiBlob.kind === "finding_start") {
              // 创建一个新的流式 finding 消息
              const findingData = (uiBlob.data as Partial<Finding>) || {};
              const findingMsg: ChatMessage = {
                id: generateId(),
                role: "agent",
                type: "finding",
                content: findingData.title || "",
                timestamp: Date.now(),
                finding: {
                  severity: findingData.severity || "medium",
                  dimension: findingData.dimension || "",
                  file: findingData.file || "",
                  line: findingData.line ?? null,
                  title: findingData.title || "",
                  impact: findingData.impact || "",
                  recommendation: findingData.recommendation || "",
                  confidence: findingData.confidence,
                  evidence: findingData.evidence,
                },
                streaming: true,
              };
              return {
                ...prev,
                logs: newLogs,
                messages: [...prev.messages, findingMsg],
              };
            }

            if (uiBlob.kind === "finding_delta") {
              // 更新当前流式 finding 的字段
              const delta = (uiBlob.data as Partial<Finding>) || {};
              const findingId = [...prev.messages].reverse().find((m) => m.type === "finding" && m.streaming)?.id;
              if (!findingId) return { ...prev, logs: newLogs };

              const updated = prev.messages.map((m) => {
                if (m.id !== findingId) return m;
                const updatedFinding = { ...(m.finding || {}), ...delta };
                return {
                  ...m,
                  content: updatedFinding.title || m.content,
                  finding: updatedFinding as Finding,
                };
              });
              return { ...prev, logs: newLogs, messages: updated };
            }

            if (uiBlob.kind === "finding_end") {
              // 完成 finding，标记 streaming=false
              const findingId = [...prev.messages].reverse().find((m) => m.type === "finding" && m.streaming)?.id;
              if (!findingId) return { ...prev, logs: newLogs };

              const finalData = (uiBlob.data as Partial<Finding>) || {};
              const updated = prev.messages.map((m) => {
                if (m.id !== findingId) return m;
                const finalFinding = { ...(m.finding || {}), ...finalData };
                return {
                  ...m,
                  content: finalFinding.title || m.content,
                  finding: finalFinding as Finding,
                  streaming: false,
                };
              });
              return { ...prev, logs: newLogs, messages: updated };
            }
          }

          if (ev.kind === "progress" && progressLines.length > 0) {
            const existingId = hasMessage(prev.messages, assistantCarrierRef.current)
              ? assistantCarrierRef.current
              : findAssistantCarrierId(prev.messages);
            if (existingId) {
              assistantCarrierRef.current = existingId;
              const updated = prev.messages.map((message) =>
                message.id === existingId ? appendProgressLines(message, progressLines) : message
              );
              return { ...prev, logs: newLogs, phase: "prefetching", messages: updated };
            }
            const id = generateId();
            assistantCarrierRef.current = id;
            const thinking = progressLines.map((line) => line.trim()).filter(Boolean).join("\n");
            return {
              ...prev,
              phase: "prefetching",
              logs: newLogs,
              messages: [
                ...prev.messages,
                {
                  id,
                  role: "agent",
                  type: "text",
                  content: "",
                  timestamp: Date.now(),
                  thinking: `${thinking}\n`,
                  streaming: true,
                },
              ],
            };
          }

          if (!ev.kind && ev.text?.trim()) {
            const content = ev.text;
            const isReport = isLikelyReviewReport(content);
            const existingId = hasMessage(prev.messages, assistantCarrierRef.current)
              ? assistantCarrierRef.current
              : findAssistantCarrierId(prev.messages);
            const logs = isReport
              ? [...newLogs, "Review report arrived outside review_report stream"]
              : newLogs;
            if (existingId) {
              const updated = markAllStreamingComplete(prev.messages).map((message) =>
                message.id === existingId
                  ? {
                      ...message,
                      type: isReport ? "report" as const : message.type,
                      content,
                      streaming: false,
                    }
                  : message
              );
              if (isReport) reportMessageRef.current = existingId;
              return isReport ? stateWithReport({
                ...prev,
                logs,
                phase: isReport ? "completed" : prev.phase,
                messages: updated,
              }, content) : {
                ...prev,
                logs,
                phase: prev.phase,
                messages: updated,
              };
            }
            const ordinaryMessage: ChatMessage = {
              id: generateId(),
              role: "agent",
              type: isReport ? "report" : "text",
              content,
              timestamp: Date.now(),
              streaming: false,
            };
            return isReport ? stateWithReport({
              ...prev,
              logs,
              phase: isReport ? "completed" : prev.phase,
              messages: [...markAllStreamingComplete(prev.messages), ordinaryMessage],
            }, content) : {
              ...prev,
              logs,
              phase: prev.phase,
              messages: [...markAllStreamingComplete(prev.messages), ordinaryMessage],
            };
          }

          return { ...prev, logs: newLogs };
        }

        if (ev.event === "turn_end") {
          reviewInProgressRef.current = false;
          assistantCarrierRef.current = null;
          reportMessageRef.current = null;
          textMessageRef.current = null;
          const messages = markAllStreamingComplete(prev.messages);
          const subagentCards = prev.subagentCards.map((card) =>
            card.thinkingStreaming ? { ...card, thinkingStreaming: false } : card
          );
          const missingReviewReport = !!prev.task
            && isBusyPhase(prev.phase)
            && !prev.reportMarkdown
            && !messages.some((message) => message.type === "report" && message.content.trim());
          if (missingReviewReport) {
            return {
              ...prev,
              phase: "error",
              error: "Review report was not received.",
              subagentCards,
              messages: [
                ...messages,
                {
                  id: generateId(),
                  role: "agent",
                  type: "text",
                  content: "Error: review report was not received.",
                  timestamp: Date.now(),
                },
              ],
            };
          }
          const phase = completedOrStoppedPhase(messages);
          return {
            ...prev,
            phase: phase === "history" && isBusyPhase(prev.phase) ? "stopped" : phase,
            messages,
            subagentCards,
          };
        }

        if (ev.event === "goal_status") {
          if (ev.status === "running") {
            reviewInProgressRef.current = true;
            if (isBusyPhase(prev.phase)) return prev;
            // If there's no agent message, create a placeholder so the user
            // sees a streaming indicator after page refresh.
            const hasAgent = prev.messages.some(
              (m) => m.role === "agent" && (m.content.trim() || m.thinking?.trim()),
            );
            if (!hasAgent) {
              const id = generateId();
              assistantCarrierRef.current = id;
              return {
                ...prev,
                phase: "reviewing",
                messages: [
                  ...prev.messages,
                  {
                    id,
                    role: "agent",
                    type: "text",
                    content: "",
                    timestamp: Date.now(),
                    thinking: "Review in progress...\n",
                    streaming: true,
                  },
                ],
              };
            }
            return { ...prev, phase: "reviewing" };
          }
          if (ev.status !== "idle" || !isBusyPhase(prev.phase)) {
            return prev;
          }
          reviewInProgressRef.current = false;
          assistantCarrierRef.current = null;
          reportMessageRef.current = null;
          textMessageRef.current = null;
          return {
            ...prev,
            phase: "stopped",
            messages: markAllStreamingComplete(prev.messages),
          };
        }

        if (ev.event === "error") {
          assistantCarrierRef.current = null;
          reportMessageRef.current = null;
          textMessageRef.current = null;
          return {
            ...prev,
            phase: "error",
            error: ev.detail || "Unknown error",
            messages: [
              ...markAllStreamingComplete(prev.messages),
              {
                id: generateId(),
                role: "agent",
                type: "text",
                content: `Error: ${ev.detail || "Unknown error"}`,
                timestamp: Date.now(),
              },
            ],
          };
        }

        if (ev.event === "review_mode_updated") {
          return { ...prev };
        }

        return prev;
      });
    });

    return unsub;
  }, [client, chatId]);

  return { state, reset, loadHistory, startReview, sendFollowUp, cancelTurn };
}
