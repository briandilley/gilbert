import type { Agent } from "@/types/agent";

type Size = "xs" | "sm" | "md" | "lg";

const SIZE_PX: Record<Size, number> = { xs: 16, sm: 24, md: 40, lg: 96 };

interface Props {
  agent: Pick<Agent, "_id" | "avatar_kind" | "avatar_value">;
  size?: Size;
  className?: string;
}

/**
 * Dumb avatar renderer for an Agent.
 *
 * - ``image`` kind hits the ``/api/agents/<id>/avatar`` HTTP route. The
 *   caller is expected to only render the image kind when the agent
 *   actually has an uploaded avatar; the route returns 404 otherwise
 *   and the ``<img>`` will silently fail.
 * - ``emoji`` / ``icon`` fall back to rendering ``avatar_value`` as
 *   text. Lucide-by-name lookup for the ``icon`` kind is a follow-up.
 * - When ``avatar_value`` is empty we render a robot emoji as a
 *   harmless default.
 */
export function AgentAvatar({ agent, size = "md", className }: Props) {
  const px = SIZE_PX[size];
  const baseStyle = { width: px, height: px } as const;

  if (agent.avatar_kind === "image" && agent.avatar_value) {
    return (
      <img
        src={`/api/agents/${encodeURIComponent(agent._id)}/avatar`}
        width={px}
        height={px}
        className={`rounded-full object-cover ${className ?? ""}`}
        style={baseStyle}
        alt=""
      />
    );
  }

  const text = agent.avatar_value || "🤖";
  return (
    <span
      className={`inline-flex items-center justify-center rounded-full bg-muted text-foreground ${className ?? ""}`}
      style={{ ...baseStyle, fontSize: px * 0.6, lineHeight: `${px}px` }}
      aria-hidden
    >
      {text}
    </span>
  );
}
