import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { useAuth } from "@/hooks/useAuth";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { PageHeader } from "@/components/layout/PageHeader";
import {
  MessageSquareIcon,
  FileTextIcon,
  InboxIcon,
  ShieldIcon,
  SettingsIcon,
  DatabaseIcon,
  MonitorIcon,
  LayoutDashboardIcon,
  PlugIcon,
  ArrowUpRightIcon,
  type LucideIcon,
} from "lucide-react";
import { PluginPanelSlot } from "@/components/PluginPanelSlot";
import { UpcomingEventCard } from "@/components/calendar/UpcomingEventCard";
import { cn } from "@/lib/utils";

interface CardStyle {
  icon: LucideIcon;
  /** Full Tailwind text-color class for the icon. Static strings so
   *  the JIT scanner can see them. */
  iconClass: string;
  /** Full bg-color class for the accent rail. */
  railClass: string;
}

const CARD_STYLES: Record<string, CardStyle> = {
  "message-square": { icon: MessageSquareIcon, iconClass: "text-blue-500", railClass: "bg-blue-500" },
  "file-text": { icon: FileTextIcon, iconClass: "text-amber-500", railClass: "bg-amber-500" },
  "inbox": { icon: InboxIcon, iconClass: "text-emerald-500", railClass: "bg-emerald-500" },
  "shield": { icon: ShieldIcon, iconClass: "text-violet-500", railClass: "bg-violet-500" },
  "settings": { icon: SettingsIcon, iconClass: "text-slate-500", railClass: "bg-slate-500" },
  "database": { icon: DatabaseIcon, iconClass: "text-cyan-500", railClass: "bg-cyan-500" },
  "monitor": { icon: MonitorIcon, iconClass: "text-rose-500", railClass: "bg-rose-500" },
  "plug": { icon: PlugIcon, iconClass: "text-pink-500", railClass: "bg-pink-500" },
};

const DEFAULT_STYLE: CardStyle = {
  icon: LayoutDashboardIcon,
  iconClass: "text-muted-foreground",
  railClass: "bg-border-strong",
};

export function DashboardPage() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { user } = useAuth();
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard", user?.user_id ?? "anon"],
    queryFn: api.getDashboard,
    enabled: connected && !!user,
  });

  if (isLoading) {
    return (
      <div>
        <PageHeader eyebrow="HOME" title="Dashboard" />
        <div className="px-6 py-12 text-xs text-muted-foreground">
          Loading…
        </div>
      </div>
    );
  }

  const cardCount = data?.cards.length ?? 0;
  const greeting = user?.display_name
    ? `Welcome back, ${user.display_name.split(" ")[0]}.`
    : "Welcome back.";

  return (
    <div>
      <PageHeader
        eyebrow="HOME"
        title="Dashboard"
        description={`${greeting} ${cardCount} section${cardCount === 1 ? "" : "s"} available.`}
      />

      <div className="px-6 py-6 space-y-6">
        {/* Above-the-grid slot: plugins can drop banner-style widgets,
            system-status panels, etc. before the standard card grid. */}
        <div className="space-y-3 empty:hidden">
          <UpcomingEventCard />
          <PluginPanelSlot slot="dashboard.top" />
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {data?.cards.map((card) => {
            const style = CARD_STYLES[card.icon] ?? DEFAULT_STYLE;
            const Icon = style.icon;
            return (
              <Link key={card.url} to={card.url} className="group">
                <Card
                  className={cn(
                    "h-full relative transition-[border-color,background-color] duration-(--duration-fast) ease-(--ease-out)",
                    "hover:bg-foreground/[0.03] group-hover:border-border-strong",
                  )}
                >
                  {/* Color-coded left rail — narrow vertical bar that
                      encodes the section's identity without filling
                      the entire card. */}
                  <span
                    aria-hidden
                    className={cn(
                      "absolute left-0 top-3 bottom-3 w-[2px] rounded-r-full",
                      "opacity-50 transition-opacity duration-(--duration-fast)",
                      "group-hover:opacity-100",
                      style.railClass,
                    )}
                  />

                  <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                      <Icon className={cn("h-4 w-4 shrink-0", style.iconClass)} />
                      <span className="flex-1">{card.title}</span>
                      <ArrowUpRightIcon
                        className={cn(
                          "size-3.5 shrink-0 text-muted-foreground",
                          "opacity-0 -translate-x-1",
                          "group-hover:opacity-100 group-hover:translate-x-0",
                          "transition-[opacity,transform] duration-(--duration-fast) ease-(--ease-out)",
                        )}
                      />
                    </CardTitle>
                    <CardDescription>{card.description}</CardDescription>
                  </CardHeader>
                </Card>
              </Link>
            );
          })}
        </div>

        {/* Below-the-grid slot: long-form widgets go here. */}
        <div className="space-y-3 empty:hidden">
          <PluginPanelSlot slot="dashboard.bottom" />
        </div>
      </div>
    </div>
  );
}
