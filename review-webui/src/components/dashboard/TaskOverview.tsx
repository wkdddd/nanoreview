import { useMemo } from "react";
import { cn } from "@/lib/utils";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

import { Clock, AlertTriangle, AlertOctagon, Info } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Finding } from "@/hooks/useReviewSession";

interface TaskOverviewProps {
  task: { target: string } | null;
  findings: Finding[];
  summary: string;
}

function computeQualityScore(findings: Finding[]): number {
  if (findings.length === 0) return 10;
  const critical = findings.filter((f) => f.severity === "critical").length;
  const high = findings.filter((f) => f.severity === "high").length;
  const medium = findings.filter((f) => f.severity === "medium").length;
  const low = findings.filter((f) => f.severity === "low").length;

  // Weighted penalty: critical=-3, high=-2, medium=-1, low=-0.3
  const penalty = critical * 3 + high * 2 + medium * 1 + low * 0.3;
  const score = Math.max(1, Math.round(10 - penalty));
  return Math.min(10, Math.max(1, score));
}

function StatCard({
  count,
  label,
  colorClass,
  icon: Icon,
}: {
  count: number;
  label: string;
  colorClass: string;
  icon: React.ElementType;
}) {
  return (
    <Card className="paper-texture">
      <CardContent className="p-4 flex items-center gap-3">
        <div className={cn("p-2 rounded-lg", colorClass)}>
          <Icon className="w-4 h-4" />
        </div>
        <div>
          <div className="text-2xl font-bold leading-none">{count}</div>
          <div className="text-xs text-muted-foreground mt-0.5">{label}</div>
        </div>
      </CardContent>
    </Card>
  );
}

function CircularProgress({ value, max = 10 }: { value: number; max?: number }) {
  const percentage = (value / max) * 100;
  const circumference = 2 * Math.PI * 36; // radius = 36
  const strokeDashoffset = circumference - (percentage / 100) * circumference;

  const color =
    value >= 8
      ? "text-green-600"
      : value >= 6
        ? "text-yellow-600"
        : value >= 4
          ? "text-orange-500"
          : "text-red-500";

  return (
    <div className="relative inline-flex items-center justify-center">
      <svg className="w-20 h-20 -rotate-90" viewBox="0 0 80 80">
        {/* Background circle */}
        <circle
          cx="40"
          cy="40"
          r="36"
          fill="none"
          stroke="hsl(var(--muted))"
          strokeWidth="6"
        />
        {/* Progress circle */}
        <circle
          cx="40"
          cy="40"
          r="36"
          fill="none"
          className={color}
          stroke="currentColor"
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={strokeDashoffset}
          style={{ transition: "stroke-dashoffset 0.6s ease" }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className={cn("text-xl font-bold", color)}>{value}</span>
        <span className="text-[10px] text-muted-foreground">/ {max}</span>
      </div>
    </div>
  );
}

export function TaskOverview({ task, findings, summary }: TaskOverviewProps) {
  const stats = useMemo(() => {
    const critical = findings.filter((f) => f.severity === "critical").length;
    const high = findings.filter((f) => f.severity === "high").length;
    const medium = findings.filter((f) => f.severity === "medium").length;
    const low = findings.filter((f) => f.severity === "low").length;
    return { total: findings.length, critical, high, medium, low };
  }, [findings]);

  const qualityScore = useMemo(() => computeQualityScore(findings), [findings]);

  return (
    <div className="space-y-4">
      {/* Top row: target name and timestamp */}
      <div className="flex items-center gap-3 flex-wrap">
        <h2 className="text-xl font-semibold text-foreground">
          {task?.target ?? "Review Target"}
        </h2>
        <span className="text-xs text-muted-foreground ml-auto">
          {new Date().toLocaleString()}
        </span>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          count={stats.total}
          label="Total Findings"
          colorClass="bg-muted text-muted-foreground"
          icon={Info}
        />
        <StatCard
          count={stats.critical}
          label="Critical"
          colorClass="bg-red-100 text-red-700"
          icon={AlertOctagon}
        />
        <StatCard
          count={stats.high}
          label="High"
          colorClass="bg-orange-100 text-orange-700"
          icon={AlertTriangle}
        />
        <StatCard
          count={stats.medium + stats.low}
          label="Medium / Low"
          colorClass="bg-blue-100 text-blue-700"
          icon={Clock}
        />
      </div>

      {/* Quality score + summary */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="flex-shrink-0 flex items-center justify-center">
          <CircularProgress value={qualityScore} />
        </div>
        <Card className="paper-texture flex-1">
          <CardContent className="p-4">
            <h3 className="text-sm font-semibold mb-2 text-foreground">
              Executive Summary
            </h3>
            {summary ? (
              <div className="prose prose-sm markdown-content max-w-none text-muted-foreground">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {summary}
                </ReactMarkdown>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground italic">
                No summary available yet.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
