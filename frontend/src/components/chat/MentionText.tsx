import { useAuth } from "@/hooks/useAuth";
import type { ReactNode } from "react";

/**
 * Renders plain text with ``@[Display Name](user_id)`` mention tokens
 * turned into styled chips. Used for **user-authored** chat messages,
 * which deliberately don't go through the full Markdown pipeline —
 * user prose preserves its own whitespace + line breaks via the
 * ``whitespace-pre-wrap`` class on the parent.
 *
 * Assistant messages use ``<MarkdownContent>`` instead; that path
 * teaches Marked.js the same syntax via the ``mentionExtension``.
 * Both end up rendering visually-identical chips (``.mention`` /
 * ``.mention-self``) so the user can't tell which side they came
 * from at a glance.
 */
const MENTION_RE = /@\[([^\]\n]+)\]\(([A-Za-z0-9._:-]+)\)/g;

export function MentionText({ content }: { content: string }) {
  const { user } = useAuth();
  const ownUserId = user?.user_id ?? "";

  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  let chunkKey = 0;
  for (const match of content.matchAll(MENTION_RE)) {
    const start = match.index ?? 0;
    if (start > lastIndex) {
      nodes.push(
        <span key={`t${chunkKey++}`}>{content.slice(lastIndex, start)}</span>,
      );
    }
    const displayName = match[1];
    const userId = match[2];
    const isSelf = ownUserId && userId === ownUserId;
    nodes.push(
      <span
        key={`m${chunkKey++}`}
        className={isSelf ? "mention mention-self" : "mention"}
        data-user-id={userId}
      >
        @{displayName}
      </span>,
    );
    lastIndex = start + match[0].length;
  }
  if (lastIndex < content.length) {
    nodes.push(
      <span key={`t${chunkKey++}`}>{content.slice(lastIndex)}</span>,
    );
  }
  return <>{nodes}</>;
}
