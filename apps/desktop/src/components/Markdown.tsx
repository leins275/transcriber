import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./Markdown.module.css";

export type MarkdownProps = {
  markdown: string;
  /** Relative image name (e.g. `screenshot-0102.png`) -> data URL. Local
   * artifact screenshots reach the webview only as base64 data URLs (the
   * webview has no filesystem access; CSP allows `img-src 'self' data:`),
   * so relative links in stored markdown are resolved through this map and
   * anything unresolvable renders as its alt text instead of a broken
   * image. */
  images?: Record<string, string>;
};

/**
 * The one markdown renderer in this app (summaries, action items).
 * Raw HTML stays disabled — react-markdown's default — so a
 * model-generated document can never inject markup; GFM tables/strikethrough
 * are on because models emit them.
 */
export function Markdown({ markdown, images }: MarkdownProps) {
  return (
    <div className={styles.markdown}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          img: ({ src, alt }) => {
            const resolved =
              typeof src === "string" && images && src in images ? images[src] : null;
            if (!resolved) {
              // Never emit an <img> for a source we cannot resolve: a
              // relative path would 404 against the webview origin and a
              // remote URL is blocked by CSP anyway.
              return <span className={styles.missingImage}>{alt || "image"}</span>;
            }
            return <img src={resolved} alt={alt ?? ""} className={styles.image} />;
          },
          // External links make no sense inside the app shell; render them
          // as plain emphasized text instead of dead anchors.
          a: ({ children }) => <em>{children}</em>,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
