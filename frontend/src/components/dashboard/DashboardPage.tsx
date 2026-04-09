import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  MessageSquareIcon,
  FileTextIcon,
  InboxIcon,
  ShieldIcon,
  SettingsIcon,
  DatabaseIcon,
  MonitorIcon,
  LayoutDashboardIcon,
  type LucideIcon,
} from "lucide-react";

interface CardStyle {
  icon: LucideIcon;
  color: string;
  border: string;
}

const CARD_STYLES: Record<string, CardStyle> = {
  "/chat": { icon: MessageSquareIcon, color: "text-blue-500", border: "border-blue-500/40" },
  "/documents": { icon: FileTextIcon, color: "text-amber-500", border: "border-amber-500/40" },
  "/inbox": { icon: InboxIcon, color: "text-green-500", border: "border-green-500/40" },
  "/roles": { icon: ShieldIcon, color: "text-purple-500", border: "border-purple-500/40" },
  "/system": { icon: SettingsIcon, color: "text-slate-500", border: "border-slate-500/40" },
  "/entities": { icon: DatabaseIcon, color: "text-cyan-500", border: "border-cyan-500/40" },
  "/screens": { icon: MonitorIcon, color: "text-rose-500", border: "border-rose-500/40" },
};

const DEFAULT_STYLE: CardStyle = {
  icon: LayoutDashboardIcon,
  color: "text-muted-foreground",
  border: "border-border",
};

export function DashboardPage() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard"],
    queryFn: api.getDashboard,
    enabled: connected,
  });

  if (isLoading) {
    return (
      <div className="p-6 text-muted-foreground">Loading dashboard...</div>
    );
  }

  return (
    <div className="p-6">
      <h1 className="text-2xl font-semibold mb-6 text-center">Gilbert</h1>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
        {data?.cards.map((card) => {
          const style = CARD_STYLES[card.url] ?? DEFAULT_STYLE;
          const Icon = style.icon;
          return (
            <Link key={card.url} to={card.url}>
              <Card className={`h-full transition-colors hover:bg-accent border-2 ${style.border}`}>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <Icon className={`h-5 w-5 ${style.color}`} />
                    {card.title}
                  </CardTitle>
                  <CardDescription>{card.description}</CardDescription>
                </CardHeader>
              </Card>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
