import { useState, useEffect, useCallback, useRef } from "react";
import { Loader2, CheckCircle2, XCircle, ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import type { SubagentCard } from "@/hooks/useReviewSession";

interface SubagentCardsProps {
  cards: SubagentCard[];
}

/** Compact elapsed-time label that ticks every second while the card is running. */
function ElapsedTime({ startedAt, active }: { startedAt: number; active: boolean }) {
  const [elapsed, setElapsed] = useState(() => {
    if (!active) return "";
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  });

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => {
      const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
      setElapsed(seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`);
    }, 1000);
    return () => clearInterval(timer);
  }, [startedAt, active]);

  return (
    <span
      className="text-[10px] text-muted-foreground/70 tabular-nums"
      style={{ visibility: elapsed ? "visible" : "hidden" }}
      aria-hidden={!elapsed}
    >
      {elapsed || "0s"}
    </span>
  );
}

const STATUS_CONFIG: Record<
  SubagentCard["status"],
  { icon: typeof Loader2; iconClass: string; borderClass: string; label: string }
> = {
  running: {
    icon: Loader2,
    iconClass: "text-blue-500 animate-spin",
    borderClass: "border-blue-200 dark:border-blue-900",
    label: "running",
  },
  completed: {
    icon: CheckCircle2,
    iconClass: "text-green-500",
    borderClass: "border-green-200 dark:border-green-900",
    label: "completed",
  },
  error: {
    icon: XCircle,
    iconClass: "text-red-500",
    borderClass: "border-red-200 dark:border-red-900",
    label: "error",
  },
};

export function SubagentCards({ cards }: SubagentCardsProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(
    () => new Set(cards.filter((card) => card.output && !card.outputStreaming).map((card) => card.id)),
  );
  const userToggledRef = useRef(new Set<string>());
  const previousStreamingRef = useRef(new Map(cards.map((card) => [card.id, card.outputStreaming])));
  const collapseTimersRef = useRef(new Map<string, number>());

  useEffect(() => {
    const previous = previousStreamingRef.current;
    for (const card of cards) {
      const wasStreaming = previous.get(card.id) ?? false;
      if (card.outputStreaming && !wasStreaming && !userToggledRef.current.has(card.id)) {
        const timer = collapseTimersRef.current.get(card.id);
        if (timer !== undefined) window.clearTimeout(timer);
        collapseTimersRef.current.delete(card.id);
        setCollapsed((current) => {
          const next = new Set(current);
          next.delete(card.id);
          return next;
        });
      }
      if (!card.outputStreaming && wasStreaming && !userToggledRef.current.has(card.id)) {
        const timer = window.setTimeout(() => {
          setCollapsed((current) => new Set(current).add(card.id));
          collapseTimersRef.current.delete(card.id);
        }, 700);
        collapseTimersRef.current.set(card.id, timer);
      }
      previous.set(card.id, card.outputStreaming);
    }
  }, [cards]);

  useEffect(() => () => {
    collapseTimersRef.current.forEach((timer) => window.clearTimeout(timer));
  }, []);

  const toggle = useCallback((id: string) => {
    userToggledRef.current.add(id);
    const timer = collapseTimersRef.current.get(id);
    if (timer !== undefined) window.clearTimeout(timer);
    collapseTimersRef.current.delete(id);
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  if (cards.length === 0) return null;

  return (
    <div className="space-y-1.5">
      {cards.map((card) => {
        const config = STATUS_CONFIG[card.status];
        const Icon = config.icon;
        const isCollapsed = collapsed.has(card.id);
        const hasOutput = card.output.length > 0 || card.outputStreaming;

        return (
          <div
            key={card.id}
            className={cn(
              "rounded-lg border bg-card/50 transition-all overflow-hidden",
              config.borderClass,
            )}
          >
            {/* Card header */}
            <button
              type="button"
              onClick={() => toggle(card.id)}
              className="flex items-center gap-2 w-full px-3 py-2 text-left hover:bg-black/[0.02] dark:hover:bg-white/[0.02] transition-colors"
              aria-expanded={!isCollapsed}
            >
              <Icon className={cn("w-3.5 h-3.5 shrink-0", config.iconClass)} />
              <span className="text-xs font-medium text-foreground truncate">
                {card.label}
              </span>
              <span className="text-[10px] text-muted-foreground/70 capitalize">
                {config.label}
              </span>
              <ElapsedTime startedAt={card.startedAt} active={card.status === "running"} />
              {hasOutput && (
                <div className="ml-auto">
                  {isCollapsed ? (
                    <ChevronRight className="w-3 h-3 text-muted-foreground" />
                  ) : (
                    <ChevronDown className="w-3 h-3 text-muted-foreground" />
                  )}
                </div>
              )}
            </button>

            {hasOutput && !isCollapsed && (
              <div className="border-t border-inherit/40 px-3 py-2">
                <div className="flex items-center gap-1.5 mb-1">
                  <span className="text-[10px] text-muted-foreground/60 font-medium">
                    Output
                  </span>
                  {card.outputStreaming && (
                    <span className="inline-block w-1 h-1 rounded-full bg-blue-500 animate-pulse" />
                  )}
                </div>
                <textarea
                  readOnly
                  aria-label={`${card.label} output`}
                  className={cn(
                    "block w-full h-32 resize-none border-0 bg-transparent p-0 text-[11px] text-muted-foreground/80 leading-relaxed focus:outline-none overflow-y-auto",
                    card.outputStreaming && "animate-pulse",
                  )}
                  value={card.output}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
