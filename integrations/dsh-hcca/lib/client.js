window.__ModuleLoader__.load({
  id: "@hcca/dsh-hcca",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    const React = require("react");

    const BG = "var(--dsw-alias-bg-base, #171717)";

    function HccaView(props) {
      const preset = String(props?.preset || "").trim();
      const sessionId = String(props?.sessionId || "");
      const enabled = !preset || preset === "hcca";
      const [online, setOnline] = React.useState(true);
      const [retry, setRetry] = React.useState(0);
      const [frameVersion, setFrameVersion] = React.useState(0);
      const frameRef = React.useRef(null);
      const loadRef = React.useRef({ failed: false, notify: null });
      const dashboardReady = () => {
        try {
          return Boolean(frameRef.current?.contentDocument?.getElementById("canvas"));
        } catch (err) {
          return false;
        }
      };
      const onFrameLoad = () => {
        const ready = dashboardReady();
        loadRef.current.failed = !ready;
        if (!ready) setOnline(false);
        loadRef.current.notify?.(ready);
      };

      React.useEffect(() => {
        if (!enabled) return;
        let cancelled = false;
        let timer = null;
        let deadline = null;
        let controller = null;
        let inFlight = false;
        let wasOffline = !online || loadRef.current.failed;
        const schedule = () => {
          clearTimeout(timer);
          if (!cancelled) timer = setTimeout(probe, 8000);
        };
        const probe = async () => {
          if (cancelled || inFlight) return;
          inFlight = true;
          controller = new AbortController();
          deadline = setTimeout(() => controller?.abort(), 6000);
          let healthy = false;
          try {
            const response = await fetch(`/api/hcca/status?session=${encodeURIComponent(sessionId)}`, {
              cache: "no-store", signal: controller.signal,
            });
            healthy = response.ok;
          } catch (err) {
            healthy = false;
          } finally {
            clearTimeout(deadline);
            inFlight = false;
          }
          if (cancelled) return;
          setOnline(healthy);
          if (!healthy) {
            wasOffline = true;
            schedule();
          } else if (wasOffline && !dashboardReady()) {
            // A failed initial navigation needs a retry. An existing dashboard
            // keeps its DOM and recovers through its own status polling.
            wasOffline = false;
            setFrameVersion((version) => version + 1);
          }
        };
        const onLoad = (ready) => {
          if (ready) return;
          wasOffline = true;
          schedule();
        };
        loadRef.current.notify = onLoad;
        probe();
        return () => {
          cancelled = true;
          clearTimeout(timer);
          clearTimeout(deadline);
          controller?.abort();
          if (loadRef.current.notify === onLoad) loadRef.current.notify = null;
        };
      }, [enabled, sessionId, retry]);

      if (preset && preset !== "hcca") {
        return React.createElement(
          "div",
          {
            style: {
              height: "100%", display: "grid", placeItems: "center", background: BG,
              color: "var(--dsw-alias-label-secondary, #a3a3a3)", textAlign: "center", padding: 24,
              font: "13px -apple-system, Segoe UI, sans-serif",
            },
          },
          React.createElement("div", null,
            React.createElement("div", { style: { fontWeight: 600, marginBottom: 6, color: "var(--dsw-alias-label-primary, #ececec)" } },
              "HCCA 仅在 HCCA 模式下可用"),
            React.createElement("div", null, "请新建会话并选择 HCCA 模式（当前: " + preset + "）。"),
          ),
        );
      }
      return React.createElement("div", {
        style: { position: "relative", width: "100%", height: "100%", minHeight: 0, background: BG },
      },
        React.createElement("iframe", {
          ref: frameRef,
          onLoad: onFrameLoad,
          title: "HCCA",
          src: `/hcca?embedded=1&session=${encodeURIComponent(sessionId)}&t=${frameVersion}`,
          style: { width: "100%", height: "100%", border: 0, display: "block", background: BG },
        }),
        !online && React.createElement("div", {
          role: "status",
          style: {
            position: "absolute", left: 16, right: 16, bottom: 16, display: "flex",
            alignItems: "center", gap: 12, padding: "10px 14px", borderRadius: 10,
            border: "1px solid var(--dsw-alias-border-l2, #3d3d3d)",
            background: "var(--dsw-alias-bg-l1, #232323)",
            color: "var(--dsw-alias-label-secondary, #a3a3a3)",
            boxShadow: "0 4px 20px rgba(0,0,0,.2)", font: "12px -apple-system, Segoe UI, sans-serif",
          },
        },
          React.createElement("span", { style: { flex: 1 } }, "HCCA 连接暂时中断，正在自动重试…"),
          React.createElement("button", {
            onClick: () => setRetry((attempt) => attempt + 1),
            style: {
              flex: "none", padding: "4px 10px", borderRadius: 6,
              border: "1px solid var(--dsw-alias-border-l2, #3d3d3d)",
              background: "transparent", color: "inherit", cursor: "pointer",
            },
          }, "重试"),
        ),
      );
    }

    const inject = ["slots", "sessions"];

    function apply(ctx) {
      const presetOf = (sessionId) => {
        try {
          const binding = ctx.sessions?.binding?.(sessionId);
          const session = binding?.session;
          const value =
            session?.projectionValues?.agentPreset ??
            session?.projection?.agentPreset ??
            binding?.projectionValues?.agentPreset ??
            "";
          return typeof value === "string" ? value : String(value?.id || value?.presetId || "");
        } catch (err) {
          return "";
        }
      };
      ctx.slots.inject("conversation.view", () =>
        ctx.slots.register({
          name: "conversation.view",
          id: "hcca",
          order: 15,
          label: () => "HCCA",
          inject: (sessionId) => ({ sessionId, preset: presetOf(sessionId) }),
        }, HccaView),
      );
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
