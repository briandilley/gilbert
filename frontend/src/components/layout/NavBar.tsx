import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/hooks/useAuth";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useWsApi } from "@/hooks/useWsApi";
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
  MessageSquareIcon,
  FileTextIcon,
  InboxIcon,
  ShieldIcon,
  SlidersHorizontalIcon,
  SettingsIcon,
  DatabaseIcon,
  MonitorIcon,
  ClockIcon,
  PackageIcon,
  PlugIcon,
  PlugZapIcon,
  MenuIcon,
  UsersIcon,
  WrenchIcon,
  SparklesIcon,
  FolderLockIcon,
  RadioIcon,
  RotateCcwIcon,
  TerminalIcon,
  BarChart3Icon,
  ChevronDownIcon,
  type LucideIcon,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { NavGroup, NavItem } from "@/types/dashboard";

/** Map of icon names returned by the backend to lucide components. */
const ICONS: Record<string, LucideIcon> = {
  "message-square": MessageSquareIcon,
  "file-text": FileTextIcon,
  "inbox": InboxIcon,
  "shield": ShieldIcon,
  "sliders": SlidersHorizontalIcon,
  "settings": SettingsIcon,
  "database": DatabaseIcon,
  "monitor": MonitorIcon,
  "clock": ClockIcon,
  "package": PackageIcon,
  "plug": PlugIcon,
  "plug-zap": PlugZapIcon,
  "users": UsersIcon,
  "wrench": WrenchIcon,
  "sparkles": SparklesIcon,
  "folder-lock": FolderLockIcon,
  "radio": RadioIcon,
  "rotate-ccw": RotateCcwIcon,
  "terminal": TerminalIcon,
  "bar-chart": BarChart3Icon,
};

/** Tailwind color for each top-level group's icon. */
const GROUP_COLORS: Record<string, string> = {
  chat: "text-blue-500",
  inbox: "text-green-500",
  knowledge: "text-amber-500",
  mcp: "text-pink-500",
  security: "text-purple-500",
  system: "text-slate-500",
};

function iconFor(name: string): LucideIcon | undefined {
  return ICONS[name];
}

