import { Component, ReactNode } from "react";

interface Props { children: ReactNode; }
interface State { error: Error | null; }

// Wrap anything that renders API-shaped data with this. Without it, a
// single undefined field (e.g. result.health being missing) throws during
// render and React unmounts the whole tree — the "loads then vanishes"
// symptom. This catches that and shows something instead of nothing.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: { componentStack: string }) {
    // eslint-disable-next-line no-console
    console.error("Dashboard render error:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            maxWidth: 640,
            margin: "10vh auto",
            padding: 24,
            border: "1px solid var(--found)",
            borderRadius: 4,
            background: "rgba(209, 73, 61, 0.08)",
            color: "var(--paper)",
            fontFamily: "var(--font-mono)",
            fontSize: 13,
          }}
        >
          <div style={{ color: "var(--found)", marginBottom: 8, letterSpacing: "0.08em" }}>
            RENDER FAILED
          </div>
          <div style={{ color: "var(--dim)", marginBottom: 12 }}>
            The analysis came back in a shape the dashboard didn't expect. Check the
            browser console for the exact field.
          </div>
          <div style={{ fontSize: 12, color: "var(--paper)", whiteSpace: "pre-wrap" }}>
            {this.state.error.message}
          </div>
          <button
            className="btn-ghost"
            onClick={() => this.setState({ error: null })}
            style={{
              marginTop: 16,
              background: "none",
              border: "1px solid var(--hairline)",
              color: "var(--dim)",
              borderRadius: 4,
              padding: "8px 14px",
              fontSize: 12,
            }}
          >
            DISMISS
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
