import path from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

// Build straight into the Python package so `haddon-serve` serves the bundle with no copy step and one process
// sits in front of the reviewer.  The output is gitignored; a committed build can drift from its source unnoticed.
const OUT = path.resolve(__dirname, "../src/haddon/ui/static")

// `npm run dev` proxies the API to a `haddon-serve` on 8080 (set HADDON_API for another) and adds the header
// nginx adds in production, so the dev server authenticates the same way the deployment does.
const API = process.env.HADDON_API ?? "http://127.0.0.1:8080"
const DEV_USER = process.env.HADDON_DEV_USER ?? "dev"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  build: { outDir: OUT, emptyOutDir: true },
  server: {
    proxy: {
      // Same path rewrite nginx does in production.
      "/ui/v1": {
        target: API,
        changeOrigin: true,
        rewrite: (p: string) => p.replace(/^\/ui/, ""),
        headers: { "X-Haddon-User": DEV_USER },
      },
      "/healthz": { target: API, changeOrigin: true },
      "/readyz": { target: API, changeOrigin: true },
      "/openapi.json": { target: API, changeOrigin: true },
    },
  },
})
