import { Bug, Gauge, ShieldCheck, Wrench } from "lucide-react";
import { cn } from "@/lib/utils";
import type {
  ReviewerProfile,
  ReviewDepth,
  ReviewFocus,
  ReviewRoutingMode,
} from "@/lib/types";

export interface ReviewConfigProps {
  depth: ReviewDepth;
  onDepthChange: (depth: ReviewDepth) => void;
  routingMode: ReviewRoutingMode;
  onRoutingModeChange: (mode: ReviewRoutingMode) => void;
  focus: ReviewFocus[];
  onFocusChange: (focus: ReviewFocus[]) => void;
  profiles: ReviewerProfile[];
}

const DEPTH_OPTIONS = [
  { value: "quick", label: "Quick", description: "High-severity scan" },
  { value: "full", label: "Full", description: "Standard review" },
  { value: "deep", label: "Deep", description: "Broader evidence budget" },
] as const;

const PROFILE_ICONS = {
  bug: Bug,
  security: ShieldCheck,
  performance: Gauge,
  maintainability: Wrench,
} as const;

export function ReviewConfig({
  depth,
  onDepthChange,
  routingMode,
  onRoutingModeChange,
  focus,
  onFocusChange,
  profiles,
}: ReviewConfigProps) {
  const toggleFocus = (value: string) => {
    onFocusChange(
      focus.includes(value)
        ? focus.filter((item) => item !== value)
        : [...focus, value],
    );
  };

  return (
    <div className="flex flex-col gap-6">
      <fieldset>
        <legend className="mb-2.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground/70">
          Review Depth
        </legend>
        <div className="grid grid-cols-3 gap-2">
          {DEPTH_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => onDepthChange(option.value)}
              className={cn(
                "flex min-w-0 flex-col items-center gap-1 rounded-md border px-3 py-2.5 transition-colors",
                depth === option.value
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "border-border/60 text-muted-foreground hover:bg-secondary/60",
              )}
            >
              <span className="text-sm font-semibold">{option.label}</span>
              <span className="text-center text-[11px] leading-tight opacity-70">{option.description}</span>
            </button>
          ))}
        </div>
      </fieldset>

      <fieldset>
        <legend className="mb-2.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground/70">
          Reviewer Routing
        </legend>
        <div className="mb-3 grid grid-cols-2 rounded-md border border-border/70 p-0.5">
          {(["auto", "explicit"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => onRoutingModeChange(mode)}
              className={cn(
                "h-8 rounded-[5px] text-xs font-medium capitalize transition-colors",
                routingMode === mode
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:bg-secondary/70",
              )}
            >
              {mode === "explicit" ? "Custom" : "Auto"}
            </button>
          ))}
        </div>
        {routingMode === "explicit" && (
          <div className="grid grid-cols-2 gap-2">
            {profiles.map((profile) => {
              const active = focus.includes(profile.id);
              const Icon = PROFILE_ICONS[profile.id as keyof typeof PROFILE_ICONS] ?? Bug;
              return (
                <button
                  key={profile.id}
                  type="button"
                  onClick={() => toggleFocus(profile.id)}
                  title={profile.description}
                  className={cn(
                    "flex min-h-10 items-center gap-2 rounded-md border px-3 py-2 text-left text-xs font-medium transition-colors",
                    active
                      ? "border-primary/40 bg-primary/10 text-primary"
                      : "border-border/60 text-muted-foreground hover:bg-secondary/60",
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  <span className="min-w-0 break-words">{profile.label.replace(" Reviewer", "")}</span>
                </button>
              );
            })}
          </div>
        )}
      </fieldset>
    </div>
  );
}
