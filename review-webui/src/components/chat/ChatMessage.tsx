import { cn } from "@/lib/utils";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { FindingDetail } from "@/components/findings/FindingDetail";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Children, Fragment, isValidElement, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Finding } from "@/hooks/useReviewSession";

interface ChatMessageProps {
  message: {
    id: string;
    role: "user" | "agent";
    type: "text" | "finding" | "report";
    content: string;
    timestamp: number;
    finding?: Finding;
    thinking?: string;
    streaming?: boolean;
  };
  onSelectFinding?: (finding: Finding) => void;
  /** Parsed findings from the review report, used to look up correct
   * severity / dimension when the user clicks a table row or inline
   * code location in the rendered markdown. */
  findings?: Finding[];
  afterThinking?: ReactNode;
}

const LOCATION_RE = /^(.+\.(?:[A-Za-z0-9]+)):(\d+)$/;

function findingFromLocation(value: string): Finding | null {
  const match = value.trim().replace(/^`|`$/g, "").match(LOCATION_RE);
  if (!match) return null;
  return {
    severity: "needs-confirmation",
    dimension: "report",
    file: match[1],
    line: Number.parseInt(match[2], 10),
    title: `Code context for ${value.trim()}`,
    impact: "",
    recommendation: "",
  };
}

function normalizeSeverity(value: string): string | null {
  const text = value.trim().toLowerCase();
  if (text.includes("critical")) return "critical";
  if (text.includes("high")) return "high";
  if (text.includes("medium")) return "medium";
  if (text.includes("low")) return "low";
  return null;
}

function parseNeedsConfirmationFindings(markdown: string): Finding[] {
  const findings: Finding[] = [];
  let inSection = false;
  for (const line of markdown.split(/\r?\n/)) {
    const heading = line.match(/^#{1,6}\s+(.+?)\s*$/);
    if (heading) {
      const normalized = heading[1].trim().replace(/[:：]\s*$/, "").toLowerCase();
      inSection = normalized.includes("needs confirmation") || normalized.includes("待确认");
      continue;
    }
    if (!inSection) continue;
    const match = line.match(/^\s*[-*+]\s+\*\*(.+?)\*\*\s+\(`(.+?)`\)\s+-\s+(.+)$/);
    if (!match) continue;
    const location = findingFromLocation(match[2]);
    if (!location) continue;
    const detail = match[3].trim();
    const severity = normalizeSeverity(detail.match(/severity:\s*([^-]+)/i)?.[1] ?? "") ?? "needs-confirmation";
    const reason = detail.replace(/^severity:\s*[^-]+-\s*/i, "").trim();
    findings.push({
      ...location,
      severity,
      dimension: "needs-confirmation",
      title: match[1].trim(),
      impact: reason,
      recommendation: "Verify this candidate before treating the review as clean.",
    });
  }
  return findings;
}

/**
 * Look up a real parsed finding from the findings array by matching
 * ``file`` and ``line``. Returns a copy with the correct severity /
 * dimension / title / impact / recommendation, or ``null`` if no match.
 */
function lookupFinding(file: string, line: number | null, findings?: Finding[]): Finding | null {
  if (!findings || findings.length === 0) return null;
  return (
    findings.find(
      (f) => f.file === file && (line === null || f.line === line),
    ) ?? null
  );
}

function textFromNode(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textFromNode).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return textFromNode(node.props.children);
  }
  return "";
}

function tableRowFinding(children: ReactNode, findings?: Finding[]): Finding | null {
  const cells = Children.toArray(children)
    .map(textFromNode)
    .map((cell) => cell.trim())
    .filter(Boolean);
  if (cells.length < 2 || !/^\d+$/.test(cells[0])) return null;
  const location = cells.find((cell) => findingFromLocation(cell));
  const synthetic = location ? findingFromLocation(location) : null;
  if (!synthetic) return null;
  // Prefer the real parsed finding (which has the correct severity)
  // over the synthetic one (which hardcodes severity="medium").
  const real = lookupFinding(synthetic.file, synthetic.line, findings);
  return real ?? {
    ...synthetic,
    title: cells[2] || synthetic.title,
    impact: cells[3] || "",
  };
}

