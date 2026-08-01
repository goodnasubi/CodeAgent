import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // 開発時は API を同一オリジンに見せる。CORS 設定に依存しないで済む
    proxy: { "/api": "http://localhost:8000" },
  },
  test: {
    // **jsdom が要るのは `location` のためだけ。** 相対 URL を絶対化する
    // 際に location.origin を基準にしており、node 環境だと未定義で落ちる。
    // 描画自体は renderToStaticMarkup で HTML 文字列にして確かめるので、
    // testing-library の類は入れていない。
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
