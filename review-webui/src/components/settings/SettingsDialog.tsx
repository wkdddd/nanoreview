import * as React from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Shield,
  X,
  RotateCcw,
  Check,
  Sun,
  Moon,
  Monitor,
} from "lucide-react";
import type { ReviewerProfile, ReviewFocus } from "@/lib/types";

export type ThemeMode = "light" | "dark" | "system";

export interface ReviewSettings {
  defaultFocus: ReviewFocus[];
  theme: ThemeMode;
}

const DEFAULT_SETTINGS: ReviewSettings = {
  defaultFocus: [],
  theme: "light",
};

const THEME_OPTIONS: {
  value: ThemeMode;
  label: string;
  description: string;
  icon: React.ElementType;
}[] = [
  { value: "light", label: "Light", description: "暖色调的明亮主题", icon: Sun },
  { value: "dark", label: "Dark", description: "温暖的深色主题", icon: Moon },
  { value: "system", label: "System", description: "跟随操作系统自动切换", icon: Monitor },
];

interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
  settings: ReviewSettings;
  onSettingsChange: (s: ReviewSettings) => void;
  profiles: ReviewerProfile[];
}

export function SettingsDialog({
  open,
  onClose,
  settings,
  onSettingsChange,
  profiles,
}: SettingsDialogProps) {
  const handleThemeChange = (theme: ThemeMode) => {
    onSettingsChange({ ...settings, theme });
  };

  const toggleDimension = (key: ReviewFocus) => {
    const next = settings.defaultFocus.includes(key)
      ? settings.defaultFocus.filter((item) => item !== key)
      : [...settings.defaultFocus, key];
    onSettingsChange({ ...settings, defaultFocus: next });
  };

  const handleReset = () => {
    onSettingsChange({ ...DEFAULT_SETTINGS });
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />

      <div className="relative z-10 flex max-h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-lg border bg-background shadow-lg">
        <div className="flex items-center justify-between border-b px-6 py-4">
          <h2 className="text-lg font-semibold">Settings</h2>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={onClose}
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>

        <div className="flex-1 space-y-6 overflow-y-auto px-6 py-5">
          {/* Appearance */}
          <section className="space-y-3">
            <h3 className="text-sm font-medium text-foreground">
              Appearance
            </h3>
            <div className="grid grid-cols-3 gap-2">
              {THEME_OPTIONS.map((option) => {
                const Icon = option.icon;
                const active = settings.theme === option.value;
                return (
                  <label
                    key={option.value}
                    className={cn(
                      "flex cursor-pointer flex-col items-center gap-1.5 rounded-md border p-3 transition-colors",
                      active
                        ? "border-primary bg-primary/5"
                        : "border-border hover:bg-accent/50",
                    )}
                  >
                    <input
                      type="radio"
                      name="theme-mode"
                      value={option.value}
                      checked={active}
                      onChange={() => handleThemeChange(option.value)}
                      className="sr-only"
                    />
                    <Icon
                      className={cn(
                        "h-5 w-5 transition-colors",
                        active ? "text-primary" : "text-muted-foreground",
                      )}
                    />
                    <span
                      className={cn(
                        "text-xs font-medium transition-colors",
                        active ? "text-foreground" : "text-muted-foreground",
                      )}
                    >
                      {option.label}
                    </span>
                    {active && (
                      <Check className="h-3 w-3 shrink-0 text-primary" />
                    )}
                  </label>
                );
              })}
            </div>
          </section>

          <Separator />

          <section className="space-y-3">
            <h3 className="text-sm font-medium text-foreground">Default Focus</h3>
            <TooltipProvider delayDuration={200}>
              <div className="flex flex-wrap gap-2">
                {profiles.map((dim) => {
                  const active = settings.defaultFocus.includes(dim.id);
                  const Icon = Shield;
                  return (
                    <Tooltip key={dim.id}>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          onClick={() => toggleDimension(dim.id)}
                          className={cn(
                            "inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium transition-colors",
                            active
                              ? "border-primary bg-primary text-primary-foreground hover:bg-primary/90"
                              : "border-border bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                          )}
                        >
                          <Icon className="h-3.5 w-3.5" />
                          <span>{dim.label}</span>
                          {active && <Check className="ml-0.5 h-3 w-3" />}
                        </button>
                      </TooltipTrigger>
                      <TooltipContent side="bottom">
                        <p>{dim.description}</p>
                      </TooltipContent>
                    </Tooltip>
                  );
                })}
              </div>
            </TooltipProvider>
          </section>
        </div>

        <div className="flex items-center justify-between border-t bg-muted/30 px-6 py-4">
          <Button
            variant="ghost"
            size="sm"
            className="text-muted-foreground hover:text-foreground"
            onClick={handleReset}
          >
            <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
            Reset Defaults
          </Button>
          <Button size="sm" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </div>
  );
}
