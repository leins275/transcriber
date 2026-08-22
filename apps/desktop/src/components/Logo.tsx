export type LogoProps = {
  /** Rendered edge length in px (the mark is square). */
  size?: number;
};

/**
 * The app mark: a waveform resolving into lines of text -- audio becoming a
 * transcript. Inline SVG rather than an `<img>` so it costs no request and
 * needs no `img-src` relaxation of the app's strict CSP (tauri.conf.json:
 * `default-src 'self'`).
 *
 * The bars are drawn in `currentColor` and the text lines in `var(--accent)`,
 * which is exactly the light-theme source artwork (`#201f1d` / `#c28d41`) --
 * so the same mark stays legible when the dark palette flips `--text` and
 * `--accent` (styles.css). Presentational only.
 */
export function Logo({ size = 28 }: LogoProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 96 96"
      role="img"
      aria-label="Transcriber"
      fill="none"
    >
      <rect
        x="4"
        y="4"
        width="88"
        height="88"
        rx="8"
        fill="none"
        stroke="var(--accent)"
        strokeWidth="3"
      />
      <g stroke="currentColor" strokeWidth="5" strokeLinecap="round">
        <line x1="24" y1="34" x2="24" y2="62" />
        <line x1="36" y1="24" x2="36" y2="72" />
        <line x1="48" y1="40" x2="48" y2="56" />
      </g>
      <g stroke="var(--accent)" strokeWidth="5" strokeLinecap="round">
        <line x1="60" y1="40" x2="76" y2="40" />
        <line x1="60" y1="52" x2="76" y2="52" />
        <line x1="60" y1="64" x2="70" y2="64" />
      </g>
    </svg>
  );
}
