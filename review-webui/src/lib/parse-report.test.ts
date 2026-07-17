import { describe, it, expect } from "vitest";
import {
  parseDimensions,
  cleanListItem,
  isListItem,
  parseRecommendations,
} from "./parse-report";

describe("parseDimensions", () => {
  it("parses explicit 'dimension — status' lines", () => {
    const body = "Security — no findings\nTests — 2 issues found";
    const result = parseDimensions(body);
    expect(result).toHaveLength(2);
    expect(result[0].dimension).toBe("Security");
    expect(result[0].status).toBe("done");
    expect(result[0].acceptedCount).toBe(0);
    expect(result[1].dimension).toBe("Tests");
    expect(result[1].status).toBe("2 issues found");
    expect(result[1].acceptedCount).toBe(1);
  });

  it("parses bolded dimension names with separator", () => {
    const body = "**Security** — no findings\n**Architecture** — validated";
    const result = parseDimensions(body);
    expect(result).toHaveLength(2);
    expect(result[0].dimension).toBe("Security");
    expect(result[0].status).toBe("done");
    expect(result[1].dimension).toBe("Architecture");
    expect(result[1].status).toBe("validated");
  });

  it("accepts standalone dimensions as Markdown list items", () => {
    const body = "- Security\n- Tests\n- Architecture";
    const result = parseDimensions(body);
    expect(result).toHaveLength(3);
    expect(result[0].dimension).toBe("Security");
    expect(result[0].status).toBe("validated");
    expect(result[1].dimension).toBe("Tests");
    expect(result[2].dimension).toBe("Architecture");
  });

  it("accepts numbered list items as standalone dimensions", () => {
    const body = "1. Security\n2. Tests";
    const result = parseDimensions(body);
    expect(result).toHaveLength(2);
    expect(result[0].dimension).toBe("Security");
    expect(result[1].dimension).toBe("Tests");
  });

  it("ignores prose and explanation text", () => {
    const body = [
      "The following dimensions were reviewed:",
      "Security — no findings",
      "This section covers the security analysis of the codebase.",
      "Tests — validated",
    ].join("\n");
    const result = parseDimensions(body);
    expect(result).toHaveLength(2);
    expect(result[0].dimension).toBe("Security");
    expect(result[1].dimension).toBe("Tests");
  });

  it("ignores continuation lines that are not list items", () => {
    const body = [
      "Security — no findings",
      "  continued from above with more detail",
      "Tests — validated",
    ].join("\n");
    const result = parseDimensions(body);
    expect(result).toHaveLength(2);
    expect(result[0].dimension).toBe("Security");
    expect(result[1].dimension).toBe("Tests");
  });

  it("converts '无发现' to done status", () => {
    const body = "安全 — 无发现";
    const result = parseDimensions(body);
    expect(result).toHaveLength(1);
    expect(result[0].status).toBe("done");
    expect(result[0].acceptedCount).toBe(0);
  });

  it("converts 'no-findings' and 'no_findings' to done status", () => {
    const body = "Security — no-findings\nTests — no_findings";
    const result = parseDimensions(body);
    expect(result).toHaveLength(2);
    expect(result[0].status).toBe("done");
    expect(result[1].status).toBe("done");
  });

  it("returns empty array for empty body", () => {
    expect(parseDimensions("")).toEqual([]);
    expect(parseDimensions("   \n  \n")).toEqual([]);
  });

  it("handles malformed lines gracefully", () => {
    const body = "---\n**\nSecurity — validated";
    const result = parseDimensions(body);
    expect(result).toHaveLength(1);
    expect(result[0].dimension).toBe("Security");
  });

  it("strips checkbox markers from list items", () => {
    const body = "- [x] Security\n- [ ] Tests";
    const result = parseDimensions(body);
    expect(result).toHaveLength(2);
    expect(result[0].dimension).toBe("Security");
    expect(result[1].dimension).toBe("Tests");
  });
});

describe("cleanListItem", () => {
  it("strips bullet markers", () => {
    expect(cleanListItem("- hello")).toBe("hello");
    expect(cleanListItem("* hello")).toBe("hello");
    expect(cleanListItem("+ hello")).toBe("hello");
  });

  it("strips numbered list markers", () => {
    expect(cleanListItem("1. hello")).toBe("hello");
    expect(cleanListItem("1) hello")).toBe("hello");
  });

  it("strips checkbox markers", () => {
    expect(cleanListItem("- [x] done")).toBe("done");
    expect(cleanListItem("- [ ] todo")).toBe("todo");
  });

  it("unescapes markdown specials", () => {
    expect(cleanListItem("\\*not bold\\*")).toBe("*not bold*");
  });
});

describe("isListItem", () => {
  it("detects bullet markers", () => {
    expect(isListItem("- hello")).toBe(true);
    expect(isListItem("* hello")).toBe(true);
    expect(isListItem("+ hello")).toBe(true);
  });

  it("detects numbered markers", () => {
    expect(isListItem("1. hello")).toBe(true);
    expect(isListItem("1) hello")).toBe(true);
  });

  it("rejects plain text", () => {
    expect(isListItem("hello world")).toBe(false);
    expect(isListItem("  continued text")).toBe(false);
  });
});

describe("parseRecommendations", () => {
  it("parses list items", () => {
    const body = "- Fix SQL injection\n- Add test coverage";
    const result = parseRecommendations(body);
    expect(result).toEqual(["Fix SQL injection", "Add test coverage"]);
  });

  it("filters out 'no fixes' lines", () => {
    const body = "No priority fixes needed";
    const result = parseRecommendations(body);
    expect(result).toEqual([]);
  });
});
