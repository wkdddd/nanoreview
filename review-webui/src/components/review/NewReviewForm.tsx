import { useState, useCallback, useEffect } from "react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { TargetInput } from "./TargetInput";
import { ReviewConfig } from "./ReviewConfig";
import type { ReviewerProfile, ReviewAction, ReviewFocus, ReviewRoutingMode } from "@/lib/types";

export interface NewReviewSubmit {
  target: string;
  action: ReviewAction;
  focus: ReviewFocus[];
}

export interface NewReviewFormProps {
  onSubmit: (task: NewReviewSubmit) => void;
  submitting: boolean;
  defaultFocus?: ReviewFocus[];
  profiles: ReviewerProfile[];
}

const ACTION_OPTIONS = [
  { value: "repo", label: "Repo" },
  { value: "diff", label: "Diff" },
] as const;

const GITHUB_URL_RE = /^(?:https?:\/\/)?(?:www\.)?github\.com\/[^/\s]+\/[^/\s]+(?:[/?#].*)?$/i;
const GITHUB_PR_URL_RE = /^(?:https?:\/\/)?(?:www\.)?github\.com\/[^/\s]+\/[^/\s]+\/pull\/\d+(?:[/?#].*)?$/i;

function SegmentedControl<T extends string>({
  label,
  value,
  options,
  disabledValues = [],
  onChange,
}: {
  label: string;
  value: T;
  options: readonly { value: T; label: string }[];
  disabledValues?: readonly T[];
  onChange: (value: T) => void;
}) {
  return (
    <fieldset className="min-w-0">
      <legend className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/70">
        {label}
      </legend>
      <div className="grid grid-cols-2 rounded-md border border-border/70 bg-background/70 p-0.5">
        {options.map((option) => {
          const disabled = disabledValues.includes(option.value);
          return (
            <button
              key={option.value}
              type="button"
              onClick={() => {
                if (!disabled) onChange(option.value);
              }}
              disabled={disabled}
              className={cn(
                "h-7 rounded-[5px] px-3 text-xs font-medium transition-colors disabled:cursor-not-allowed",
                value === option.value
                  ? "bg-primary text-primary-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-secondary/70 hover:text-foreground",
                disabled && "text-muted-foreground/35 hover:bg-transparent hover:text-muted-foreground/35",
              )}
            >
              {option.label}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

export function NewReviewForm({
  onSubmit,
  submitting,
  defaultFocus = [],
  profiles,
}: NewReviewFormProps) {
  const [target, setTarget] = useState("");
  const [action, setAction] = useState<ReviewAction>("repo");
  const [focus, setFocus] = useState<ReviewFocus[]>(defaultFocus);
  const [routingMode, setRoutingMode] = useState<ReviewRoutingMode>(
    defaultFocus.length > 0 ? "explicit" : "auto",
  );
  const [error, setError] = useState<string | null>(null);
  const trimmedTarget = target.trim();
  const isGithubTarget = GITHUB_URL_RE.test(trimmedTarget);
  const isGithubPrTarget = GITHUB_PR_URL_RE.test(trimmedTarget);

  useEffect(() => {
    setFocus(defaultFocus);
    setRoutingMode(defaultFocus.length > 0 ? "explicit" : "auto");
  }, [defaultFocus]);

  useEffect(() => {
    if (isGithubPrTarget && action !== "diff") {
      setAction("diff");
      setError(null);
    }
  }, [action, isGithubPrTarget]);

  const handleSubmit = useCallback(() => {
    const trimmed = trimmedTarget;
    if (!trimmed || submitting) return;
    const effectiveAction: ReviewAction = isGithubPrTarget ? "diff" : action;
    if (routingMode === "explicit" && focus.length === 0) {
      setError("Select at least one reviewer for Custom routing.");
      return;
    }
    if (isGithubTarget && effectiveAction === "diff" && !isGithubPrTarget) {
      setError("GitHub Diff review requires a pull request URL, for example https://github.com/owner/repo/pull/123.");
      return;
    }
    setError(null);
    onSubmit({
      target: trimmed,
      action: effectiveAction,
      focus: routingMode === "auto" ? [] : focus,
    });
  }, [trimmedTarget, submitting, isGithubPrTarget, action, isGithubTarget, focus, routingMode, onSubmit]);

  const handleTargetChange = useCallback((value: string) => {
    setTarget(value);
    if (error) setError(null);
  }, [error]);

  const handleActionChange = useCallback((value: ReviewAction) => {
    if (isGithubPrTarget && value === "repo") return;
    setAction(value);
    if (error) setError(null);
  }, [error, isGithubPrTarget]);

  return (
    <div className="flex h-full min-h-0 w-full items-center justify-center overflow-hidden px-3 py-3">
      <Card className="flex max-h-full w-full max-w-xl flex-col border-border/50 shadow-[0_1px_3px_0_hsl(var(--foreground)/0.04),0_4px_12px_0_hsl(var(--foreground)/0.02)]">
        <CardContent className="min-h-0 overflow-y-auto p-4 scrollbar-thin scrollbar-track-transparent">
          <div className="flex min-h-0 flex-col">
            {/* Title */}
            <div className="mb-3">
              <h1 className="text-base font-semibold tracking-tight text-foreground">
                New Code Review
              </h1>
              <p className="mt-0.5 text-xs text-muted-foreground/70">
                Provide a target and configure the review parameters.
              </p>
            </div>

            <div className="flex flex-col gap-4">
              {/* Target input */}
              <div>
                <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-muted-foreground/70">
                  Target
                </label>
                <TargetInput
                  value={target}
                  onChange={handleTargetChange}
                  onSubmit={handleSubmit}
                />
                {error ? (
                  <p className="mt-1.5 text-xs leading-relaxed text-destructive">
                    {error}
                  </p>
                ) : null}
              </div>

              <div className="max-w-[240px]">
                <SegmentedControl
                  label="Scope"
                  value={action}
                  options={ACTION_OPTIONS}
                  disabledValues={isGithubPrTarget ? ["repo"] : []}
                  onChange={handleActionChange}
                />
              </div>

              <Separator className="bg-border/50" />

              {/* Review configuration */}
              <ReviewConfig
                routingMode={routingMode}
                onRoutingModeChange={setRoutingMode}
                focus={focus}
                onFocusChange={setFocus}
                profiles={profiles}
              />

              <Separator className="bg-border/50" />

              {/* Submit */}
              <div className="flex justify-end">
                <Button
                  type="button"
                  onClick={handleSubmit}
                  disabled={!target.trim() || focus.length === 0 || submitting}
                  className={cn(
                    "h-8 min-w-[100px] gap-1.5 font-medium text-xs",
                    "bg-primary text-primary-foreground",
                    "shadow-[0_1px_2px_0_hsl(var(--primary)/0.3)]",
                    "hover:bg-primary/90",
                    "disabled:opacity-40 disabled:shadow-none",
                  )}
                >
                  {submitting ? (
                    <>
                      <Loader2 className="h-3 w-3 animate-spin" />
                      Starting…
                    </>
                  ) : (
                    "Start Review"
                  )}
                </Button>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
