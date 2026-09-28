import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  // The dev server proxies the API, so `npm run dev` works against a local
  // `docker compose up` on port 8000 the same way the image serves both.
  server: {
    proxy: {
      "/runs": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
