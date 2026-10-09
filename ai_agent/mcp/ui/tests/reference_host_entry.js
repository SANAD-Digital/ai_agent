// Reference host: the official AppBridge (with its schema validation) talking to our view in a sandboxed iframe.
import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";

window.startHost = async function ({ html, toolResult, toolInput, caps, context, late }) {
  const log = (window.__log = []);
  const errors = (window.__errors = []);
  const iframe = document.getElementById("f");
  const bridge = new AppBridge(null, { name: "reference-host", version: "1.0.0" }, caps, { hostContext: context });
  bridge.onerror = (e) => errors.push("bridge error: " + (e && e.message));
  bridge.onsizechange = (p) => { log.push("size-changed"); if (p.height) iframe.style.height = p.height + "px"; };
  bridge.oncalltool = async (params) => { log.push("tools/call:" + params.name); (window.__calls = window.__calls || []).push(params); return window.hostReply(params); };
  bridge.onopenlink = async (p) => { log.push("open-link"); return {}; };
  bridge.onmessage = async (p) => { log.push("message"); return {}; };
  bridge.onupdatemodelcontext = async (p) => { log.push("update-model-context"); return {}; };
  bridge.ondownloadfile = async (p) => { log.push("download-file"); return {}; };
  bridge.onrequestdisplaymode = async (p) => ({ mode: p.mode });
  bridge.onloggingmessage = (p) => log.push("log");
  bridge.oninitialized = async () => {
    log.push("initialized");
    try {
      await bridge.sendToolInput({ arguments: toolInput || {} });
      await bridge.sendToolResult(toolResult);
      log.push("result-sent");
    } catch (e) { errors.push("send failed: " + e.message); }
  };
  window.__bridge = bridge;
  window.__ping = async () => { try { await bridge.ping(); return "pong"; } catch (e) { return "ping failed: " + e.message; } };
  if (late) {
    // A host that starts listening only after the document has run (the view's first ui/initialize is lost).
    iframe.srcdoc = html;
    await new Promise((r) => iframe.addEventListener("load", r, { once: true }));
    await new Promise((r) => setTimeout(r, late));
    await bridge.connect(new PostMessageTransport(iframe.contentWindow, iframe.contentWindow));
  } else {
    await bridge.connect(new PostMessageTransport(iframe.contentWindow, iframe.contentWindow));
    iframe.srcdoc = html;
  }
  log.push("bridge-connected");
};
