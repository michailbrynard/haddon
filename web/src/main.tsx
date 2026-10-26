import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter } from "react-router-dom"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { App } from "./App"
import { Toaster } from "@/components/ui/sonner"
import "./index.css"

// A stale balance is worse than an extra request, so cached reads go stale after ten seconds and a window
// that regains focus refetches.
const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 10_000, retry: 1, refetchOnWindowFocus: true } },
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <App />
        <Toaster position="bottom-right" />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
