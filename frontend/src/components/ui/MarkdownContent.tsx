import { useMemo } from "react";
import { Marked, type TokenizerAndRendererExtension } from "marked";
import { markedHighlight } from "marked-highlight";
import hljs from "highlight.js/lib/core";
import DOMPurify from "dompurify";
import { useAuth } from "@/hooks/useAuth";

// Register common languages — add more as needed
import javascript from "highlight.js/lib/languages/javascript";
import typescript from "highlight.js/lib/languages/typescript";
import python from "highlight.js/lib/languages/python";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import yaml from "highlight.js/lib/languages/yaml";
import css from "highlight.js/lib/languages/css";
import xml from "highlight.js/lib/languages/xml";
import sql from "highlight.js/lib/languages/sql";
import markdown from "highlight.js/lib/languages/markdown";
import diff from "highlight.js/lib/languages/diff";

hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("js", javascript);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("ts", typescript);
hljs.registerLanguage("python", python);
hljs.registerLanguage("py", python);
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("sh", bash);
hljs.registerLanguage("shell", bash);
hljs.registerLanguage("json", json);
hljs.registerLanguage("yaml", yaml);
hljs.registerLanguage("yml", yaml);
hljs.registerLanguage("css", css);
hljs.registerLanguage("html", xml);
hljs.registerLanguage("xml", xml);
hljs.registerLanguage("sql", sql);
hljs.registerLanguage("markdown", markdown);
hljs.registerLanguage("md", markdown);
hljs.registerLanguage("diff", diff);

// Inline @-mention extension. Recognizes ``@[Display Name](user_id)``
// (markdown-link-shaped) and emits a styled chip. The trailing
// ``data-user-id`` attribute is the stable identifier; the visible
// text inside the chip is purely cosmetic so renames don't strand
// historic mentions. Allowed through DOMPurify by the ``ADD_ATTR``
// config below — without that the attribute would be stripped and
// the SPA couldn't tell self-mentions apart from others.
const mentionExtension: TokenizerAndRendererExtension = {
  name: "mention",
  level: "inline",
  start(src: string) {
    const i = src.indexOf("@[");
    return i === -1 ? undefined : i;
  },
  tokenizer(src: string) {
    const match = /^@\[([^\]\n]+)\]\(([A-Za-z0-9._:-]+)\)/.exec(src);
    if (!match) return undefined;
    return {
      type: "mention",
      raw: match[0],
      displayName: match[1],
      userId: match[2],
    };
  },
  renderer(token) {
    // ``escapeHtml`` is intentionally minimal — the displayName is
    // already constrained by the tokenizer regex (no ``]`` or newlines)
    // and DOMPurify runs over the full output afterwards.
    const safeName = String(token.displayName).replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c,
    );
    const safeId = String(token.userId).replace(/"/g, "&quot;");
    return `<span class="mention" data-user-id="${safeId}">@${safeName}</span>`;
  },
};

const marked = new Marked(
  markedHighlight({
    emptyLangClass: "hljs",
    langPrefix: "hljs language-",
    highlight(code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value;
      }
      // Auto-detect for unlabeled blocks
      return hljs.highlightAuto(code).value;
    },
  }),
);
marked.use({ extensions: [mentionExtension] });

interface MarkdownContentProps {
  content: string;
  className?: string;
}

export function MarkdownContent({
  content,
  className = "",
}: MarkdownContentProps) {
  const { user } = useAuth();
  const ownUserId = user?.user_id ?? "";

  const html = useMemo(() => {
    const raw = marked.parse(content, { async: false }) as string;
    // ``data-user-id`` is the only non-standard attribute on the
    // mention chip — DOMPurify would otherwise strip it and the SPA
    // would lose the self-mention highlight hook.
    const sanitized = DOMPurify.sanitize(raw, { ADD_ATTR: ["data-user-id"] });
    // Inject the ``mention-self`` class for chips pointing at the
    // current viewer. Doing this at HTML-generation time (rather
    // than via a post-mount useEffect) makes the highlight present
    // on the very first render — including streaming updates where
    // ``useAuth()`` may have already populated by the time the
    // chip text arrives. The previous useEffect-based approach lost
    // the class every time ``dangerouslySetInnerHTML`` replaced the
    // contents, then raced ``user`` resolution on first paint.
    if (!ownUserId) return sanitized;
    // ``data-user-id`` is double-quoted by the marked extension
    // (see mentionExtension.renderer), so a simple string-replace
    // is safe. We tighten with the class prefix to avoid double-
    // adding if the chip already has the self class somehow.
    const needle = `<span class="mention" data-user-id="${ownUserId}"`;
    const replacement = `<span class="mention mention-self" data-user-id="${ownUserId}"`;
    return sanitized.split(needle).join(replacement);
  }, [content, ownUserId]);

  return (
    <div
      className={`markdown-content ${className}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
