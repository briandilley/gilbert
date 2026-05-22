// Mention-picker matching logic. Pure functions kept out of ChatInput
// so they're testable without spinning up React.
//
// The picker triggers on an unescaped ``@`` that has whitespace (or
// the start of the message) immediately before it AND a continuation
// of word characters right after, up to the cursor. Examples:
//
//   "@a"         → triggers, query="a"
//   "hi @ali"    → triggers, query="ali"
//   "foo@bar"    → does NOT trigger (in-word, looks like an email)
//   "```\n@x"    → triggers in the SOURCE markdown, but we defer code-
//                  block escaping to the caller (it gets cursor +
//                  surrounding text and can check fence state)

export interface MentionMatch {
  /** Substring index of the ``@`` itself (the byte we'll replace). */
  triggerStart: number;
  /** Substring index just past the last typed char of the query. */
  triggerEnd: number;
  /** Text typed between the ``@`` and the cursor — used to filter
   *  the suggestions list. May be empty when the user just typed @. */
  query: string;
}

/**
 * Detect a mention-in-progress at the given cursor position.
 *
 * Returns ``null`` when the cursor isn't currently authoring a mention
 * (no recent ``@``, the ``@`` is in-word like an email, or it's inside
 * a fenced code block). The caller passes the message + cursor; this
 * function never touches the DOM.
 */
export function detectMentionAtCursor(
  text: string,
  cursor: number,
): MentionMatch | null {
  if (cursor < 0 || cursor > text.length) return null;

  // Walk backwards from the cursor to find an ``@`` that satisfies
  // both: (a) every char between it and the cursor is a valid mention-
  // query character, and (b) the char immediately before it is
  // whitespace or the start of the string.
  let i = cursor;
  while (i > 0) {
    const c = text[i - 1];
    if (c === "@") {
      const before = i >= 2 ? text[i - 2] : "";
      if (before && !/\s/.test(before)) return null; // in-word @
      const query = text.slice(i, cursor);
      if (/[\s)]/.test(query)) return null; // user kept typing past the picker window
      // Inside a fenced code block? Bail — ``@`` inside ``` shouldn't
      // pop the picker.
      if (insideFencedCodeBlock(text, i - 1)) return null;
      return {
        triggerStart: i - 1,
        triggerEnd: cursor,
        query,
      };
    }
    // Stop scanning at whitespace or another ``@`` — the query can't
    // span those.
    if (/\s/.test(c)) return null;
    i -= 1;
  }
  // Cursor at message start
  if (cursor === 0) return null;
  // Reached start of string without an ``@``
  if (text[0] === "@") {
    const query = text.slice(1, cursor);
    if (/[\s)]/.test(query)) return null;
    if (insideFencedCodeBlock(text, 0)) return null;
    return { triggerStart: 0, triggerEnd: cursor, query };
  }
  return null;
}

/**
 * Cheap fenced-code-block check. Counts ``` openers before ``index`` —
 * if odd, we're inside an open code block.
 *
 * Doesn't handle indented code blocks (4-space prefix) or inline
 * single-backtick spans — those are minor edge cases the user can
 * work around with the Escape key.
 */
export function insideFencedCodeBlock(text: string, index: number): boolean {
  const prefix = text.slice(0, index);
  const matches = prefix.match(/```/g);
  return !!matches && matches.length % 2 === 1;
}

/**
 * Build the mention markup we splice into the textarea on selection.
 * Sentence-end punctuation isn't appended — keep the cursor right
 * after the closing paren so the user can type a comma / period / etc.
 */
export function renderMentionMarkup(member: {
  user_id: string;
  display_name: string;
}): string {
  // Inserts the visible-only form ``@<DisplayName> ``. The structured
  // ``@[Name](user_id)`` tag is for storage + rendering, NOT for the
  // chat input — surfacing the user_id in the textarea was disorienting
  // ("@[Dylan](usr_444d0d46df3b)" is not what a person expects to see
  // when they pick a name from a dropdown).
  //
  // The backend's ``resolve_bare_mentions_to_structured`` rewrites
  // ``@<Name>`` to the structured form at send time (case-insensitive
  // match against the room's member list), so persistence + chip
  // rendering + notifications still get the durable form.
  //
  // Newlines stripped because they'd break the visible flow; otherwise
  // the display name passes through unchanged.
  const safeName = member.display_name.replace(/\n/g, " ");
  return `@${safeName} `;
}

/**
 * Filter + rank a member list against the user's typed query. Substring
 * match against display_name; exact-prefix matches sort first so
 * "ali" surfaces Alice before Allison-Alice. Case-insensitive.
 */
export function filterMembers<T extends { display_name: string }>(
  members: T[],
  query: string,
  limit = 8,
): T[] {
  if (!query) return members.slice(0, limit);
  const q = query.toLowerCase();
  const exact: T[] = [];
  const partial: T[] = [];
  for (const m of members) {
    const name = m.display_name.toLowerCase();
    if (name.startsWith(q)) {
      exact.push(m);
    } else if (name.includes(q)) {
      partial.push(m);
    }
  }
  return [...exact, ...partial].slice(0, limit);
}
