import { Link, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "@/hooks/useAuth";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useWsApi } from "@/hooks/useWsApi";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { cn } from "@/lib/utils";
import { groupIconFor, GROUP_ACCENT_BG, GROUP_COLORS } from "./nav-shared";
import { usePageSidebarContent } from "./PageSidebar";
import type { NavGroup, NavItem } from "@/types/dashboard";
import { isGroupActive } from "./TopBar";

/**
 * Contextual left sidebar — renders one of three things, in order of
 * precedence:
 *
 *   1. A page-set override (`usePageSidebar(...)` content). Chat puts
 *      its rooms list here; Inbox puts its mailbox folder list here;
 *      plugin-contributed pages can do the same.
 *
 *   2. The children of the currently-active top-level nav group. E.g.,
 *      on `/security/users` this shows Users / Roles / Tools / AI
 *      Profiles / etc. The list comes from `dashboard.get`'s `nav`,
 *      so plugin nav contributions appear here for free.
 *
 *   3. Nothing — when on a leaf top-level (Chat, Inbox in the absence
 *      of a page override) or off-graph (Account, Dashboard), the
 *      whole sidebar collapses away. Main content gets the space.
 *
 * The `sidebar.bottom` plugin slot is mounted at the foot whenever the
 * sidebar is visible.
 */
export function SideNav() {
  const pageOverride = usePageSidebarContent();
  const location = useLocation();
  const { user } = useAuth();
  const { connected } = useWebSocket();
  const api = useWsApi();

  const { data } = useQuery({
    queryKey: ["dashboard", user?.user_id ?? "anon"],
    queryFn: api.getDashboard,
    enabled: connected && !!user,
  });
  const groups: NavGroup[] = data?.nav ?? [];

  const activeGroup = groups.find((g) => isGroupActive(g, location.pathname));

  // Page override always wins. When no override is set, we only show
  // the sidebar if the active group actually has children to show.
  const showSidebar =
    pageOverride !== null ||
    (activeGroup !== undefined && activeGroup.items.length > 0);

  if (!showSidebar) {
    return null;
  }

  return (
    <aside className="hidden md:flex flex-col w-60 shrink-0 border-r bg-sidebar text-sidebar-foreground">
      <div className="flex flex-col flex-1 min-h-0 overflow-y-auto">
        {pageOverride !== null ? (
          // Page-set content takes the whole sidebar. We don't render
          // a section header here — pages typically render their own
          // (title + actions) inside their content.
          <div className="flex flex-col flex-1 min-h-0">{pageOverride}</div>
        ) : (
          activeGroup && <GroupChildrenList group={activeGroup} />
        )}
      </div>
      <div className="border-t px-2 py-2 shrink-0">
        {/* ``sidebar.bottom`` plugin slot — now-playing widgets,
            active-goal indicators, presence. */}
        <PluginPanelSlot slot="sidebar.bottom" />
      </div>
    </aside>
  );
}

function GroupChildrenList({ group }: { group: NavGroup }) {
  const Icon = groupIconFor(group.icon);
  const color = GROUP_COLORS[group.key] ?? "text-muted-foreground";
  const accentBg = GROUP_ACCENT_BG[group.key] ?? "bg-primary";

  return (
    <div className="flex flex-col">
      <div className="px-4 py-3 flex items-center gap-2 border-b">
        {Icon && <Icon className={cn("size-4", color)} />}
        <h2 className="text-sm font-semibold tracking-tight">{group.label}</h2>
      </div>
      <nav className="flex flex-col gap-0.5 p-2">
        {group.items.map((item) => (
          <ChildRow
            key={item.url ?? `action:${item.action}:${item.label}`}
            item={item}
            color={color}
            accentBg={accentBg}
          />
        ))}
      </nav>
    </div>
  );
}

function ChildRow({
  item,
  color,
  accentBg,
}: {
  item: NavItem;
  color: string;
  accentBg: string;
}) {
  const location = useLocation();
  const Icon = groupIconFor(item.icon);
  const active =
    !!item.url &&
    (location.pathname === item.url ||
      location.pathname.startsWith(item.url + "/"));

  const inner = (
    <>
      {active && (
        <span
          className={cn(
            "absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r-full",
            accentBg,
          )}
        />
      )}
      {Icon && (
        <Icon
          className={cn("size-4 shrink-0", color, !active && "opacity-80")}
        />
      )}
      <span className="truncate">{item.label}</span>
    </>
  );

  const baseClass = cn(
    "group relative flex items-center h-9 px-2.5 gap-2.5 rounded-md text-sm transition-colors",
    active
      ? "bg-sidebar-accent text-sidebar-accent-foreground font-medium"
      : "text-sidebar-foreground/80 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
  );

  // Action items would need a confirm dialog — for now, all
  // production action items (e.g. ``restart_host``) live in the
  // System group and are reachable from the mobile drawer. The
  // desktop section sidebar never renders an action item because
  // group children that are pure RPCs would be confusing here without
  // a host page. If a future action shows up in a section, surface
  // it; until then, treat unknown actions as no-ops.
  if (item.action || !item.url) {
    return (
      <span className={cn(baseClass, "cursor-default opacity-60")}>
        {inner}
      </span>
    );
  }

  return (
    <Link to={item.url} className={baseClass} title={item.description}>
      {inner}
    </Link>
  );
}
