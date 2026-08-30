import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./Markdown.module.css";

export type MarkdownProps = {
  markdown: string;
};

/**
 * The one markdown renderer in this app (summaries).
 * Raw HTML stays disabled — react-markdown's default — so a
 * model-generated document can never inject markup; GFM tables/strikethrough
 * are on because models emit them.
 */
export function Markdown({ markdown }: MarkdownProps) {
  return (
    <div className={styles.markdown}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Never emit an <img>: a relative path would 404 against the
          // webview origin and a remote URL is blocked by CSP anyway, so an
          // embedded image renders as its alt text instead of breaking.
          img: ({ alt }) => <span className={styles.missingImage}>{alt || "image"}</span>,
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
