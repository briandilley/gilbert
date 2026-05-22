import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

/**
 * The "page sidebar" lets any rendered page (or plugin-contributed
 * page) take over the contents of the global left ``SideNav`` for the
 * duration of its mount. Useful when the page has its own primary
 * navigation that *is* the sidebar — chat room list, mailbox folder
 * list, plugin-defined section views.
 *
 * When no page has set an override, `SideNav` falls back to rendering
 * the children of the currently-active top-level nav group. When a
 * page's `usePageSidebar(...)` provides content, that content replaces
 * the section children entirely.
 *
 * Implementation note — **two contexts, not one**. The naive design
 * with a single `{content, setContent}` context value re-renders every
 * consumer whenever content updates. That includes pages that only
 * read the setter, which then immediately call setContent again from
 * their useEffect → infinite render loop. Splitting the setter (which
 * is a stable `useState` setter) from the content (which actually
 * changes) means pages consume *only* the stable side and never
 * re-render due to sidebar updates. `SideNav` consumes the content
 * side. Plugin pages do the same thing — they import
 * `usePageSidebar` from `@/components/layout/PageSidebar`.
 */
const SetContentContext = createContext<
  (n: ReactNode | null) => void
>(() => {});

const ContentContext = createContext<ReactNode | null>(null);

export function PageSidebarProvider({ children }: { children: ReactNode }) {
  const [content, setContent] = useState<ReactNode | null>(null);
  // setContent from useState is reference-stable across renders, so
  // SetContentContext's value never changes — consumers of *just* the
  // setter (i.e. all pages) won't re-render when content updates.
  return (
    <SetContentContext.Provider value={setContent}>
      <ContentContext.Provider value={content}>
        {children}
      </ContentContext.Provider>
    </SetContentContext.Provider>
  );
}

/** Read the current page-set override. Used by `SideNav`. */
export function usePageSidebarContent(): ReactNode | null {
  return useContext(ContentContext);
}

/**
 * Declarative hook: pass JSX to render into the global SideNav while
 * this page is mounted. Pass `null` to clear (rarely needed — the
 * cleanup effect handles unmount).
 *
 * The new JSX is published on every render of the calling component,
 * so derived state inside the sidebar tree (current selection,
 * filtered list, …) stays in sync without extra plumbing. The setter
 * comes from a stable-value context, so the calling page does NOT
 * re-render when the sidebar content changes.
 */
export function usePageSidebar(content: ReactNode | null): void {
  const setContent = useContext(SetContentContext);
  useEffect(() => {
    setContent(content);
    return () => setContent(null);
  });
}

/**
 * Component form of `usePageSidebar` for callers that prefer JSX:
 *
 * ```tsx
 * <PageSidebar>
 *   <ChatRoomsList ... />
 * </PageSidebar>
 * ```
 */
export function PageSidebar({ children }: { children: ReactNode }) {
  usePageSidebar(children);
  return null;
}
