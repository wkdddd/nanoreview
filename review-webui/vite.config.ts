import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

function numericArg(name: string): number | undefined {
  const index = process.argv.indexOf(name);
  if (index >= 0 && index + 1 < process.argv.length) {
    const value = Number(process.argv[index + 1]);
    return Number.isFinite(value) ? value : undefined;
  }
  const prefix = `${name}=`;
  const match = process.argv.find((arg) => arg.startsWith(prefix));
  if (match) {
    const value = Number(match.slice(prefix.length));
    return Number.isFinite(value) ? value : undefined;
  }
  return undefined;
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.NANOBOT_API_URL ?? "http://127.0.0.1:8765";
  const wsTarget = target.replace(/^http/, "ws");
  const port = numericArg("--port") ?? Number(env.VITE_PORT ?? 5175);
  const hmrPort = Number(env.VITE_HMR_PORT ?? port + 1);

  return {
    // 使用相对路径，便于在非域名根路径下部署（如 /nanoreview/）
    base: "./",
    root: process.cwd(),
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    build: {
      outDir: path.resolve(__dirname, "../nanoreview/web/dist"),
      emptyOutDir: true,
      sourcemap: false,
    },
    server: {
      host: "127.0.0.1",
      port,
      strictPort: true,
      hmr: {
        host: "127.0.0.1",
        port: hmrPort,
      },
      proxy: {
        "/webui": { target, changeOrigin: true },
        "/api": { target, changeOrigin: true },
        "/auth": { target, changeOrigin: true },
        "/": {
          target: wsTarget,
          ws: true,
          changeOrigin: true,
          bypass: (req) =>
            req.headers.upgrade === "websocket" ? undefined : req.url,
        },
      },
    },
  };
});
