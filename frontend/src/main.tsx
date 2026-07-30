import { Component, StrictMode, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HashRouter } from "react-router-dom";
import { App } from "./App";
import "./styles.css";

const client = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, retry: 1 } } });

class FrontendErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ESCO Engineering City failed to render", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <main className="startup-error" role="alert">
          <div className="brand-mark">E</div>
          <p className="eyebrow">FRONTEND STARTUP ERROR</p>
          <h1>Engineering City could not start</h1>
          <p>{this.state.error.message || "An unexpected rendering error occurred."}</p>
          <p className="startup-help">
            Run <code>.\scripts\product\run-demo.ps1</code> and open{" "}
            <code>http://127.0.0.1:4173</code>. Do not open the generated HTML file directly.
          </p>
          <button onClick={() => window.location.reload()}>Reload application</button>
        </main>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <FrontendErrorBoundary>
      <QueryClientProvider client={client}>
        <HashRouter><App /></HashRouter>
      </QueryClientProvider>
    </FrontendErrorBoundary>
  </StrictMode>
);
