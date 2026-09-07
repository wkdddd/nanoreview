import { GitBranch, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";

export interface SessionInfo {
  target: string;
  dimensions?: string[];
  focus?: string;
  status?: "running" | "completed" | "failed" | "stopped";
  duration?: string;
  findingCounts?: {
    total: number;
    critical: number;
    high: number;
    medium: number;
    low: number;
  };
}

export interface SessionInfoBarProps {
  info: SessionInfo | null;
}

const statusDot: Record<string, string> = {
  running: "bg-emerald-500",
  completed: "bg-slate-400",
  failed: "bg-red-500",
  stopped: "bg-amber-500",
};

export function SessionInfoBar({ info }: SessionInfoBarProps) {
  if (!info) return null;

  const counts = info.findingCounts;

  return (
    <div className="flex items-center gap-1.5 min-w-0 flex-1">
      {/* Target */}
      <div className="flex items-center gap-1 min-w-0">
        <GitBranch className="h-2.5 w-2.5 shrink-0 text-muted-foreground" />
        <span className="text-[11px] text-muted-foreground truncate">
          {info.target}
        </span>
      </div>

      <span className="text-border text-[10px]">|</span>

      {/* Status dot */}
      {info.status && (
        <span
          className={cn(
            "inline-block h-1 w-1 rounded-full shrink-0",
            statusDot[info.status] || "bg-muted-foreground"
          )}
        />
      )}

      {/* Dimensions count */}
      {info.dimensions && info.dimensions.length > 0 && (
        <span className="text-[10px] text-muted-foreground/70 shrink-0">
          {info.dimensions.length}D
        </span>
      )}

      {/* Finding counts */}
      {counts && counts.total > 0 && (
        <>
          <span className="text-border text-[10px]">|</span>
          <div className="flex items-center gap-0.5 shrink-0">
            <AlertTriangle className="h-2.5 w-2.5 text-muted-foreground" />
            <span className="text-[10px] text-muted-foreground/70">
              {counts.total}
            </span>
            {counts.critical > 0 && (
              <span className="text-[9px] font-semibold text-red-600 bg-red-50 dark:text-red-400 dark:bg-red-950 px-0.5 py-0 rounded leading-3">
                {counts.critical}C
              </span>
            )}
            {counts.high > 0 && (
              <span className="text-[9px] font-semibold text-orange-600 bg-orange-50 dark:text-orange-400 dark:bg-orange-950 px-0.5 py-0 rounded leading-3">
                {counts.high}H
              </span>
            )}
            {counts.medium > 0 && (
              <span className="text-[9px] font-semibold text-yellow-600 bg-yellow-50 dark:text-yellow-400 dark:bg-yellow-950 px-0.5 py-0 rounded leading-3">
                {counts.medium}M
              </span>
            )}
            {counts.low > 0 && (
              <span className="text-[9px] font-semibold text-blue-600 bg-blue-50 dark:text-blue-400 dark:bg-blue-950 px-0.5 py-0 rounded leading-3">
                {counts.low}L
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
