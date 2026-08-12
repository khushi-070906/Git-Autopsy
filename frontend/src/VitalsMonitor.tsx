// The signature element: a monitor-style trace. While scanning or on a
// clean repo it plays a looping heartbeat in monitor green. Once a cause
// has been determined it renders as a single flatline in crash-cart red
// with a drop marker at the point of failure — no looping animation, this
// is a settled finding, not a live signal.

const BEAT = "M0,32 L40,32 L48,32 L54,10 L60,54 L66,18 L72,32 L80,32 L200,32";

function beatPath(offset: number) {
  // Shift the same waveform along x so two copies tile seamlessly.
  return BEAT.replace(/(-?\d+(\.\d+)?),/g, (_m, n) => `${Number(n) + offset},`);
}

export function VitalsMonitor({
  mode,
  label,
  markerText,
}: {
  mode: "scanning" | "stable" | "flatline";
  label: string;
  markerText?: string;
}) {
  const color = mode === "flatline" ? "var(--found)" : "var(--live)";

  return (
    <div className="vitals-monitor">
      <span className="vitals-label" style={{ color }}>{label}</span>
      {markerText && (
        <span
          className="vitals-marker"
          style={{ color, border: `1px solid ${color}`, background: "rgba(0,0,0,0.25)" }}
        >
          {markerText}
        </span>
      )}
      {mode === "flatline" ? (
        <svg viewBox="0 0 400 64" preserveAspectRatio="none" width="100%" height="100%">
          <line x1="0" y1="40" x2="240" y2="40" stroke={color} strokeWidth="2" />
          <path
            d="M240,40 L248,40 L254,14 L260,58 L266,26 L272,40 L400,40"
            fill="none"
            stroke={color}
            strokeWidth="2"
          />
          <circle cx="260" cy="58" r="3.5" fill={color} />
        </svg>
      ) : (
        <div className="vitals-track">
          <svg viewBox="0 0 400 64" preserveAspectRatio="none" width="100%" height="100%">
            <path d={beatPath(0)} fill="none" stroke={color} strokeWidth="2" />
            <path d={beatPath(200)} fill="none" stroke={color} strokeWidth="2" />
          </svg>
        </div>
      )}
    </div>
  );
}