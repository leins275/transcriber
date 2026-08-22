import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Logo } from "./Logo";

describe("Logo", () => {
  it("is labelled so the brand reads to a screen reader", () => {
    render(<Logo />);
    expect(screen.getByRole("img", { name: /transcriber/i })).toBeInTheDocument();
  });

  it("takes its size from the prop and stays square", () => {
    render(<Logo size={48} />);
    const svg = screen.getByRole("img", { name: /transcriber/i });
    expect(svg).toHaveAttribute("width", "48");
    expect(svg).toHaveAttribute("height", "48");
  });

  it("draws through theme tokens so it survives the dark palette", () => {
    const { container } = render(<Logo />);
    const html = container.innerHTML;
    expect(html).toContain("currentColor");
    expect(html).toContain("var(--accent)");
    // No hard-coded artwork colours that would go invisible in one theme.
    expect(html).not.toContain("#201f1d");
  });
});
