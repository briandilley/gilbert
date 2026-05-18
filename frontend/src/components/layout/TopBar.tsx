import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/hooks/useAuth";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useWsApi } from "@/hooks/useWsApi";
import { useIsMobile } from "@/hooks/useMediaQuery";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { MenuIcon } from "lucide-react";
import { NotificationBell } from "@/components/notifications/NotificationBell";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { BrowserSpeakerControl } from "./BrowserSpeakerControl";
import { cn } from "@/lib/utils";
import { groupIconFor, GROUP_COLORS } from "./nav-shared";
import type { NavGroup, NavItem } from "@/types/dashboard";

/**
 * Primary nav across the top: horizontal row of top-level groups
 * (Chat, Inbox, MCP, Security, System, plugin-contributed top-levels)
 * plus the right-side cluster (connection dot, ``header.widgets``
 * plugin slot, notifications, avatar with ``header.user-menu``).
 *
 * Clicking a top-level group navigates to its default URL. The
 * secondary nav (children) appears in the contextual ``SideNav`` on
 * the left of the main content area whenever the active group has
 * children. Top-level rendering is from the same ``dashboard.get`` RPC
 * that drives `SideNav`, so plugin nav contributions show up here for
 * free.
 *
 * On mobile, the horizontal nav collapses to a hamburger that opens a
 * Sheet drawer listing every group + its nested children (because
 * there isn't space for both top + side at once).
 */
export function TopBar() {
  const { user, logout } = useAuth();
  const { connected } = useWebSocket();
  const api = useWsApi();
  const navigate = useNavigate();
  const location = useLocation();
  const isMobile = useIsMobile();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false);
  const [restarting, setRestarting] = useState(false);

  const { data } = useQuery({
    queryKey: ["dashboard", user?.user_id ?? "anon"],
    queryFn: api.getDashboard,
    enabled: connected && !!user,
  });
  const groups: NavGroup[] = data?.nav ?? [];

  const initials =
    user?.display_name
      ?.split(" ")
      .map((n) => n[0])
      .join("")
      .toUpperCase()
      .slice(0, 2) || "?";

  const handleAction = (action: NonNullable<NavItem["action"]>) => {
    setMobileOpen(false);
    if (action === "restart_host") {
      setRestartConfirmOpen(true);
    }
  };

  const confirmRestart = async () => {
    setRestarting(true);
    try {
      await api.restartHost();
    } catch {
      // Socket drops mid-request when the host exits; the connection
      // indicator handles the reconnect.
    } finally {
      setRestartConfirmOpen(false);
      setRestarting(false);
    }
  };

  return (
    <header className="sticky top-0 z-40 flex h-14 items-center gap-2 border-b bg-background/95 px-3 backdrop-blur supports-[backdrop-filter]:bg-background/80 sm:gap-4 sm:px-4">
      {/* Mobile hamburger */}
      <Button
        variant="ghost"
        size="icon-sm"
        className="md:hidden"
        onClick={() => setMobileOpen(true)}
        aria-label="Open navigation"
      >
        <MenuIcon className="size-5" />
      </Button>

      <Link
        to="/"
        className="font-semibold text-lg tracking-tight sm:mr-2"
      >
        Gilbert
      </Link>

      {/* Desktop primary nav — top-level groups only. No popovers; the
          secondary nav for the active group renders in the SideNav. */}
      {!isMobile && (
        <nav className="hidden md:flex items-center gap-0.5 overflow-x-auto overflow-y-hidden">
          {groups.map((group) => (
            <TopGroupButton
              key={group.key}
              group={group}
              active={isGroupActive(group, location.pathname)}
            />
          ))}
        </nav>
      )}

      <div className="ml-auto flex items-center gap-2 sm:gap-3">
        <div
          className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-500" : "bg-red-500"}`}
          title={connected ? "Connected" : "Disconnected"}
        />

        {/* Plugins can drop widgets next to the bell. */}
        <PluginPanelSlot slot="header.widgets" />

        <BrowserSpeakerControl />

        <NotificationBell />

        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                variant="ghost"
                className="relative h-8 w-8 rounded-full"
              />
            }
          >
            <Avatar className="h-8 w-8">
              <AvatarFallback className="text-xs">{initials}</AvatarFallback>
            </Avatar>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <div className="px-2 py-1.5 text-sm">
              <div className="font-medium">{user?.display_name}</div>
              <div className="text-muted-foreground text-xs">
                {user?.email}
              </div>
            </div>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={() => navigate("/account")}>
              Account settings
            </DropdownMenuItem>
            <PluginPanelSlot slot="header.user-menu" />
            <DropdownMenuItem onClick={logout}>Log out</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* Mobile drawer — full hierarchy (groups + their children),
          because there's no room to split into top + side at mobile
          widths. */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-72 p-0">
          <SheetHeader className="px-4 py-3 border-b">
            <SheetTitle className="text-base">Gilbert</SheetTitle>
          </SheetHeader>
          <nav className="flex flex-col px-2 py-2 overflow-y-auto">
            {groups.map((group) => (
              <MobileGroupBlock
                key={group.key}
                group={group}
                onNavigate={() => setMobileOpen(false)}
                onAction={handleAction}
              />
            ))}
          </nav>
        </SheetContent>
      </Sheet>

      <Dialog
        open={restartConfirmOpen}
        onOpenChange={(o) => !restarting && setRestartConfirmOpen(o)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Restart Gilbert?</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            The Gilbert host process will exit and the supervisor will
            relaunch it. Active conversations and WebSocket connections
            will be briefly disconnected and should reconnect automatically.
          </p>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setRestartConfirmOpen(false)}
              disabled={restarting}
            >
              Cancel
            </Button>
            <Button onClick={confirmRestart} disabled={restarting}>
              {restarting ? "Restarting…" : "Restart"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </header>
  );
}

