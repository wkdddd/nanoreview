import type { ReactNode } from "react";
import { PanelLeft } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { useIsMobile } from "@/hooks/use-mobile";
import { ReviewHeader } from "./ReviewHeader";
import { ReviewSidebar } from "./ReviewSidebar";
import { SessionInfoBar, type SessionInfo } from "./SessionInfoBar";
import type { ChatSummary } from "@/lib/types";

export interface ReviewShellProps {
  /** Header props */
  connectionStatus: string;
  modelName: string | null;
  onOpenSettings?: () => void;
  onLogout?: () => void;

  /** Sidebar props */
  dailySessions: ChatSummary[];
  activeKey: string | null;
  sidebarLoading: boolean;
  sidebarError?: string | null;
  onTaskSelect: (key: string) => void;
  onNewTask: () => void;
  onTaskDelete: (key: string) => void;
  onTaskPin: (key: string) => void;
  onTaskRename: (key: string, customTitle: string) => Promise<void>;

  /** Content areas */
  mainContent: ReactNode;
  rightPanelContent?: ReactNode;

  /** Panel visibility */
  sidebarOpen: boolean;
  onToggleSidebar: () => void;

  /** Session info bar */
  sessionInfo?: SessionInfo | null;
}

export function ReviewShell({
  connectionStatus,
  modelName,
  onOpenSettings,
  onLogout,
  dailySessions,
  activeKey,
  sidebarLoading,
  sidebarError,
  onTaskSelect,
  onNewTask,
  onTaskDelete,
  onTaskPin,
  onTaskRename,
  mainContent,
  rightPanelContent,
  sidebarOpen,
  onToggleSidebar,
  sessionInfo,
}: ReviewShellProps) {
  const isMobile = useIsMobile();

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background">
      {/* Fixed header */}
      <ReviewHeader
        connectionStatus={connectionStatus}
        modelName={modelName}
        onOpenSettings={onOpenSettings}
        onLogout={onLogout}
      />

      {/* Body: sidebar | main+right */}
      <div className="flex flex-1 overflow-hidden relative">
        {/* Left sidebar (collapsible) */}
        {sidebarOpen ? (
          <div className="flex shrink-0 relative">
            <ReviewSidebar
              dailySessions={dailySessions}
              activeKey={activeKey}
              loading={sidebarLoading}
              error={sidebarError}
              onSelect={onTaskSelect}
              onNewTask={onNewTask}
              onDelete={onTaskDelete}
              onPin={onTaskPin}
              onRename={onTaskRename}
            />
          </div>
        ) : null}

        {/* Main content area + right panel (always rendered together) */}
        <main className="flex-1 overflow-hidden flex flex-col relative">
          {/* Toolbar: sidebar toggle + session info in one row */}
          <div className="flex items-center px-2.5 py-1 border-b bg-card/50 shrink-0 gap-1.5">
            <Button
              variant="ghost"
              size="icon"
              className="h-5 w-5 rounded shrink-0"
              onClick={onToggleSidebar}
              title={sidebarOpen ? "Collapse sidebar (Ctrl+B)" : "Expand sidebar (Ctrl+B)"}
            >
              <PanelLeft className="h-3 w-3" />
            </Button>

            {/* Session info inline */}
            <SessionInfoBar info={sessionInfo || null} />
          </div>

          {/* Content row: chat thread + resizable right panel.
              Right panel: starts ~320px, draggable up to ~half the screen
              (50vw), no narrower than 300px. Hidden below the md breakpoint,
              matching the previous `hidden md:block` behavior. Double-clicking
              the handle resets the right panel to its default width. */}
          <div className="flex flex-1 min-h-0 overflow-hidden">
            {rightPanelContent && !isMobile ? (
              <ResizablePanelGroup
                orientation="horizontal"
                className="h-full w-full"
              >
                <ResizablePanel className="h-full min-w-0 overflow-hidden">
                  {mainContent}
                </ResizablePanel>
                <ResizableHandle />
                <ResizablePanel
                  defaultSize={320}
                  minSize={300}
                  maxSize="50vw"
                  className="h-full overflow-y-auto bg-card scrollbar-thin scrollbar-track-transparent"
                >
                  {rightPanelContent}
                </ResizablePanel>
              </ResizablePanelGroup>
            ) : (
              <div className="flex-1 min-w-0 overflow-hidden">{mainContent}</div>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