/** Extract header cell texts from a markdown table element's children. */
function extractTableHeaders(tableChildren: ReactNode): string[] {
  const parts = Children.toArray(tableChildren);
  const thead = parts.find((p) => isValidElement(p) && p.type === "thead");
  if (!isValidElement(thead)) return [];
  const headerRow = Children.toArray(thead.props.children).find((p) =>
    isValidElement(p),
  );
  if (!isValidElement(headerRow)) return [];
  return Children.toArray(headerRow.props.children).map((th) =>
    textFromNode(th).trim(),
  );
}

/** Detect the standard findings table (#, File, Issue, Impact). */
function isFindingsTable(headers: string[]): boolean {
  return (
    headers.length === 4 &&
    /^#\s*$/.test(headers[0]) &&
    /file/i.test(headers[1]) &&
    /issue/i.test(headers[2]) &&
    /impact/i.test(headers[3])
  );
}

export function ChatMessage({ message, onSelectFinding, findings, afterThinking }: ChatMessageProps) {
  const isUser = message.role === "user";
  const isThinkingOnly = !isUser
    && message.type === "text"
    && message.content.trim().length === 0
    && !!message.thinking?.trim();
  const hasThinking = message.thinking && message.thinking.length > 0;
  const [userToggledThinking, setUserToggledThinking] = useState(false);
  const [showThinking, setShowThinking] = useState(() => Boolean(message.streaming || isThinkingOnly));
  const collapseTimerRef = useRef<number | null>(null);
  const hasVisibleContent = message.type !== "text" || message.content.trim().length > 0;
  const clickableFindings = useMemo(
    () => [
      ...(findings ?? []),
      ...(message.type === "report" ? parseNeedsConfirmationFindings(message.content) : []),
    ],
    [findings, message.content, message.type],
  );
  const toggleThinking = () => {
    setUserToggledThinking(true);
    setShowThinking((value) => !value);
  };

  useEffect(() => {
    if (!hasThinking) return;
    if (collapseTimerRef.current !== null) {
      window.clearTimeout(collapseTimerRef.current);
      collapseTimerRef.current = null;
    }
    if (message.streaming) {
      if (!userToggledThinking) setShowThinking(true);
      return;
    }
    if (isThinkingOnly) {
      if (!userToggledThinking) setShowThinking(true);
      return;
    }
    if (!userToggledThinking) {
      collapseTimerRef.current = window.setTimeout(() => {
        setShowThinking(false);
        collapseTimerRef.current = null;
      }, 700);
    }
    return () => {
      if (collapseTimerRef.current !== null) {
        window.clearTimeout(collapseTimerRef.current);
        collapseTimerRef.current = null;
      }
    };
  }, [hasThinking, isThinkingOnly, message.streaming, userToggledThinking]);

  return (
    <div className={cn("flex flex-col", isUser ? "items-end" : "items-start")}>
      {/* Thinking block (agent only, collapsible) */}
      {hasThinking && !isUser && (
        <div className="mb-1.5 w-full max-w-[80%]">
          <button
            onClick={toggleThinking}
            className="flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground transition-colors px-1"
            aria-label="Toggle thinking"
            aria-expanded={showThinking}
          >
            {showThinking ? (
              <ChevronDown className="w-2.5 h-2.5" />
            ) : (
              <ChevronRight className="w-2.5 h-2.5" />
            )}
            <span className="italic">
              {message.streaming && message.type !== "report"
                ? "Thinking..."
                : isThinkingOnly
                  ? "已停止前的思考"
                  : "Thinking"}
            </span>
          </button>
          {showThinking && (
            <div className="mt-0.5 px-2 py-1.5 text-[11px] text-muted-foreground/80 leading-relaxed bg-muted/50 rounded-md border border-border/50 max-h-32 overflow-y-auto scrollbar-thin">
              {message.thinking}
            </div>
          )}
        </div>
      )}

      {afterThinking}

      {hasVisibleContent && (
        <div
          className={cn(
            "text-xs leading-relaxed",
            isUser
              ? "px-3 py-2 bg-primary/10 rounded-xl rounded-tr-sm text-foreground max-w-[65%]"
              : "max-w-[90%]"
          )}
        >
          {message.type === "finding" && message.finding ? (
            <div
              className="cursor-pointer"
              onClick={() => onSelectFinding?.(message.finding!)}
            >
              <FindingDetail finding={message.finding} variant="card" />
            </div>
          ) : message.type === "report" ? (
            <div className="prose prose-sm markdown-content max-w-none">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  code({ children, className, ...props }) {
                    const text = String(children).trim();
                    const locationFinding = findingFromLocation(text);
                    if (!className && locationFinding) {
                      const real = lookupFinding(
                        locationFinding.file,
                        locationFinding.line,
                        clickableFindings,
                      );
                      return (
                        <button
                          type="button"
                          className="rounded bg-muted px-1.5 py-0.5 font-mono text-[0.85em] text-primary hover:bg-primary/10"
                          onClick={() => onSelectFinding?.(real ?? locationFinding)}
                        >
                          {text}
                        </button>
                      );
                    }
                    return (
                      <code className={className} {...props}>
                        {children}
                      </code>
                    );
                  },
                  tr({ children, ...props }) {
                    const rowFinding = tableRowFinding(children, clickableFindings);
                    if (!rowFinding) {
                      return <tr {...props}>{children}</tr>;
                    }
                    return (
                      <tr
                        {...props}
                        role="button"
                        tabIndex={0}
                        className="cursor-pointer hover:bg-primary/5"
                        onClick={() => onSelectFinding?.(rowFinding)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            onSelectFinding?.(rowFinding);
                          }
                        }}
                      >
                        {children}
                      </tr>
                    );
                  },
                  table({ children, ...props }) {
                    const headers = extractTableHeaders(children);
                    const findingsTable = isFindingsTable(headers);
                    return (
                      <div className="overflow-x-auto my-2">
                        <table
                          className={cn(findingsTable && "table-fixed w-full")}
                          {...props}
                        >
                          {findingsTable && (
                            <colgroup>
                              <col className="w-8" />
                              <col className="w-[20%]" />
                              <col className="w-[30%]" />
                              <col />
                            </colgroup>
                          )}
                          {children}
                        </table>
                      </div>
                    );
                  },
                  td({ children, ...props }) {
                    const text = textFromNode(children).trim();
                    const loc = findingFromLocation(text);
                    if (loc) {
                      // 完整路径显示，在分隔符 (/ \ :) 后插入 <wbr>
                      // 实现按需换行：空间足够时单行，不够时在分隔符处断行
                      const full =
                        loc.line != null ? `${loc.file}:${loc.line}` : loc.file;
                      const segments = full.split(/([\/\\:])/);
                      return (
                        <td title={text} {...props}>
                          <code className="font-mono text-[0.8em] text-primary/90">
                            {segments.map((seg, i) => (
                              <Fragment key={i}>
                                {seg}
                                {/[\/\\:]/.test(seg) ? <wbr /> : null}
                              </Fragment>
                            ))}
                          </code>
                        </td>
                      );
                    }
                    return <td {...props}>{children}</td>;
                  },
                }}
              >
                {message.content}
              </ReactMarkdown>
            </div>
          ) : (
            <span className="whitespace-pre-wrap">{message.content}</span>
          )}

          {/* Streaming cursor */}
          {message.streaming && (
            <span
              className="inline-block w-1 h-3 bg-foreground/60 animate-pulse ml-0.5 align-text-bottom rounded-sm"
            />
          )}
        </div>
      )}
      {!hasVisibleContent && message.streaming && !isUser && (
        <div
          className="ml-1 inline-block h-2.5 w-2.5 rounded-full border-2 border-muted-foreground/40 border-t-muted-foreground animate-spin"
          aria-label="Thinking"
        />
      )}
    </div>
  );
}
