/**
 * Pure parsing utilities for review report sections.
 *
 * Extracted from ``useReviewSession.ts`` so they can be unit-tested
 * without importing the React hook.
 */

export interface DimensionResult {
  dimension: string;
  status: string;
  acceptedCount: number;
  rejectedCount: number;
  uncertainCount: number;
}

/** Strip Markdown list-item syntax (bullets, numbers, checkboxes) and unescape. */
export function cleanListItem(line: string): string {
  return line
    .trim()
    .replace(/^[-*+]\s+/, "")
    .replace(/^\d+[.)]\s+/, "")
    .replace(/^\[[ xX-]\]\s+/, "")
    .replace(/\\([\\`*_{}\[\]()#+\-.!|])/g, "$1")
    .trim();
}

/** Check whether a raw line starts with a Markdown list-item marker. */
export function isListItem(line: string): boolean {
  const trimmed = line.trim();
  return /^[-*+]\s+/.test(trimmed) || /^\d+[.)]\s+/.test(trimmed);
}

/** Parse a "Recommendations" section body into a list of strings. */
export function parseRecommendations(body: string): string[] {
  if (!body.trim()) return [];
  return body
    .split(/\r?\n/)
    .map(cleanListItem)
    .filter((line) => line && !/^no (?:priority )?(?:fixes|recommendations)/i.test(line));
}

/** Count the number of Markdown list items in a section body. */
export function countListItems(body: string): number {
  if (!body.trim()) return 0;
  return body
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => /^[-*+]\s+/.test(line) || /^\d+[.)]\s+/.test(line))
    .length;
}

/**
 * Parse a "Checks Performed" / "Review Dimensions" section body into
 * structured dimension results.
 *
 * Acceptance rules:
 * - Lines with an explicit ``dimension — status`` separator are always parsed.
 * - Standalone dimensions that appeared as Markdown list items (``- Security``)
 *   are accepted with a default ``"validated"`` status.
 * - Prose, explanation text, and continuation lines that lack a separator
 *   and are not list items are ignored.
 *
 * The ``no findings`` / ``无发现`` status is converted to ``status: "done"``.
 */
export function parseDimensions(body: string): DimensionResult[] {
  if (!body.trim()) return [];
  return body
    .split(/\r?\n/)
    .map((rawLine) => {
      const wasListItem = isListItem(rawLine);
      const line = cleanListItem(rawLine);
      const match = line.match(/^(?:\*\*)?([^*\u2014-]+?)(?:\*\*)?\s*[\u2014-]\s*(.+)$/);
      // Accept lines that have an explicit "dimension — status" separator,
      // or standalone dimensions that appeared as Markdown list items.
      // Ignore prose, explanation text, and continuation lines.
      if (!match && !wasListItem) return null;
      const dimension = match ? match[1].trim() : line.trim();
      const rawStatus = match ? match[2].trim() : "validated";
      if (!dimension) return null;
      const noFindings = /no[_\s-]?findings|无发现/i.test(rawStatus);
      return {
        dimension,
        status: noFindings ? "done" : rawStatus,
        acceptedCount: noFindings ? 0 : 1,
        rejectedCount: 0,
        uncertainCount: 0,
      };
    })
    .filter((item): item is DimensionResult => item !== null);
}

