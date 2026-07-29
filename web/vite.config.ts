import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // 開発時は API を同一オリジンに見せる。CORS 設定に依存しないで済む
    proxy: { "/api": "http://localhost:8000" },
  },
});