export function NavBar() {
  const { user, logout } = useAuth();
  const { connected } = useWebSocket();
  const api = useWsApi();
  const location = useLocation();
  const navigate = useNavigate();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [restartConfirmOpen, setRestartConfirmOpen] = useState(false);
  const [restarting, setRestarting] = useState(false);

  const handleItemAction = (action: NonNullable<NavItem["action"]>) => {
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
      // Socket will drop mid-request when the host exits; swallow and
      // let the connection indicator show the reconnect.
    } finally {
      setRestartConfirmOpen(false);
      setRestarting(false);
    }
  };

  // Key the dashboard query on the user id so a login / logout
  // swap refetches automatically — otherwise the previous user's
  // cached menu would stick until the tab is refreshed.
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

  const isGroupActive = (group: NavGroup): boolean => {
    if (group.items.length === 0) {
      return location.pathname === group.url ||
        location.pathname.startsWith(group.url + "/");
    }
    return group.items.some(
      (i) =>
        !!i.url &&
        (location.pathname === i.url ||
          location.pathname.startsWith(i.url + "/")),
    );
  };

  return (
    <header className="sticky top-0 z-50 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex h-14 items-center gap-2 px-3 sm:gap-4 sm:px-4">
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

        <Link to="/" className="font-semibold text-lg sm:mr-2">
          Gilbert
        </Link>

        {/* Desktop horizontal nav */}
        <nav className="hidden md:flex items-center gap-1 overflow-x-auto">
          {groups.map((group) => (
            <DesktopNavGroup
              key={group.key}
              group={group}
              active={isGroupActive(group)}
              onAction={handleItemAction}
            />
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-2 sm:gap-3">
          <div
            className={`h-2 w-2 rounded-full ${connected ? "bg-green-500" : "bg-red-500"}`}
            title={connected ? "Connected" : "Disconnected"}
          />

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
              <DropdownMenuItem onClick={logout}>Log out</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {/* Mobile drawer navigation */}
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-72 p-0">
          <SheetHeader>
            <SheetTitle>Gilbert</SheetTitle>
          </SheetHeader>
          <nav className="flex flex-col px-2 pb-4 overflow-y-auto">
            {groups.map((group) => (
              <MobileNavGroup
                key={group.key}
                group={group}
                onNavigate={() => setMobileOpen(false)}
                onAction={handleItemAction}
                active={isGroupActive(group)}
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

// ── Desktop ───────────────────────────────────────────────────────────

function DesktopNavGroup({
  group,
  active,
  onAction,
}: {
  group: NavGroup;
  active: boolean;
  onAction: (action: NonNullable<NavItem["action"]>) => void;
}) {
  const color = GROUP_COLORS[group.key] ?? "text-muted-foreground";
  const Icon = iconFor(group.icon);

  // Leaf group — single button, no dropdown.
  if (group.items.length === 0) {
    return (
      <Link to={group.url} title={group.description || group.label}>
        <Button
          variant={active ? "secondary" : "ghost"}
          size="sm"
          className="gap-1.5"
        >
          {Icon && <Icon className={`h-4 w-4 ${color}`} />}
          <span className="hidden lg:inline">{group.label}</span>
        </Button>
      </Link>
    );
  }

  // Group with children — dropdown.
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant={active ? "secondary" : "ghost"}
            size="sm"
            className="gap-1.5"
            title={group.description || group.label}
          />
        }
      >
        {Icon && <Icon className={`h-4 w-4 ${color}`} />}
        <span className="hidden lg:inline">{group.label}</span>
        <ChevronDownIcon className="h-3 w-3 opacity-60" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        // ``w-auto`` overrides the default ``w-(--anchor-width)`` in
        // DropdownMenuContent so the popover can grow past the trigger
        // button's width; the max-w cap keeps it from escaping the
        // viewport on narrow windows with long descriptions.
        className="w-auto min-w-52 max-w-[min(24rem,calc(100vw-2rem))]"
      >
        {group.items.map((item) => (
          <DropdownSubItem
            key={item.url ?? `action:${item.action}:${item.label}`}
            item={item}
            color={color}
            onAction={onAction}
          />
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function DropdownSubItem({
  item,
  color,
  onAction,
}: {
  item: NavItem;
  color: string;
  onAction: (action: NonNullable<NavItem["action"]>) => void;
}) {
  const Icon = iconFor(item.icon);
  const navigate = useNavigate();
  const handleClick = () => {
    if (item.action) {
      onAction(item.action);
    } else if (item.url) {
      navigate(item.url);
    }
  };
  return (
    <DropdownMenuItem onClick={handleClick} className="cursor-pointer">
      <div className="flex items-start gap-2">
        {Icon && <Icon className={`size-4 mt-0.5 shrink-0 ${color}`} />}
        <div className="flex-1">
          <div className="text-sm font-medium whitespace-nowrap">
            {item.label}
          </div>
          {item.description && (
            <div className="text-xs text-muted-foreground">
              {item.description}
            </div>
          )}
        </div>
      </div>
    </DropdownMenuItem>
  );
}

// ── Mobile ────────────────────────────────────────────────────────────

function MobileNavGroup({
  group,
  onNavigate,
  onAction,
  active,
}: {
  group: NavGroup;
  onNavigate: () => void;
  onAction: (action: NonNullable<NavItem["action"]>) => void;
  active: boolean;
}) {
  const color = GROUP_COLORS[group.key] ?? "text-muted-foreground";
  const Icon = iconFor(group.icon);

  // Leaf — single flat link.
  if (group.items.length === 0) {
    return (
      <Link
        to={group.url}
        onClick={onNavigate}
        className={`flex items-center gap-3 rounded-md px-3 py-2.5 text-sm transition-colors ${
          active
            ? "bg-secondary text-foreground"
            : "text-foreground/80 hover:bg-accent hover:text-foreground"
        }`}
      >
        {Icon && <Icon className={`size-4 ${color}`} />}
        <span>{group.label}</span>
      </Link>
    );
  }

  // Group with children — section header + indented links.
  return (
    <div className="mt-3 first:mt-0">
      <div className="px-3 py-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {Icon && <Icon className={`size-3.5 ${color}`} />}
        <span>{group.label}</span>
      </div>
      <div className="flex flex-col">
        {group.items.map((item) => {
          const ItemIcon = iconFor(item.icon);
          const rowClass =
            "flex items-center gap-3 rounded-md px-3 py-2 pl-6 text-sm transition-colors text-foreground/80 hover:bg-accent hover:text-foreground";
          if (item.action) {
            return (
              <button
                key={`action:${item.action}:${item.label}`}
                type="button"
                onClick={() => onAction(item.action!)}
                className={`${rowClass} text-left`}
              >
                {ItemIcon && <ItemIcon className={`size-4 ${color}`} />}
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
              {ItemIcon && <ItemIcon className={`size-4 ${color}`} />}
              <span>{item.label}</span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
