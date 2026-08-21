import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ServiceBanner } from "./ServiceBanner";
import type { ServiceStatusView } from "../types";

describe("ServiceBanner", () => {
  it("names the configured base URL and states what to do when unavailable", () => {
    const status: ServiceStatusView = {
      state: "unavailable",
      base_url: "http://127.0.0.1:8756",
      detail: "Transcription service is not responding. Ingest still works.",
    };
    render(<ServiceBanner status={status} />);
    expect(screen.getByText(/http:\/\/127\.0\.0\.1:8756/)).toBeInTheDocument();
    expect(
      screen.getByText("Transcription service is not responding. Ingest still works."),
    ).toBeInTheDocument();
  });

  it("does not falsely promise that failed jobs will resume on their own (E19)", () => {
    // E19: a job that exhausts the poll-error budget is set to `Failed`
    // terminally -- nothing retries or resubmits it. The banner must not
    // claim otherwise; it must tell the operator the actionable truth
    // (re-drop the recording once the service is back).
    const status: ServiceStatusView = {
      state: "unavailable",
      base_url: "http://127.0.0.1:8756",
      detail: null,
    };
    render(<ServiceBanner status={status} />);
    expect(screen.queryByText(/resume once it reconnects/i)).not.toBeInTheDocument();
    expect(screen.getByText(/re-drop/i)).toBeInTheDocument();
  });

  it("shows a starting indication while the sidecar is coming up", () => {
    const status: ServiceStatusView = { state: "starting", base_url: null, detail: null };
    render(<ServiceBanner status={status} />);
    expect(screen.getByText(/starting/i)).toBeInTheDocument();
  });

  it("renders nothing when the service is ready", () => {
    const status: ServiceStatusView = {
      state: "ready",
      base_url: "http://127.0.0.1:8756",
      detail: null,
    };
    const { container } = render(<ServiceBanner status={status} />);
    expect(container).toBeEmptyDOMElement();
  });
});
