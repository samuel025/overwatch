import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into the Python package so `overwatch run` serves it; `npm run dev` proxies the API.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../overwatch/ui/dist", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8080" } },
});
