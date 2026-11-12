/**
 * HCCA host plugin (plain ESM, no build step).
 *
 * - supervises the Python sidecar that owns the collaboration state
 *   (spawn + health check + retry + crash log, so a dead sidecar never looks
 *   like a broken tab);
 * - proxies a same-origin page (`/hcca`) and status API (`/api/hcca/*`) so the
 *   conversation tab can embed the dashboard under the DSH web origin (no
 *   cross-origin iframe / CSP surprises).
 */
import { spawn } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync } from "node:fs";
import { createConnection } from "node:net";
import { homedir } from "node:os";

export const name = "@hcca/dsh-hcca";
export const inject = [];

const state = { port: 8765, child: null, starting: false };

function sidecarBase() {
  return `http://127.0.0.1:${state.port}`;
}

async function readSidecar(path, options = {}) {
  const res = await fetch(sidecarBase() + path, { cache: "no-store", ...options });
  const body = await res.text();
  return { status: res.status, body, type: res.headers.get("content-type") || "application/json" };
}

function logFile() {
  return `${homedir()}/.dsh/hcca-sidecar-${state.port}.log`;
}

/** True when nothing is listening on the sidecar port. */
function portFree(port, waitMs = 400) {
  return new Promise((resolve) => {
    const socket = createConnection({ host: "127.0.0.1", port });
    let settled = false;
    const done = (free) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(free);
    };
    socket.once("connect", () => done(false));
    socket.once("error", () => done(true));
    setTimeout(() => done(true), waitMs);
  });
}

async function waitPortFree(port, timeoutMs = 6000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await portFree(port)) return true;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

async function healthy(timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${sidecarBase()}/healthz`, { cache: "no-store" });
      if (res.ok && (await res.json()).version === "0.2.1") return true;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  return false;
}

function killStale() {
  try {
    const pidFile = `${homedir()}/.dsh/hcca-sidecar-${state.port}.pid`;
    if (!existsSync(pidFile)) return;
    const pid = Number(readFileSync(pidFile, "utf8").trim());
    if (Number.isFinite(pid) && pid > 0 && pid !== process.pid) {
      try {
        process.kill(pid, 0);
        process.kill(pid, "SIGKILL");
        console.info(`[hcca] killed stale sidecar ${pid}`);
      } catch {}
    }
  } catch {}
}

async function ensureSidecar(python, root) {
  if (state.child || state.starting) return;
  state.starting = true;
  try {
    if (await healthy(500)) return;
    killStale();
    await waitPortFree(state.port);
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      let log = null;
      try {
        log = openSync(logFile(), "a");
      } catch {}
      const child = spawn(
        python,
        ["-m", "hcca.dsh_worker", "--port", String(state.port)],
        {
          cwd: root,
          detached: true,
          stdio: ["ignore", log || "ignore", log || "ignore"],
          windowsHide: true,
        },
      );
      state.child = child;
      child.on("error", (err) => {
        console.warn("[hcca] sidecar failed to start:", err?.message || err);
      });
      child.on("exit", (code, signal) => {
        console.warn(`[hcca] sidecar exited code=${code} signal=${signal}`);
        if (state.child === child) state.child = null;
      });
      child.unref();
      if (log !== null) {
        try {
          closeSync(log);
        } catch {}
      }
      if (await healthy(9000)) {
        console.info(`[hcca] sidecar on ${sidecarBase()}/`);
        return;
      }
      console.warn(`[hcca] sidecar not healthy (attempt ${attempt}/3)`);
      try {
        child.kill("SIGKILL");
      } catch {}
      if (state.child === child) state.child = null;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    console.error("[hcca] sidecar failed after 3 attempts; see", logFile());
  } finally {
    state.starting = false;
  }
}

export function apply(ctx, config) {
  const options = config || {};
  state.port = Number(options.port || process.env.HCCA_SIDECAR_PORT || 8765);
  if (!Number.isInteger(state.port) || state.port < 1 || state.port > 65535) {
    throw new Error("HCCA sidecar port must be between 1 and 65535");
  }
  process.env.HCCA_SIDECAR_PORT = String(state.port);
  mkdirSync(`${homedir()}/.dsh`, { recursive: true });
  const python = String(options.python || process.env.HCCA_PYTHON || (process.platform === "win32" ? "python" : "python3"));
  const root = String(options.sidecarRoot || process.env.HCCA_SIDECAR_ROOT || process.cwd());

  void ensureSidecar(python, root);
  // A sidecar can die later (port race, OOM, killed by user). Recover without
  // requiring a DSH restart; polling is cheap and only starts one instance.
  const watchdog = setInterval(() => {
    void (async () => {
      if (state.starting) return;
      if (!state.child) {
        await ensureSidecar(python, root);
        return;
      }
      if (!(await healthy(1500))) {
        console.warn("[hcca] sidecar unhealthy; restarting");
        try {
          state.child.kill("SIGKILL");
        } catch {}
        state.child = null;
        await ensureSidecar(python, root);
      }
    })();
  }, 15000);
  watchdog.unref?.();

  // Web-only: same-origin routes used by the HCCA conversation tab.
  ctx.inject(["webServer"], (ctx) => {
    const proxy = async (req, res) => {
      try {
        const url = new URL(req.url || "/", "http://127.0.0.1");
        const target = url.pathname.replace(/^\/api\/hcca/, "/api") + (url.search || "");
        const method = req.method || "GET";
        let body;
        if (method !== "GET" && method !== "HEAD") {
          const chunks = [];
          for await (const chunk of req) chunks.push(Buffer.from(chunk));
          body = Buffer.concat(chunks);
        }
        const payload = await readSidecar(target, {
          method,
          headers: body ? { "content-type": req.headers["content-type"] || "application/json" } : undefined,
          body,
        });
        res.writeHead(payload.status, {
          "content-type": payload.type,
          "cache-control": "no-store",
        });
        res.end(payload.body);
      } catch (err) {
        res.writeHead(503, { "content-type": "application/json" });
        res.end(JSON.stringify({ error: "hcca sidecar unavailable", detail: String(err?.message || err) }));
      }
    };
    const page = async (req, res) => {
      try {
        const url = new URL(req.url || "/hcca", "http://127.0.0.1");
        const payload = await readSidecar("/" + (url.search || ""));
        res.writeHead(payload.status, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
        res.end(payload.body);
      } catch (err) {
        res.writeHead(503, { "content-type": "text/html; charset=utf-8" });
        res.end("<h1>HCCA sidecar unavailable</h1>");
      }
    };
    const disposers = [
      ctx.webServer.register({ kind: "prefix", path: "/api/hcca", handler: proxy }),
      ctx.webServer.register({ kind: "exact", path: "/hcca", handler: page }),
    ];
    ctx.effect?.(() => () => {
      clearInterval(watchdog);
      disposers.forEach((dispose) => dispose?.());
    });
  });
}
