import { useQuery } from "@tanstack/react-query";
import { useWsApi } from "@/hooks/useWsApi";
import { useWebSocket } from "@/hooks/useWebSocket";
import { ServiceCard } from "./ServiceCard";
import { PageHeader } from "@/components/layout/PageHeader";

export function SystemPage() {
  const api = useWsApi();
  const { connected } = useWebSocket();
  const { data, isLoading } = useQuery({
    queryKey: ["system-services"],
    queryFn: api.listServices,
    enabled: connected,
  });

  const count = data?.services.length ?? 0;

  return (
    <div>
      <PageHeader
        eyebrow="OPERATIONS"
        title="System"
        description={
          isLoading
            ? "Loading…"
            : `${count} service${count === 1 ? "" : "s"} registered.`
        }
      />
      <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6 sm:py-6 space-y-3">
        {data?.services.map((svc) => (
          <ServiceCard key={svc.name} service={svc} />
        ))}
      </div>
    </div>
  );
}