/** True when the current pathname falls inside this group. */
export function isGroupActive(group: NavGroup, pathname: string): boolean {
  if (
    group.url &&
    (pathname === group.url || pathname.startsWith(group.url + "/"))
  ) {
    return true;
  }
  return group.items.some(
    (i) =>
      !!i.url && (pathname === i.url || pathname.startsWith(i.url + "/")),
  );
}

function TopGroupButton({
  group,
  active,
}: {
  group: NavGroup;
  active: boolean;
}) {
  const Icon = groupIconFor(group.icon);
  const color = GROUP_COLORS[group.key] ?? "text-muted-foreground";

  return (
    <Link
      to={group.url}
      title={group.description || group.label}
      className={cn(
        "relative inline-flex items-center gap-1.5 rounded-md px-3 h-9 text-sm transition-colors",
        active
          ? "text-foreground font-medium"
          : "text-foreground/70 hover:text-foreground hover:bg-accent/60",
      )}
    >
      {Icon && <Icon className={cn("size-4", color)} />}
      <span className="hidden lg:inline">{group.label}</span>
      {active && (
        <span
          className={cn(
            "absolute left-2 right-2 -bottom-px h-0.5 rounded-full",
            GROUP_ACCENT[group.key] ?? "bg-primary",
          )}
        />
      )}
    </Link>
  );
}

const GROUP_ACCENT: Record<string, string> = {
  chat: "bg-blue-500",
  inbox: "bg-emerald-500",
  knowledge: "bg-amber-500",
  mcp: "bg-pink-500",
  security: "bg-violet-500",
  system: "bg-slate-500",
};

function MobileGroupBlock({
  group,
  onNavigate,
  onAction,
}: {
  group: NavGroup;
  onNavigate: () => void;
  onAction: (action: NonNullable<NavItem["action"]>) => void;
}) {
  const location = useLocation();
  const Icon = groupIconFor(group.icon);
  const color = GROUP_COLORS[group.key] ?? "text-muted-foreground";
  const active = isGroupActive(group, location.pathname);

  if (group.items.length === 0) {
    return (
      <Link
        to={group.url}
        onClick={onNavigate}
        className={cn(
          "flex items-center gap-3 rounded-md px-3 py-2.5 text-sm transition-colors",
          active
            ? "bg-secondary text-foreground font-medium"
            : "text-foreground/80 hover:bg-accent hover:text-foreground",
        )}
      >
        {Icon && <Icon className={cn("size-4", color)} />}
        <span>{group.label}</span>
      </Link>
    );
  }

  return (
    <div className="mt-3 first:mt-0">
      <div className="px-3 py-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {Icon && <Icon className={cn("size-3.5", color)} />}
        <span>{group.label}</span>
      </div>
      <div className="flex flex-col">
        {group.items.map((item) => {
          const ItemIcon = groupIconFor(item.icon);
          const childActive =
            !!item.url &&
            (location.pathname === item.url ||
              location.pathname.startsWith(item.url + "/"));
          const rowClass = cn(
            "flex items-center gap-3 rounded-md px-3 py-2 pl-6 text-sm transition-colors",
            childActive
              ? "bg-secondary text-foreground font-medium"
              : "text-foreground/80 hover:bg-accent hover:text-foreground",
          );
          if (item.action) {
            return (
              <button
                key={`action:${item.action}:${item.label}`}
                type="button"
                onClick={() => onAction(item.action!)}
                className={cn(rowClass, "text-left")}
              >
                {ItemIcon && <ItemIcon className={cn("size-4", color)} />}
                <span>{item.label}</span>
              </button>
            );
          }
          return (
            <Link
              key={item.url}
              to={item.url!}
              onClick={onNavigate}
              className={rowClass}
            >
              {ItemIcon && <ItemIcon className={cn("size-4", color)} />}
              <span>{item.label}</span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
