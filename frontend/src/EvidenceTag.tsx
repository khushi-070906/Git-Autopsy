import { EvidenceItem } from "./api";

const TAG_COLOR: Record<string, string> = {
  FACT: "var(--tag-fact)",
  EVIDENCE: "var(--tag-evidence)",
  INFERENCE: "var(--tag-inference)",
  RECOMMENDATION: "var(--tag-recommendation)",
};

export function EvidenceTag({ item }: { item: EvidenceItem }) {
  const color = TAG_COLOR[item.kind] || "var(--dim)";
  return (
    <div
      style={{
        display: "flex",
        gap: 10,
        padding: "8px 0",
        borderBottom: "1px solid var(--hairline)",
        alignItems: "flex-start",
      }}
    >
      <span
        style={{
          flexShrink: 0,
          width: 108,
          fontSize: 10,
          letterSpacing: "0.1em",
          textTransform: "uppercase",
          fontWeight: 700,
          color,
          border: `1px solid ${color}`,
          borderRadius: 2,
          padding: "2px 6px",
          textAlign: "center",
          fontFamily: "var(--font-mono)",
        }}
      >
        {item.kind}
      </span>
      <span style={{ color: "var(--paper)", fontSize: 13, lineHeight: 1.5 }}>{item.text}</span>
    </div>
  );
}
