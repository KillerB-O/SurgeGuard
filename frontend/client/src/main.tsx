import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider, keepPreviousData } from "@tanstack/react-query";

import App from "./App";
import { AuthProvider } from "@/contexts/AuthContext";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Dashboard polling supplies freshness, so keep the last good data on
      // screen instead of blanking every panel on a transient failure.
      retry: 1,
      refetchOnWindowFocus: false,
      placeholderData: keepPreviousData,
    },
  },
});

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
