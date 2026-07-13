import { useState, useEffect, useCallback } from "react";
import { Loader2, CheckCircle2, XCircle, ChevronDown, ChevronRight, Brain } from "lucide-react";
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

  if (!elapsed) return null;
  return <span className="text-[10px] text-muted-foreground/70 tabular-nums">{elapsed}</span>;
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
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const toggle = useCallback((id: string) => {
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
        const hasThinking = card.thinking.trim().length > 0;

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
              {hasThinking && (
                <div className="ml-auto">
                  {isCollapsed ? (
                    <ChevronRight className="w-3 h-3 text-muted-foreground" />
                  ) : (
                    <ChevronDown className="w-3 h-3 text-muted-foreground" />
                  )}
                </div>
              )}
            </button>

            {/* Thinking content */}
            {hasThinking && !isCollapsed && (
              <div className="border-t border-inherit/40 px-3 py-2">
                <div className="flex items-center gap-1.5 mb-1">
                  <Brain className="w-3 h-3 text-muted-foreground/60" />
                  <span className="text-[10px] text-muted-foreground/60 font-medium">
                    Thinking
                  </span>
                  {card.thinkingStreaming && (
                    <span className="inline-block w-1 h-1 rounded-full bg-blue-500 animate-pulse" />
                  )}
                </div>
                <div
                  className={cn(
                    "text-[11px] text-muted-foreground/80 whitespace-pre-wrap break-words max-h-40 overflow-y-auto leading-relaxed",
                    card.thinkingStreaming && "animate-pulse",
                  )}
                >
                  {card.thinking}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
